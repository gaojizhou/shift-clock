from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch, ReconResult
from app.services.recon_service import (
    _calculate_laoqin_work_hours,
    get_overtime_recon_detail,
    get_recon_for,
    run_recon_for_date,
)

SH = ZoneInfo("Asia/Shanghai")


def _dt(date: str, h: int, m: int = 0) -> datetime:
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=SH)


def _punch(
    emp: str,
    date: str,
    h: int,
    direct: str,
    *,
    minute: int = 0,
    natural_date: str | None = None,
) -> LaoqinPunch:
    return LaoqinPunch(
        emp_id=emp,
        punch_time=_dt(date, h, minute),
        inout_type=direct,
        natual_date=natural_date or date,
    )


def _scan(
    db,
    emp: str,
    date: str,
    *,
    hours: float = 8.0,
    squad: str | None = "2班",
    post: str = "出料工",
) -> None:
    f = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db.add(
        AttendanceRecord(
            emp_id=emp,
            furnace_id=f.id,
            squad=squad,
            post=post,
            employ_type="正式",
            scan_time=_dt(date, 10),
            source="扫码",
            natual_date=date,
            work_hours=hours,
        )
    )


def test_work_hours_recon_uses_attendance_result_not_shift_cover(db_session):
    day = "2026-05-10"
    _scan(db_session, "T0001", day)
    db_session.add(_punch("T0001", day, 8, "进"))
    db_session.add(_punch("T0001", day, 16, "出"))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001", user_name="T0001", kq_date=day,
            status_name="正常", cqgs="8.00000",
        )
    )
    db_session.commit()

    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_half_shift_only_requires_four_hours(db_session):
    day = "2026-05-11"
    _scan(db_session, "T0001", day, hours=4.0)
    db_session.add(_punch("T0001", day, 9, "进"))
    db_session.add(_punch("T0001", day, 13, "出"))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001", user_name="T0001", kq_date=day,
            status_name="正常", cqgs="4.00000",
        )
    )
    db_session.commit()

    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_attendance_result_cqgs_eight_auto_valid_without_punches(db_session):
    day = "2026-05-15"
    _scan(db_session, "T0001", day)
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    recon = get_recon_for(db_session, "T0001", day)
    assert recon["status"] == "自动一致"
    assert recon["laoqinWorkHours"] == 8.0


def test_attendance_result_cqgs_four_auto_valid_for_half_shift(db_session):
    day = "2026-05-16"
    _scan(db_session, "T0001", day, hours=4.0)
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="4.00000",
        )
    )
    db_session.commit()

    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_overtime_requires_manual_check_after_base_shift_matches(db_session):
    day = "2026-05-18"
    _scan(db_session, "T0001", day)
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="1班",
            post="物料转运工",
            employ_type="正式",
            scan_time=_dt(day, 20),
            source="加班",
            natual_date=day,
            record_kind="加班",
            work_hours=4.0,
        )
    )
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    recon = get_recon_for(db_session, "T0001", day)
    assert recon["status"] == "加班需核对"
    assert recon["laoqinWorkHours"] == 8.0

    run_recon_for_date(db_session, day)
    assert get_recon_for(db_session, "T0001", day)["status"] == "加班需核对"


def test_overtime_without_slot_coverage_requires_check_after_base_shift_matches(db_session):
    day = "2026-05-19"
    _scan(db_session, "T0001", day, squad=None, post="值班长")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="2班",
            post="物料转运工",
            employ_type="正式",
            scan_time=_dt(day, 20),
            source="加班",
            natual_date=day,
            record_kind="加班",
            work_hours=4.0,
            shift_slot="中班",
        )
    )
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    recon = get_recon_for(db_session, "T0001", day)
    assert recon["status"] == "加班需核对"
    assert recon["laoqinWorkHours"] == 8.0

    run_recon_for_date(db_session, day)
    assert get_recon_for(db_session, "T0001", day)["status"] == "加班需核对"


