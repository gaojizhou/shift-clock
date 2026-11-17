"""Regional post alias fallback and live quota post precedence."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Furnace, QuotaRule, AttendanceRecord
from app.services.quota_service import find_quota
from app.services.selectable_posts import get_selectable_posts


def _add_shanshan_f10(db_session):
    f = Furnace(
        region="示例厂区B",
        furnace_no="10号工作点",
        furnace_type_code="FT03",
        name="示例厂区B10号工作点",
    )
    db_session.add(f)
    db_session.flush()
    for post, cap in (("出料工", 2), ("供气工", 1), ("物料工", 3)):
        db_session.add(
            QuotaRule(furnace_id=f.id, squad="1班", post=post, capacity=cap)
        )
    db_session.commit()
    return f


def _add_shanshan_f20(db_session):
    f = Furnace(
        region="示例厂区B",
        furnace_no="20号工作点",
        furnace_type_code="FT03",
        name="示例厂区B20号工作点",
    )
    db_session.add(f)
    db_session.flush()
    for post, cap in (("出料工", 2), ("供气工", 1), ("物料转运工", 3)):
        db_session.add(
            QuotaRule(furnace_id=f.id, squad="1班", post=post, capacity=cap)
        )
    db_session.commit()
    return f


def test_selectable_posts_from_quota_rules(db_session):
    _add_shanshan_f10(db_session)
    posts = get_selectable_posts(db_session, region="示例厂区B", furnace="10号工作点")
    assert set(posts) == {"供气工", "出料工", "物料工"}
    assert "物料转运工" not in posts
    assert "加料操作工" not in posts


def test_scan_as_tuiliao_driver_counts_tuiliao_gong_quota(client, db_session):
    day = "2026-04-12"
    f = _add_shanshan_f10(db_session)

    r1 = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0003",
            "region": "示例厂区B",
            "furnace": "10号工作点",
            "squad": "1班",
            "post": "物料转运工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0003", "x-test-role": "worker"},
    )
    assert r1.status_code == 201

    row = db_session.scalar(
        select(AttendanceRecord).where(AttendanceRecord.emp_id == "T0003", AttendanceRecord.natual_date == day)
    )
    assert row is not None
    assert row.post == "物料工"

    q_ui = client.get(
        "/api/v1/quota",
        params={
            "region": "示例厂区B",
            "furnace": "10号工作点",
            "post": "物料转运工",
            "squad": "1班",
            "date": day,
        },
        headers={"x-test-user": "hr1", "x-test-role": "hr"},
    ).json()
    assert q_ui["formalUsed"] == 1
    assert q_ui["capacity"] == 3

    q_canon = find_quota(
        db_session,
        region="示例厂区B",
        furnace="10号工作点",
        post="物料工",
        squad="1班",
        natual_date=day,
    )
    assert q_canon is not None
    assert q_canon["formalUsed"] == 1

    r2 = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0005",
            "region": "示例厂区B",
            "furnace": "10号工作点",
            "squad": "1班",
            "post": "物料工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0005", "x-test-role": "worker"},
    )
    assert r2.status_code == 201

    # Legacy row stored as alias still counts
    db_session.add(
        AttendanceRecord(
            emp_id="T0009",
            furnace_id=f.id,
            squad="1班",
            post="物料转运工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()

    q_after = find_quota(
        db_session,
        region="示例厂区B",
        furnace="10号工作点",
        post="物料工",
        squad="1班",
        natual_date=day,
    )
    assert q_after["formalUsed"] == 3


def test_live_quota_post_wins_over_legacy_alias(client, db_session):
    day = "2026-04-13"
    f = _add_shanshan_f20(db_session)

    r = client.post(
        "/api/v1/scan",
        json={
            "userid": "T0003",
            "region": "示例厂区B",
            "furnace": "20号工作点",
            "squad": "1班",
            "post": "物料转运工",
            "employType": "正式",
            "natualDate": day,
        },
        headers={"x-test-user": "T0003", "x-test-role": "worker"},
    )
    assert r.status_code == 201

    row = db_session.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == "T0003",
            AttendanceRecord.natual_date == day,
        )
    )
    assert row is not None
    assert row.post == "物料转运工"

    # Historical rows stored with the old quota name still consume the live quota slot.
    db_session.add(
        AttendanceRecord(
            emp_id="T0009",
            furnace_id=f.id,
            squad="1班",
            post="物料工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()

    q = find_quota(
        db_session,
        region="示例厂区B",
        furnace="20号工作点",
        post="物料转运工",
        squad="1班",
        natual_date=day,
    )
    assert q is not None
    assert q["capacity"] == 3
    assert q["formalUsed"] == 2
