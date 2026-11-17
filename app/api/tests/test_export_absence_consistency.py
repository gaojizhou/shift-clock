"""三张月度表均取原始旷工小时数，补充行与只有加班的员工也使用同一口径。"""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from app.models import (
    AttendanceRecord,
    Employee,
    Furnace,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
)
from app.services.anomaly_service import set_confirm
from app.services.export_service import build_export_meta, build_export_xlsx
from app.services.recon_service import run_recon_for_date

_SH = ZoneInfo("Asia/Shanghai")


def _dt(date: str, hour: int, minute: int = 0) -> datetime:
    y, m, d = (int(x) for x in date.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=_SH)


def _valid_half_day_attendance(db, emp_id: str, date: str):
    """范围外工作点（11号工作点）一条有效半班出勤：打卡覆盖 + 考勤结果 4h。"""
    furnace = db.query(Furnace).filter_by(furnace_no="11号工作点").first()
    db.add(AttendanceRecord(
        emp_id=emp_id, furnace_id=furnace.id, squad="1班", post="出料工",
        employ_type="正式", work_hours=4.0, scan_time=_dt(date, 8),
        source="扫码", status="active", natual_date=date, record_kind="出勤",
    ))
    for hour, minute, direct in ((1, 50, "进"), (10, 10, "出")):
        db.add(LaoqinPunch(
            emp_id=emp_id, punch_time=_dt(date, hour, minute),
            inout_type=direct, natual_date=date,
        ))
    db.add(ExternalAttendanceResultRaw(
        user_code=emp_id, user_name=emp_id, kq_date=date,
        status_name="正常", cqgs="4.00000",
    ))
    db.commit()


def test_single_employee_supplement_row_matches_payroll_absence(db_session):
    db_session.add(Employee(emp_id="T0501", name="补充行员工", role="worker"))
    db_session.commit()
    # 范围外出勤也参与逐日裁剪；全天请假的日期不再重复计旷工。
    _valid_half_day_attendance(db_session, "T0501", "2026-05-06")
    _valid_half_day_attendance(db_session, "T0501", "2026-05-07")
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0501", user_name="T0501", kq_date="2026-05-08",
        status_name="请假", leave_03="8.00000", kggs="10.04000",
    ))
    db_session.commit()
    run_recon_for_date(db_session, "2026-05-06")
    run_recon_for_date(db_session, "2026-05-07")

    furnace_35 = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    meta = build_export_meta(
        db_session,
        month="2026-05",
        furnaces=["35号工作点"],
        furnace_ids=[furnace_35.id],
        emp_id="T0501",
        persist=False,
    )

    payroll_rows = [
        row for row in meta["payrollSheet"]["rows"] if row["工号"] == "T0501"
    ]
    assert len(payroll_rows) == 1
    payroll_absence = payroll_rows[0]["旷工天数"]
    # 源旷工虽然有 10.04h，但同日请假已占满 8h。
    assert payroll_absence == 0

    days = meta["monthlyHours"]["daysInMonth"]
    supplement_rows = [
        row for row in meta["monthlyHours"]["rows"]
        if str(row[4]) == "T0501"
    ]
    assert len(supplement_rows) == 1
    supplement_absence = supplement_rows[0][6 + days + 3]
    # 应出勤列保持原口径，旷工列使用原始小时数。
    assert supplement_rows[0][6 + days] == 27
    assert supplement_absence == payroll_absence == 0

    full_meta = build_export_meta(
        db_session,
        month="2026-05",
        furnaces=["35号工作点"],
        furnace_ids=[furnace_35.id],
        emp_id=None,
        persist=False,
    )
    full_payroll = [
        row for row in full_meta["payrollSheet"]["rows"] if row["工号"] == "T0501"
    ]
    if full_payroll:
        full_hours = [
            row for row in full_meta["monthlyHours"]["rows"]
            if str(row[4]) == "T0501"
        ]
        assert full_hours
        assert full_hours[0][6 + days + 3] == full_payroll[0]["旷工天数"]


