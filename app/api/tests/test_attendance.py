from app.models import AttendanceRecord
from app.services.attendance_service import get_attendance


def test_makeup_shows_pending(client, db_session):
    from app.models import Furnace

    f = db_session.query(Furnace).first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            natual_date="2026-04-15",
            post="出料工",
            employ_type="正式",
            source="补录",
            status="待审",
        )
    )
    db_session.commit()

    data = get_attendance(db_session, userid="T0001", month="2026-04")
    day = next(d for d in data["days"] if d["date"] == "2026-04-15")
    assert day["status"] == "待审"


def test_attendance_api(client, db_session):
    r = client.get(
        "/api/v1/attendance",
        params={"userid": "T0001", "month": "2026-04"},
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r.status_code == 200
    assert len(r.json()["days"]) >= 28


def test_attendance_includes_detail(db_session):
    from datetime import datetime, timezone
    from app.models import AttendanceRecord, Furnace

    f = db_session.query(Furnace).first()
    day = "2026-04-03"
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.fromisoformat(f"{day}T08:00:00+08:00"),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()
    data = get_attendance(db_session, userid="T0001", month="2026-04")
    d = next(x for x in data["days"] if x["date"] == day)
    assert d["detail"] is not None
    assert d["detail"]["post"] == "出料工"


def test_day_status_dual_empty_neutral(db_session):
    from app.services.attendance_service import day_status

    assert day_status(db_session, emp_id="T0005", natual_date="2026-04-20") == "无数据"


def test_makeup_approve_proxy_post_squad(client, db_session):
    from app.models import AttendanceRecord, Furnace

    f = db_session.query(Furnace).first()
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    # 代签的岗位+班组须在本工作点有编制（供气工仅 3 班有编制）；否则按越编硬拦拒绝
    r = client.post(
        "/api/v1/makeup/approve",
        json={
            "empId": "T0003",
            "date": "2026-04-10",
            "furnace": f.furnace_no,
            "region": f.region,
            "post": "供气工",
            "squad": "3班",
        },
    )
    assert r.status_code == 200
    row = (
        db_session.query(AttendanceRecord)
        .filter_by(emp_id="T0003", natual_date="2026-04-10", post="供气工", squad="3班")
        .first()
    )
    assert row is not None


def test_nonformal_makeup_keeps_half_shift(client, db_session):
    """实习/待岗补录提交 4h 时不得被静默改回整班。"""
    from app.models import Furnace

    f = db_session.query(Furnace).first()
    cases = [
        ("T0003", "实习", "2026-04-21"),
        ("T0005", "待岗", "2026-04-22"),
    ]
    for emp_id, employ_type, day in cases:
        resp = client.post(
            "/api/v1/makeup",
            json={
                "userid": emp_id,
                "date": day,
                "furnace": f.furnace_no,
                "region": f.region,
                "squad": "3班",
                "post": "出料工",
                "employType": employ_type,
                "reason": "漏扫",
                "workHours": 4.0,
            },
            headers={"x-test-user": emp_id, "x-test-role": "worker"},
        )
        assert resp.status_code == 200

    for emp_id, employ_type, day in cases:
        row = (
            db_session.query(AttendanceRecord)
            .filter_by(emp_id=emp_id, natual_date=day, source="补录")
            .one()
        )
        assert row.employ_type == employ_type
        assert row.work_hours == 4.0


def test_nonformal_proxy_signin_keeps_half_shift(client, db_session):
    """值班长给实习/待岗代签半班时，前后端按 4h 原值保存。"""
    from app.models import Furnace

    f = db_session.query(Furnace).first()
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    cases = [
        ("T0003", "实习", "2026-04-23"),
        ("T0005", "待岗", "2026-04-24"),
    ]
    for emp_id, employ_type, day in cases:
        resp = client.post(
            "/api/v1/makeup/approve",
            json={
                "empId": emp_id,
                "date": day,
                "furnace": f.furnace_no,
                "region": f.region,
                "post": "出料工",
                "squad": "3班",
                "employType": employ_type,
                "workHours": 4.0,
            },
        )
        assert resp.status_code == 200

    for emp_id, employ_type, day in cases:
        row = (
            db_session.query(AttendanceRecord)
            .filter_by(emp_id=emp_id, natual_date=day, source="代签")
            .one()
        )
        assert row.employ_type == employ_type
        assert row.work_hours == 4.0
