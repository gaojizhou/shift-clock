from datetime import datetime, timezone

from app.models import Furnace, AttendanceRecord
from app.services.quota_service import find_quota


def _add_formal(db_session, f, emp_id: str):
    db_session.add(
        AttendanceRecord(
            emp_id=emp_id,
            furnace_id=f.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date="2026-04-10",
        )
    )
    db_session.commit()


def test_formal_allowed_sequence(db_session):
    f = db_session.query(Furnace).first()
    q0 = find_quota(
        db_session,
        region="示例厂区A",
        furnace="35号工作点",
        post="出料工",
        squad="3班",
        natual_date="2026-04-10",
    )
    assert q0["formalAllowed"] is True

    _add_formal(db_session, f, "T0001")
    q1 = find_quota(
        db_session,
        region="示例厂区A",
        furnace="35号工作点",
        post="出料工",
        squad="3班",
        natual_date="2026-04-10",
    )
    assert q1["formalAllowed"] is True
    assert q1["formalUsed"] == 1

    _add_formal(db_session, f, "T0003")
    q2 = find_quota(
        db_session,
        region="示例厂区A",
        furnace="35号工作点",
        post="出料工",
        squad="3班",
        natual_date="2026-04-10",
    )
    assert q2["formalAllowed"] is False
    assert "正式" not in q2["allowedEmployTypes"]
    assert q2["formalUsed"] == 2


def test_quota_11_furnace_with_squad(db_session):
    q = find_quota(
        db_session,
        region="示例厂区A",
        furnace="11号工作点",
        post="出料工",
        squad="3班",
        natual_date="2026-04-10",
    )
    assert q is not None
    assert q["capacity"] >= 1
    assert q["squadless"] is False


def test_seed_formal_not_over_capacity(db_session):
    """U-04: after two formal scans, third must not count as formal over cap in quota view."""
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    _add_formal(db_session, f, "T0001")
    _add_formal(db_session, f, "T0003")
    q = find_quota(
        db_session,
        region="示例厂区A",
        furnace="35号工作点",
        post="出料工",
        squad="3班",
        natual_date="2026-04-10",
    )
    assert q["formalUsed"] <= q["capacity"]
