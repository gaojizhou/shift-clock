import json
from datetime import datetime, timezone

from app.models import AuditLog, LaoqinPunch, AttendanceRecord
from app.scan_active import SCAN_STATUS_CANCELLED
from app.services.anomaly_service import get_anomalies
from app.services.export_service import build_export_meta
from app.services.recon_service import get_recon_for


def _seed_t0001_day(db_session, f):
    day = "2026-04-10"
    db_session.add(
        LaoqinPunch(
            emp_id="T0001",
            punch_time=datetime.fromisoformat(f"{day}T09:00:00+08:00"),
            inout_type="进",
            natual_date=day,
        )
    )
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.fromisoformat(f"{day}T09:15:00+08:00"),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()
    return day


def test_exclude_voids_scan_and_recon(client, db_session):
    from app.models import Furnace

    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    day = _seed_t0001_day(db_session, f)

    get_anomalies(db_session, day)

    client.post("/api/v1/auth/login", json={"userid": "hr1", "role": "hr"})
    client.post(
        "/api/v1/confirm/exclude",
        json={
            "furnace": f.furnace_no,
            "region": f.region,
            "date": day,
            "empId": "T0001",
            "excluded": True,
            "reason": "错扫",
        },
    )

    row = (
        db_session.query(AttendanceRecord)
        .filter_by(emp_id="T0001", natual_date=day)
        .first()
    )
    assert row.status == SCAN_STATUS_CANCELLED
    assert get_recon_for(db_session, "T0001", day)["status"] == "劳勤有工作点无"

    after = get_anomalies(db_session, day)
    items = after if isinstance(after, list) else after.get("items", [])
    # 取消后已不属于工作点有效员工日；孤立劳勤证据不能重新生成工作台行。
    assert items == []

    meta = build_export_meta(db_session, month="2026-04", furnaces=[f.furnace_no], emp_id="T0001")
    assert meta["payrollSheet"]["sampleRow"]["实际出勤天数"] == 0
    assert meta["payrollSheet"]["rowCount"] == 0
    assert meta["payrollSheet"]["rows"] == []


def test_exclude_writes_audit_log_with_reason(client, db_session):
    """取消打卡必须留痕：写 confirm_exclude 审计且 reason 落库。"""
    from app.models import Furnace

    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    day = _seed_t0001_day(db_session, f)

    client.post("/api/v1/auth/login", json={"userid": "hr1", "role": "hr"})
    client.post(
        "/api/v1/confirm/exclude",
        json={
            "furnace": f.furnace_no,
            "region": f.region,
            "date": day,
            "empId": "T0001",
            "excluded": True,
            "reason": "代扫",
        },
    )

    log = (
        db_session.query(AuditLog)
        .filter_by(action="confirm_exclude")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log is not None
    payload = json.loads(log.payload)
    assert payload["empId"] == "T0001"
    assert payload["date"] == day
    assert payload["reason"] == "代扫"


def test_exclude_does_not_physically_delete(client, db_session):
    """回归守护：取消为软取消，记录仍在表中（status=已取消），绝不物理删除。"""
    from app.models import Furnace

    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    day = _seed_t0001_day(db_session, f)

    before = db_session.query(AttendanceRecord).filter_by(emp_id="T0001", natual_date=day).count()
    assert before == 1

    client.post("/api/v1/auth/login", json={"userid": "hr1", "role": "hr"})
    client.post(
        "/api/v1/confirm/exclude",
        json={
            "furnace": f.furnace_no,
            "region": f.region,
            "date": day,
            "empId": "T0001",
            "excluded": True,
            "reason": "错扫",
        },
    )

    # 行数不变，记录仍在（软取消），状态置为已取消
    after = db_session.query(AttendanceRecord).filter_by(emp_id="T0001", natual_date=day).count()
    assert after == before
    row = db_session.query(AttendanceRecord).filter_by(emp_id="T0001", natual_date=day).first()
    assert row.status == SCAN_STATUS_CANCELLED

    # 恢复后记录仍是同一条（未删未重建），状态回 active
    client.post(
        "/api/v1/confirm/exclude",
        json={
            "furnace": f.furnace_no,
            "region": f.region,
            "date": day,
            "empId": "T0001",
            "excluded": False,
        },
    )
    restored = db_session.query(AttendanceRecord).filter_by(emp_id="T0001", natual_date=day).all()
    assert len(restored) == 1
    assert restored[0].id == row.id
    assert restored[0].status == "active"
