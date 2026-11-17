from datetime import datetime, timezone

from app.models import AttendanceRecord, Furnace, LaoqinPunch, QuotaRule
from app.services.attendance_service import get_attendance, last_scan_context
from app.services.recon_service import get_recon_for
from app.services.selectable_posts import get_selectable_posts


def test_chief_attendance_and_recon(db_session):
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    if not db_session.query(QuotaRule).filter_by(furnace_id=f.id, post="值班长").first():
        db_session.add(QuotaRule(furnace_id=f.id, squad=None, post="值班长", capacity=1))
    day = "2026-04-10"
    db_session.add(
        LaoqinPunch(
            emp_id="L0001",
            punch_time=datetime.fromisoformat(f"{day}T08:00:00+08:00"),
            inout_type="进",
            natual_date=day,
        )
    )
    db_session.add(
        AttendanceRecord(
            emp_id="L0001",
            furnace_id=f.id,
            squad=None,
            post="值班长",
            employ_type="正式",
            scan_time=datetime.fromisoformat(f"{day}T08:30:00+08:00"),
            source="扫码",
            natual_date=day,
        )
    )
    db_session.commit()

    posts = get_selectable_posts(db_session, region=f.region, furnace=f.furnace_no)
    assert "值班长" not in posts

    att = get_attendance(db_session, userid="L0001", month="2026-04")
    statuses = {d["status"] for d in att["days"]}
    assert "正常" in statuses

    assert get_recon_for(db_session, "L0001", day)["status"] in ("一致", "工作点有劳勤无", "劳勤有工作点无")


def test_last_scan_context_no_hardcode(db_session):
    assert last_scan_context(db_session, "T0005") is None
