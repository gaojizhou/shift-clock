"""不分班岗（squad 空）对账特例：钉钉在岗时长满4h即自动通过（端到端）。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch, ReconResult
from app.services.recon_service import run_recon_for_date

_SH = ZoneInfo("Asia/Shanghai")


def _scan(db, emp, date, post, squad, hours=8.0):
    f = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    y, m, d = (int(x) for x in date.split("-"))
    db.add(AttendanceRecord(
        emp_id=emp, furnace_id=f.id, squad=squad, post=post, employ_type="正式",
        work_hours=hours, scan_time=datetime(y, m, d, 12, tzinfo=_SH),
        source="扫码", natual_date=date,
    ))


def _punch(db, emp, date, h, mi, direct="进"):
    y, m, d = (int(x) for x in date.split("-"))
    db.add(LaoqinPunch(emp_id=emp, punch_time=datetime(y, m, d, h, mi, tzinfo=_SH),
                       inout_type=direct, natual_date=date))


def _result(db, emp, date, hours):
    db.add(ExternalAttendanceResultRaw(
        user_code=emp, user_name=emp, kq_date=date,
        status_name="正常", cqgs=f"{hours:.5f}",
    ))


def _status(db, emp, date):
    return db.query(ReconResult).filter_by(emp_id=emp, date=date).first().status


def test_squadless_half_shift_4h_auto_pass(db_session):
    db = db_session
    day = "2026-05-20"
    # 不分班维修工(squad=None)，劳勤结果工时 4.5h，满足半班 4h。
    _scan(db, "T0001", day, "维修工", None, hours=4.0)
    _punch(db, "T0001", day, 9, 0)
    _punch(db, "T0001", day, 13, 30, "出")
    _result(db, "T0001", day, 4.5)
    db.commit()
    run_recon_for_date(db, day)
    assert _status(db, "T0001", day) == "自动一致"


def test_squadless_under_4h_not_auto(db_session):
    db = db_session
    day = "2026-05-21"
    # 不分班，劳勤结果工时 3h < 4h → 时间错位（需人工确认）
    _scan(db, "T0005", day, "维修工", None, hours=4.0)
    _punch(db, "T0005", day, 9, 0)
    _punch(db, "T0005", day, 12, 0, "出")
    _result(db, "T0005", day, 3.0)
    db.commit()
    run_recon_for_date(db, day)
    assert _status(db, "T0005", day) == "时间错位"


def test_squadded_4h_still_needs_cover(db_session):
    db = db_session
    day = "2026-05-22"
    # 分班岗整班要求 8h，劳勤结果工时 5h 不足 → 时间错位
    _scan(db, "T0003", day, "出料工", "3班")
    _punch(db, "T0003", day, 9, 0)
    _punch(db, "T0003", day, 14, 0, "出")
    _result(db, "T0003", day, 5.0)
    db.commit()
    run_recon_for_date(db, day)
    assert _status(db, "T0003", day) == "时间错位"