def test_overtime_only_employee_absence_consistent_across_sheets(db_session):
    db_session.add(Employee(emp_id="T0502", name="加班员工", role="worker"))
    furnace = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db_session.add(AttendanceRecord(
        emp_id="T0502", furnace_id=furnace.id, squad="3班", post="供气工",
        employ_type="正式", work_hours=3.0, scan_time=_dt("2026-05-08", 12),
        source="加班", status="active", natual_date="2026-05-08",
        record_kind="加班", reason="岗位缺人",
    ))
    db_session.commit()
    run_recon_for_date(db_session, "2026-05-08")
    set_confirm(db_session, emp_id="T0502", date="2026-05-08", kind="hr", status="通过", actor="HR01")

    meta = build_export_meta(
        db_session,
        month="2026-05",
        furnaces=["35号工作点"],
        furnace_ids=[furnace.id],
        emp_id=None,
        persist=False,
    )

    payroll_rows = [
        row for row in meta["payrollSheet"]["rows"] if row["工号"] == "T0502"
    ]
    assert len(payroll_rows) == 1
    payroll_absence = payroll_rows[0]["旷工天数"]
    # 主工作点未确定且没有原始旷工，不推算满月旷工
    assert payroll_absence == 0

    days = meta["monthlyHours"]["daysInMonth"]
    calendar_rows = [
        row for row in meta["monthlyHours"]["rows"]
        if str(row[4]) == "T0502"
    ]
    assert len(calendar_rows) == 1
    calendar_absence = calendar_rows[0][6 + days + 3]
    # 班次有效工时不得与薪资表矛盾（旧实现此处恒为 0）
    assert calendar_absence == payroll_absence


@pytest.mark.parametrize(("hours", "expected"), [
    (["4", "6"], 1.25),
    (["0.02", "0.02"], 0.01),  # 先合计再四舍五入，不能按天取整
    (["10.04"], 1),  # 单日最多 8h
    (["8", "2.04"], 1.26),  # 两日合计后四舍五入
    (["8"], 1),
    ([None, "", "0", " ", "bad", "NaN", "Infinity", "-8"], 0),
])
def test_raw_absence_hours_month_scope_rounding_and_excel(db_session, hours, expected):
    """没有扫码或有效出勤时也能导出源旷工，且不串月、不串员工。"""
    for day, value in enumerate(hours, 1):
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001", kq_date=f"2026-05-{day:02d}", kggs=value,
        ))
    for employee_id, day in [
        ("T0001", "2026-04-30"), ("T0001", "2026-06-01"),
        ("T0002", "2026-05-01"),
    ]:
        db_session.add(ExternalAttendanceResultRaw(
            user_code=employee_id, kq_date=day, kggs="80",
        ))
    db_session.commit()
    db_session.expire_all()  # 实际走快照的 load_only 字段读取
    meta = build_export_meta(
        db_session, month="2026-05", furnaces=["35号工作点"], emp_id="T0001",
        payroll2_include_zero_rows=True, persist=False,
    )
    assert meta["payrollSheet2"]["rows"][0]["旷工天数"] == expected
    if expected:
        assert meta["payrollSheet"]["rows"][0]["旷工天数"] == expected
        assert meta["monthlyHours"]["rows"][0][-2] == expected
        workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
        for name in ("输出给薪资", "输出给薪资2", "班次有效工时"):
            sheet = workbook[name]
            headers = [cell.value for cell in sheet[1]]
            assert sheet.cell(2, headers.index("旷工天数") + 1).value == expected
    else:
        assert meta["payrollSheet"]["rows"] == []
        assert meta["monthlyHours"]["rows"] == []