def test_cross_squad_overtime_auto_valid_when_strict_punches_cover_overtime_shift(db_session):
    day = "2026-05-20"
    _scan(db_session, "T0001", day, squad="2班", post="物料转运工")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add(
        AttendanceRecord(
            emp_id="T0001",
            furnace_id=f.id,
            squad="1班",
            post="出料工",
            employ_type="正式",
            scan_time=_dt(day, 20),
            source="加班",
            natual_date=day,
            record_kind="加班",
            work_hours=8.0,
            shift_slot="晚班",
        )
    )
    db_session.add(_punch("T0001", day, 2, "进", natural_date=day))
    db_session.add(_punch("T0001", day, 10, "出", natural_date=day))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    recon = get_recon_for(db_session, "T0001", day)
    assert recon["status"] == "自动一致"
    assert recon["laoqinWorkHours"] == 8.0

    run_recon_for_date(db_session, day)
    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_partial_overtime_checks_claimed_hours_from_shift_start(db_session):
    """中班申报4h只需覆盖18:00-22:00，不再错误要求覆盖完整中班至次日02:00。"""
    day = "2026-05-20"
    _scan(db_session, "T0001", day, squad="3班", post="副班组长")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    overtime = AttendanceRecord(
        emp_id="T0001",
        furnace_id=f.id,
        squad="1班",
        post="副班组长",
        employ_type="正式",
        scan_time=_dt(day, 20),
        source="加班",
        natual_date=day,
        record_kind="加班",
        work_hours=4.0,
        shift_slot="中班",
    )
    db_session.add(overtime)
    db_session.add(_punch("T0001", day, 9, "进", minute=42, natural_date=day))
    db_session.add(_punch("T0001", day, 22, "出", natural_date=day))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    detail = get_overtime_recon_detail(db_session, overtime)
    assert detail["covered"] is True
    assert detail["shiftStart"].startswith("2026-05-20T18:00:00")
    assert detail["shiftEnd"].startswith("2026-05-20T22:00:00")
    assert detail["windowStart"].startswith("2026-05-20T06:00:00")
    assert detail["windowEnd"].startswith("2026-05-21T10:00:00")
    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_overtime_uses_shift_start_and_end_padded_by_twelve_hours(db_session):
    """晚班 02:00-10:00 的取卡窗口应为前日 14:00 至当日 22:00。"""
    day = "2026-05-21"
    _scan(db_session, "T0001", day, squad="2班", post="物料转运工")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    overtime = AttendanceRecord(
        emp_id="T0001",
        furnace_id=f.id,
        squad="1班",
        post="出料工",
        employ_type="正式",
        scan_time=_dt(day, 20),
        source="加班",
        natual_date=day,
        record_kind="加班",
        work_hours=8.0,
        shift_slot="晚班",
    )
    db_session.add(overtime)
    # 前日 18:00 早于旧的固定窗口（前日 20:00），但处于晚班开始前 12 小时范围内。
    db_session.add(
        _punch("T0001", "2026-05-20", 18, "进", natural_date="2026-05-20")
    )
    db_session.add(_punch("T0001", day, 10, "出", natural_date=day))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    detail = get_overtime_recon_detail(db_session, overtime)
    assert detail["covered"] is True
    assert detail["windowStart"].startswith("2026-05-20T14:00:00")
    assert detail["shiftStart"].startswith("2026-05-21T02:00:00")
    assert detail["shiftEnd"].startswith("2026-05-21T10:00:00")
    assert detail["windowEnd"].startswith("2026-05-21T22:00:00")
    assert [p["inoutType"] for p in detail["punches"]] == ["进", "出"]
    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"

    run_recon_for_date(db_session, day)
    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_overtime_marks_best_incomplete_pair_among_all_window_punches(db_session):
    """未完整覆盖时仍返回窗口内全部卡，并标记与申报区间最贴近的计算卡对。"""
    day = "2026-05-23"
    _scan(db_session, "T0001", day, squad="3班", post="副班组长")
    f = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    overtime = AttendanceRecord(
        emp_id="T0001",
        furnace_id=f.id,
        squad="1班",
        post="副班组长",
        employ_type="正式",
        scan_time=_dt(day, 20),
        source="加班",
        natual_date=day,
        record_kind="加班",
        work_hours=4.0,
        shift_slot="中班",
    )
    db_session.add(overtime)
    db_session.add_all([
        _punch("T0001", day, 10, "进", natural_date=day),
        _punch("T0001", day, 12, "出", natural_date=day),
        _punch("T0001", day, 17, "进", natural_date=day),
        _punch("T0001", day, 20, "出", natural_date=day),
        _punch("T0001", day, 21, "进", natural_date=day),
        _punch("T0001", day, 23, "出", natural_date=day),
    ])
    db_session.commit()

    detail = get_overtime_recon_detail(db_session, overtime)

    assert detail["covered"] is False
    assert len(detail["punches"]) == 6
    selected = [p for p in detail["punches"] if p["selectedForCalculation"]]
    assert [p["calculationRole"] for p in selected] == ["start", "end"]
    assert selected[0]["time"].startswith("2026-05-23T17:00:00")
    assert selected[1]["time"].startswith("2026-05-23T20:00:00")


def test_adjacent_day_punch_alone_does_not_create_recon_result(db_session):
    day = "2026-05-22"
    db_session.add(
        _punch("T0003", "2026-05-21", 18, "进", natural_date="2026-05-21")
    )
    db_session.commit()

    run_recon_for_date(db_session, day)

    assert (
        db_session.query(ReconResult).filter_by(emp_id="T0003", date=day).first()
        is None
    )


def test_run_recon_for_date_uses_attendance_result_cqgs(db_session):
    day = "2026-05-17"
    _scan(db_session, "T0001", day)
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        )
    )
    db_session.commit()

    run_recon_for_date(db_session, day)
    assert get_recon_for(db_session, "T0001", day)["status"] == "自动一致"


def test_punch_pairing_prefers_in_then_out_after_leading_out():
    day = "2026-05-12"
    punches = [
        _punch("T0001", day, 0, "出"),
        _punch("T0001", day, 8, "进"),
        _punch("T0001", day, 16, "出"),
    ]

    assert _calculate_laoqin_work_hours(punches, date=day) == 8


def test_unknown_start_pairs_with_nearest_out():
    day = "2026-05-13"
    punches = [
        _punch("T0001", day, 8, "自动判断"),
        _punch("T0001", day, 16, "出"),
    ]

    assert _calculate_laoqin_work_hours(punches, date=day) == 8


def test_missing_attendance_result_hours_does_not_fall_back_to_punch_gap(db_session):
    day = "2026-05-14"
    _scan(db_session, "T0001", day)
    db_session.add(_punch("T0001", "2026-05-13", 20, "进", natural_date="2026-05-13"))
    db_session.add(_punch("T0001", "2026-05-15", 4, "出", natural_date="2026-05-15"))
    db_session.add(
        ExternalAttendanceResultRaw(
            user_code="T0001", user_name="T0001", kq_date=day,
            status_name="正常", cqgs=None,
        )
    )
    db_session.commit()

    run_recon_for_date(db_session, day)
    recon = get_recon_for(db_session, "T0001", day)
    assert recon["laoqinWorkHours"] is None
    assert recon["status"] == "工作点有劳勤无"
