"""上岗提醒：扫码上岗即通知本工作点值班长（谁/何时/哪个工作点/什么岗）。"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Employee, Furnace, FurnaceChief, NotificationRecord
from app.services.on_duty_service import notify_on_duty


def _set_sole_chief(db, furnace, emp_id):
    db.query(FurnaceChief).filter(FurnaceChief.furnace_id == furnace.id).delete()
    furnace.chief_user_id = None
    if emp_id:
        db.add(FurnaceChief(furnace_id=furnace.id, emp_id=emp_id))
    db.commit()


def _notifs(db, target):
    return db.scalars(
        select(NotificationRecord).where(NotificationRecord.target == target)
    ).all()


def test_on_duty_notifies_chief(db_session):
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "L0001")
    db_session.add(Employee(emp_id="W9001", name="张三", role="worker"))
    db_session.commit()

    before = len(_notifs(db_session, "L0001"))
    r = notify_on_duty(
        db_session,
        furnace_no="35号工作点",
        region="示例厂区A",
        date="2026-04-12",
        emp_id="W9001",
        post="出料工",
        squad="3班",
        scan_time=datetime(2026, 4, 12, 1, 5, tzinfo=timezone.utc),  # 上海 09:05
    )
    assert r["notified"] is True
    assert r["targets"] == ["L0001"]

    rows = _notifs(db_session, "L0001")
    assert len(rows) == before + 1
    payload = rows[-1].payload
    # 内容含：姓名、岗位、工作点编号、班次、上海时刻、整班
    for token in ("张三", "出料工", "35号工作点", "3班", "09:05", "上岗", "on_duty", "整班"):
        assert token in payload, token


def test_on_duty_half_shift_emphasized(db_session):
    """半班(0.5)上岗：主题/正文重点标注 ⚠️ 半班。"""
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "L0001")
    db_session.add(Employee(emp_id="W9002", name="李四", role="worker"))
    db_session.commit()

    notify_on_duty(
        db_session,
        furnace_no="35号工作点",
        region="示例厂区A",
        date="2026-04-12",
        emp_id="W9002",
        post="出料工",
        squad="3班",
        work_hours=4.0,
    )
    payload = _notifs(db_session, "L0001")[-1].payload
    for token in ("半班", "⚠️", "0.5"):
        assert token in payload, token
    assert "整班" not in payload


def test_on_duty_skips_self_when_chief_scans(db_session):
    """值班长给自己扫码上岗时不重复提醒自己。"""
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, "L0001")
    r = notify_on_duty(
        db_session,
        furnace_no="35号工作点",
        region="示例厂区A",
        date="2026-04-12",
        emp_id="L0001",
        post="值班长",
    )
    assert r["notified"] is False


def test_on_duty_no_chief_no_send(db_session):
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    _set_sole_chief(db_session, f35, None)
    r = notify_on_duty(
        db_session,
        furnace_no="35号工作点",
        region="示例厂区A",
        date="2026-04-12",
        emp_id="W9001",
        post="出料工",
        squad="1班",
    )
    assert r["notified"] is False