@pytest.mark.parametrize(("work_hours", "leave_hours", "raw_absence", "expected"), [
    (8, 0, "8", 0),       # HR 确认满班通过，源旷工不得再扣一天
    (4, 2, "8", 0.25),    # 半班出勤 + 2h 请假，仅余 2h 旷工
    (4, 8, "8", 0),       # 请假先裁至剩余 4h，旷工归零
    (8, 8, "8", 0),       # 三种原始数据同日冲突时仍不重复扣旷工
])
def test_confirmed_attendance_and_leave_cap_absence_in_every_sheet(
    db_session, work_hours, leave_hours, raw_absence, expected,
):
    from app.services.export_service import build_monthly_calendar

    employee_id, day = "T0503", "2026-05-09"
    db_session.add(Employee(emp_id=employee_id, name="人工确认员工", role="worker"))
    furnace = db_session.query(Furnace).filter_by(furnace_no="35号工作点").one()
    # 故意没有劳勤打卡，验证人工判通过也会减少可计旷工的小时数。
    db_session.add(AttendanceRecord(
        emp_id=employee_id, furnace_id=furnace.id, squad="1班", post="出料工",
        employ_type="正式", work_hours=work_hours, source="补录", status="active",
        natual_date=day, record_kind="出勤", scan_time=_dt(day, 8),
    ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code=employee_id, kq_date=day, cqgs="0", kggs=raw_absence,
        leave_03=str(leave_hours), status_name="旷工",
    ))
    db_session.commit()
    run_recon_for_date(db_session, day)
    set_confirm(db_session, emp_id=employee_id, date=day, kind="hr", status="通过", actor="HR01")
    meta = build_export_meta(db_session, month="2026-05", furnaces=["35号工作点"],
                             emp_id=employee_id, persist=False)
    for name in ("payrollSheet", "payrollSheet2"):
        row = meta[name]["rows"][0]
        assert row["实际出勤天数"] == work_hours / 8
        assert row["旷工天数"] == expected
    assert meta["monthlyHours"]["rows"][0][-2] == expected
    direct = build_monthly_calendar(db_session, month="2026-05", restrict_furnace_ids={furnace.id}, persist=False)
    assert next(row for row in direct["rows"] if row[4] == employee_id)[-2] == expected
    workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
    for name in ("输出给薪资", "输出给薪资2", "班次有效工时"):
        sheet = workbook[name]
        headers = [cell.value for cell in sheet[1]]
        assert sheet.cell(2, headers.index("旷工天数") + 1).value == expected


@pytest.mark.parametrize(("today", "month", "expected"), [
    ("2026-05-10", "2026-05", 1),  # 仅昨天计入，今天和明天排除
    ("2026-05-01", "2026-05", 0),  # 月初尚无闭合日期
    ("2026-04-30", "2026-05", 0),  # 未来月份
    ("2026-06-01", "2026-05", 3),  # 完整历史月
])
def test_absence_uses_closed_dates_in_all_sheets(db_session, monkeypatch, today, month, expected):
    from datetime import date
    from app.services import export_service

    monkeypatch.setattr(export_service, "_shanghai_today", lambda: date.fromisoformat(today))
    for day in ("2026-05-09", "2026-05-10", "2026-05-11", "2026-05-15x"):
        db_session.add(ExternalAttendanceResultRaw(user_code="T0001", kq_date=day, kggs="8"))
    db_session.commit()
    meta = build_export_meta(db_session, month=month, furnaces=["35号工作点"], emp_id="T0001",
                             payroll2_include_zero_rows=True, persist=False)
    assert meta["payrollSheet2"]["rows"][0]["旷工天数"] == expected
    if expected:
        assert meta["payrollSheet"]["rows"][0]["旷工天数"] == expected
        assert meta["monthlyHours"]["rows"][0][-2] == expected
    else:
        assert not meta["payrollSheet"]["rows"]
        assert not meta["monthlyHours"]["rows"]
    assert export_service._expected_attendance_days(month) == (
        8 if today == "2026-05-10" else 27 if today == "2026-06-01" else 0
    )


def test_partial_attendance_outside_export_scope_caps_supplement_absence(db_session):
    employee_id, day = "T0504", "2026-05-06"
    db_session.add(Employee(emp_id=employee_id, name="跨工作点半班员工", role="worker"))
    db_session.commit()
    _valid_half_day_attendance(db_session, employee_id, day)
    result = db_session.query(ExternalAttendanceResultRaw).filter_by(user_code=employee_id, kq_date=day).one()
    result.kggs = "8"
    db_session.commit()
    run_recon_for_date(db_session, day)
    meta = build_export_meta(db_session, month="2026-05", furnaces=["35号工作点"], emp_id=employee_id, persist=False)
    assert meta["payrollSheet"]["rows"][0]["旷工天数"] == 0.5
    assert meta["payrollSheet2"]["rows"][0]["旷工天数"] == 0.5
    assert meta["monthlyHours"]["rows"][0][-2] == 0.5
