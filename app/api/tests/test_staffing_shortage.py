"""C3: 缺编实时检测 + 推送值班长 + confirm 返回缺编区块。"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AttendanceRecord, Furnace, FurnaceChief, NotificationRecord, QuotaRule
from app.services.staffing_service import compute_shortage, detect_and_notify_shortage


def _set_sole_chief(db, furnace, emp_id):
    """把某工作点的值班长设为唯一 emp_id（清掉 conftest 预置的 L0001 绑定）；emp_id=None 则无值班长。
    缺编通知收件人按 furnace_chief 多值班长表推导，故测试须经该表设定。"""
    db.query(FurnaceChief).filter(FurnaceChief.furnace_id == furnace.id).delete()
    furnace.chief_user_id = None
    if emp_id:
        db.add(FurnaceChief(furnace_id=furnace.id, emp_id=emp_id))
    db.commit()


def _add_scan(db, emp, furnace_id, day, post="出料工", squad="3班", fraction=1.0):
    db.add(
        AttendanceRecord(
            emp_id=emp,
            furnace_id=furnace_id,
            squad=squad,
            post=post,
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
            work_hours=fraction * 8,
        )
    )
    db.commit()


def test_compute_shortage_capacity_minus_actual(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    # 出料工 capacity=2，实到 1 → 缺 1
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")
    rows = compute_shortage(db_session, furnace_no="35号工作点", date=day)
    chu = next(r for r in rows if r["post"] == "出料工" and r["squad"] == "3班")
    assert chu["capacity"] == 2
    assert chu["actual"] == 1.0
    assert chu["shortage"] == 1.0


def test_half_day_counts_toward_actual(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班", fraction=1.0)
    _add_scan(db_session, "T0003", f35.id, day, post="出料工", squad="3班", fraction=0.5)
    rows = compute_shortage(db_session, furnace_no="35号工作点", date=day)
    chu = next(r for r in rows if r["post"] == "出料工" and r["squad"] == "3班")
    assert chu["actual"] == 1.5
    assert chu["shortage"] == 0.5


def test_full_staffing_no_shortage(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _add_scan(db_session, "T0001", f35.id, day, post="供气工", squad="3班")  # capacity 1
    rows = compute_shortage(db_session, furnace_no="35号工作点", date=day)
    assert all(r["post"] != "供气工" for r in rows)


def test_detect_and_notify_creates_one_record(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "chief35")
    db_session.commit()
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")

    r = detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)
    assert r["notified"] is True
    notes = db_session.scalars(
        select(NotificationRecord).where(NotificationRecord.target == "chief35")
    ).all()
    assert len(notes) == 1

    # 同工作点同日缺编明细未变 → 不二次推送
    r2 = detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)
    assert r2["notified"] is False
    notes2 = db_session.scalars(
        select(NotificationRecord).where(NotificationRecord.target == "chief35")
    ).all()
    assert len(notes2) == 1


def test_shortage_change_repushes(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "chief35")
    db_session.commit()
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")
    detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)
    assert (
        len(
            db_session.scalars(
                select(NotificationRecord).where(NotificationRecord.target == "chief35")
            ).all()
        )
        == 1
    )

    # 重复调用：缺编明细未变 → 不再推
    assert detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)["notified"] is False

    # 补满供气工：缺编明细变化（供气工从缺编中消失）→ 重新推送
    _add_scan(db_session, "T0003", f35.id, day, post="供气工", squad="3班")
    r = detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)
    assert r["notified"] is True
    assert (
        len(
            db_session.scalars(
                select(NotificationRecord).where(NotificationRecord.target == "chief35")
            ).all()
        )
        == 2
    )


def test_no_chief_no_notify(db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, None)
    db_session.commit()
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")
    r = detect_and_notify_shortage(db_session, furnace_no="35号工作点", date=day)
    assert r["notified"] is False
    assert r["shortage"]  # 仍能算出缺编


def test_scan_endpoint_triggers_on_duty_notification(client, db_session):
    """扫码上岗即给本工作点值班长推一条「上岗提醒」（不再是缺编提醒）。"""
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "chief35")
    db_session.commit()

    r = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0001",
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "squad": "3班",
            "post": "出料工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r.status_code == 201
    notes = db_session.scalars(
        select(NotificationRecord).where(NotificationRecord.target == "chief35")
    ).all()
    assert len(notes) == 1
    payload = notes[0].payload
    assert "on_duty" in payload and "上岗" in payload and "出料工" in payload


def test_confirm_returns_shortage_block(client, db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")
    sections = client.get(
        "/api/v1/confirm",
        params={"furnace": "35号工作点", "region": "示例厂区A", "date": day},
    ).json()
    assert "shortage" in sections
    assert any(s["post"] == "出料工" and s["shortage"] == 1.0 for s in sections["shortage"])


def test_staffing_shortage_endpoint(client, db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _add_scan(db_session, "T0001", f35.id, day, post="出料工", squad="3班")
    res = client.get(
        "/api/v1/staffing/shortage",
        params={"furnace": "35号工作点", "region": "示例厂区A", "date": day},
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    ).json()
    assert res["furnace"] == "35号工作点"
    assert any(s["post"] == "出料工" for s in res["shortage"])
