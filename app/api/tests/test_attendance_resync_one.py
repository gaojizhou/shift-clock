"""单人定点重拉接口 /admin/dingtalk-attendance/resync-one：
目标日取考勤结果、D±1 取打卡证据，且仅工作点当天有效员工可重拉。"""
from datetime import datetime
from unittest.mock import patch

from app.models import AttendanceRecord, Employee, EmployeeFavorite, Furnace, Region
from app.services import dingtalk_attendance_service as svc


def _patches(calls):
    def fake_sync(db, dfrom, dto, codes=None):
        calls["sync"] = (dfrom, dto, codes)
        return {"fetched": 4, "upserted": 4}

    def fake_result(db, dfrom, dto, progress_cb=None, codes=None):
        calls["result"] = (dfrom, dto, codes)
        return {"dateFrom": dfrom, "dateTo": dto, "dates": [dfrom], "synced": {"fetched": 1, "upserted": 1}}

    def fake_transform(db, dfrom, dto, progress_cb=None, codes=None):
        calls["transform"] = (dfrom, dto, codes)
        return {"inserted": 2, "matched": 1}

    return (patch.object(svc, "sync_range", fake_sync),
            patch.object(svc, "pull_attendance_result_range", fake_result),
            patch.object(svc, "transform_range", fake_transform))


def _attendance(db, emp_id: str, date: str):
    if not db.query(Employee).filter_by(emp_id=emp_id).first():
        db.add(Employee(emp_id=emp_id, name=emp_id, deleted=False))
    furnace = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db.add(AttendanceRecord(
        emp_id=emp_id,
        furnace_id=furnace.id,
        squad="早班",
        post="出料工",
        employ_type="正式",
        scan_time=datetime.fromisoformat(f"{date}T08:00:00"),
        source="扫码",
        status="active",
        natual_date=date,
    ))
    db.commit()


def test_resync_one_window_and_code_filter(client, db_session):
    _attendance(db_session, "CS0001", "2026-05-10")
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        r = client.post(
            "/api/v1/admin/dingtalk-attendance/resync-one",
            json={"empId": "CS0001", "date": "2026-05-10"},
            headers={"x-test-user": "H0001", "x-test-role": "hr"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # 响应说明打卡证据覆盖 D±1。
    assert body["dateFrom"] == "2026-05-09"
    assert body["dateTo"] == "2026-05-11"
    # 只拉这一个工号
    assert calls["sync"] == ("2026-05-10", "2026-05-10", ["CS0001"])
    assert calls["result"] == ("2026-05-10", "2026-05-10", ["CS0001"])
    assert calls["transform"] == ("2026-05-10", "2026-05-10", ["CS0001"])
    assert body["synced"]["upserted"] == 4
    assert body["attendanceResult"]["synced"]["upserted"] == 1


def test_resync_one_stat_allowed(client, db_session):
    _attendance(db_session, "CS0002", "2026-05-10")
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        r = client.post(
            "/api/v1/admin/dingtalk-attendance/resync-one",
            json={"empId": "CS0002", "date": "2026-05-10"},
            headers={"x-test-user": "S0001", "x-test-role": "stat"},
        )
    assert r.status_code == 200, r.text


def test_resync_one_without_furnace_record_is_rejected(client, db_session):
    r = client.post(
        "/api/v1/admin/dingtalk-attendance/resync-one",
        json={"empId": "CS0099", "date": "2026-05-10"},
        headers={"x-test-user": "H0001", "x-test-role": "hr"},
    )
    assert r.status_code == 409
    assert "没有有效或待审工作点记录" in r.json()["detail"]


def test_resync_one_worker_forbidden(client, db_session):
    r = client.post(
        "/api/v1/admin/dingtalk-attendance/resync-one",
        json={"empId": "CS0003", "date": "2026-05-10"},
        headers={"x-test-user": "CS0003", "x-test-role": "worker"},
    )
    assert r.status_code == 403


def test_resync_one_bad_date(client, db_session):
    r = client.post(
        "/api/v1/admin/dingtalk-attendance/resync-one",
        json={"empId": "CS0001", "date": "not-a-date"},
        headers={"x-test-user": "H0001", "x-test-role": "hr"},
    )
    assert r.status_code == 400


def test_resync_one_uses_attendance_furnace_region_for_permission(
    client, db_session
):
    _attendance(db_session, "CS_REGION", "2026-05-10")
    db_session.add_all([
        Region(code="RESYNC_LOCAL", name="重拉甲地区", sort=140),
        Employee(emp_id="RESYNC_HR", name="重拉人事", role="hr"),
        EmployeeFavorite(
            emp_id="RESYNC_HR", kind="region", value="重拉甲地区"
        ),
        EmployeeFavorite(
            emp_id="CS_REGION", kind="region", value="重拉甲地区"
        ),
    ])
    db_session.commit()

    response = client.post(
        "/api/v1/admin/dingtalk-attendance/resync-one",
        json={"empId": "CS_REGION", "date": "2026-05-10"},
        headers={"x-test-user": "RESYNC_HR", "x-test-role": "hr"},
    )
    assert response.status_code == 403
    assert "其他地区工作点" in response.json()["detail"]


def test_resync_one_obeys_attendance_furnace_month_lock(client, db_session):
    from app.services.monthly_export_lock_service import set_month_locks

    _attendance(db_session, "CS_LOCKED", "2026-05-10")
    furnace = db_session.query(Furnace).filter_by(furnace_no="35号工作点").one()
    set_month_locks(
        db_session,
        month="2026-05",
        furnace_ids=[furnace.id],
        locked=True,
        actor="region-hr",
    )

    response = client.post(
        "/api/v1/admin/dingtalk-attendance/resync-one",
        json={"empId": "CS_LOCKED", "date": "2026-05-10"},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert response.status_code == 409
    assert "1个工作点" in response.json()["detail"]


def test_resync_one_blocked_when_any_same_day_furnace_is_locked(client, db_session):
    from app.services.monthly_export_lock_service import set_month_locks

    local = db_session.query(Furnace).filter_by(furnace_no="35号工作点").one()
    remote = Furnace(region="外地地区", furnace_no="外地1号工作点")
    db_session.add_all([
        Region(code="REMOTE_R", name="外地地区", sort=199),
        remote,
        Employee(emp_id="CS_CROSS", name="跨工作点员工", deleted=False),
    ])
    db_session.flush()
    for furnace, hour in ((remote, 8), (local, 18)):
        db_session.add(AttendanceRecord(
            emp_id="CS_CROSS",
            furnace_id=furnace.id,
            squad="早班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.fromisoformat(f"2026-05-10T{hour:02d}:00:00"),
            source="扫码",
            status="active",
            natual_date="2026-05-10",
        ))
    db_session.commit()
    set_month_locks(
        db_session, month="2026-05", furnace_ids=[remote.id], locked=True, actor="admin"
    )
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3:
        response = client.post(
            "/api/v1/admin/dingtalk-attendance/resync-one",
            json={"empId": "CS_CROSS", "date": "2026-05-10"},
            headers={"x-test-user": "admin", "x-test-role": "admin"},
        )
    assert response.status_code == 409, response.text
    assert "calls" not in calls or not calls
