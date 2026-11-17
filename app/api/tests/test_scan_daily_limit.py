"""One active scan per employee per natural day."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Furnace, QuotaRule, AttendanceRecord


def test_second_scan_same_day_rejected(client, db_session):
    day = "2026-04-12"
    f35 = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    f11 = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "11号工作点"
        )
    )
    assert f35 and f11

    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f35.id,
            squad="3班",
            post="供气工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()

    # Same furnace, different post → blocked
    r1 = client.post(
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
    assert r1.status_code == 409
    assert r1.json()["duplicate"] is True

    # Different furnace → blocked
    r2 = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0001",
            "region": "示例厂区A",
            "furnace": "11号工作点",
            "squad": "3班",
            "post": "出料工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r2.status_code == 409
    assert r2.json()["existingFurnace"] == "35号工作点"

    # Different user same day → allowed
    r3 = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0003",
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "squad": "3班",
            "post": "供气工",
            "employType": "实习",
            "natualDate": day,
        },
        headers={"x-test-user": "T0003", "x-test-role": "worker"},
    )
    assert r3.status_code == 201


def test_t0001_scan_does_not_mark_t0003_attendance_normal(client, db_session):
    """Identity isolation: one worker's scan must not affect another's attendance."""
    day = "2026-04-12"
    f35 = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    assert f35

    r = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0001",
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "squad": "3班",
            "post": "供气工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r.status_code == 201

    hdr = {"x-test-user": "hr1", "x-test-role": "hr"}
    t1 = client.get("/api/v1/attendance?userid=T0001&month=2026-04", headers=hdr).json()
    t3 = client.get("/api/v1/attendance?userid=T0003&month=2026-04", headers=hdr).json()
    d1 = next(d for d in t1["days"] if d["date"] == day)
    d3 = next(d for d in t3["days"] if d["date"] == day)
    assert d1["status"] == "正常"
    assert d3["status"] != "正常"


def test_quota_isolated_per_post_after_one_post_filled(client, db_session):
    """Formal fill on one post must not block formal on other posts (same furnace/squad)."""
    day = "2026-04-12"
    f35 = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    assert f35
    db_session.add(QuotaRule(furnace_id=f35.id, squad="3班", post="供气工", capacity=1))
    db_session.add(QuotaRule(furnace_id=f35.id, squad="3班", post="物料转运工", capacity=1))
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f35.id,
            squad="3班",
            post="供气工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()

    hdr = {"x-test-user": "hr1", "x-test-role": "hr"}
    q_full = client.get(
        "/api/v1/quota",
        params={
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "post": "供气工",
            "squad": "3班",
            "date": day,
        },
        headers=hdr,
    ).json()
    q_open = client.get(
        "/api/v1/quota",
        params={
            "region": "示例厂区A",
            "furnace": "35号工作点",
            "post": "物料转运工",
            "squad": "3班",
            "date": day,
        },
        headers=hdr,
    ).json()
    assert q_full["formalUsed"] == 1
    assert q_full["formalAllowed"] is False
    assert "正式" not in q_full["allowedEmployTypes"]
    assert q_open["formalUsed"] == 0
    assert q_open["formalAllowed"] is True
    assert "正式" in q_open["allowedEmployTypes"]
