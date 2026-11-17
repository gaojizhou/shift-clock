from datetime import datetime, timezone
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import event, select

from app.domain.payroll import fold_amounts, payroll_ratio
from app.models import AttendanceRecord, Furnace, ExternalAttendanceResultRaw, LaoqinPunch, ReconResult
from app.services import export_service
from app.services.export_service import (
    _build_payroll2_sheet,
    _expected_attendance_days,
    build_export_meta,
    build_export_xlsx,
    export_health_checks,
)


def _add_scan(db, emp, employ_type, day):
    f35 = db.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    db.add(
        AttendanceRecord(
            emp_id=emp,
            furnace_id=f35.id,
            squad="3班",
            post="出料工",
            employ_type=employ_type,
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=day,
        )
    )
    db.commit()


def _add_work_record(
    db,
    furnace,
    day,
    post,
    *,
    emp="T0001",
    squad="1班",
    hours=8.0,
):
    db.add(
        AttendanceRecord(
            emp_id=emp,
            furnace_id=furnace.id,
            squad=squad,
            post=post,
            employ_type="正式",
            work_hours=hours,
            record_kind="出勤",
            recon_dingtalk=True,
            source="扫码",
            status="active",
            natual_date=day,
        )
    )


def _add_result(
    db,
    day,
    *,
    emp="T0001",
    work_hours=0.0,
    leave_01=0.0,
    leave_02=0.0,
    leave_03=0.0,
):
    db.add(
        ExternalAttendanceResultRaw(
            user_code=emp,
            user_name=emp,
            kq_date=day,
            status_name="正常",
            cqgs=f"{work_hours:.5f}",
            leave_01=f"{leave_01:.5f}" if leave_01 else None,
            leave_02=f"{leave_02:.5f}" if leave_02 else None,
            leave_03=f"{leave_03:.5f}" if leave_03 else None,
        )
    )


def _add_raw_absence(db):
    """独立的原始旷工 10 小时，月度各表应承接 1.25 天。"""
    # 两个自然日分别 8h 和 2h，避免用单日超过 8h 的无效样本。
    for day, hours in [("2026-04-28", "8"), ("2026-04-29", "2")]:
        row = db.scalar(select(ExternalAttendanceResultRaw).where(
            ExternalAttendanceResultRaw.user_code == "T0001",
            ExternalAttendanceResultRaw.kq_date == day,
        ))
        if row is None:
            row = ExternalAttendanceResultRaw(user_code="T0001", kq_date=day)
            db.add(row)
        row.kggs = hours
    db.commit()


@pytest.mark.parametrize("group_by", [None, ["工号", "工作点", "班组", "岗位", "用工类型"]])
def test_display_leave_fills_both_payroll_sheets_without_changing_calculations(db_session, group_by):
    furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, post in [("2026-04-01", "出料工"), ("2026-04-02", "物料转运工")]:
        _add_work_record(db_session, furnace, day, post)
        _add_result(db_session, day, work_hours=8)
    _add_result(db_session, "2026-04-03", leave_01=8)
    db_session.commit()
    kwargs = dict(month="2026-04", furnaces=["35号工作点"], emp_id="T0001", payroll2_group_by=group_by)
    before = build_export_meta(db_session, **kwargs)

    # 即使与满班出勤同日，展示字段也保留原始总量，不裁剪、不抵扣旷工。
    result = db_session.scalar(select(ExternalAttendanceResultRaw).where(
        ExternalAttendanceResultRaw.kq_date == "2026-04-01"
    ))
    result.leave_05 = "4.00000"
    result.leave_07 = "8.00000"
    result.leave_09 = "12.00000"
    result.leave_10 = "16.00000"
    result.leave_12 = "2.00000"
    result.leave_13 = "24.00000"
    result.leave_04 = "80.00000"  # 无预留列，不并入病假。
    result.leave_06 = "80.00000"  # 无预留列，不并入其他假期。
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-04", leave_05="2.00000",
    ))
    db_session.commit()
    after = build_export_meta(db_session, **kwargs)
    expected = {
        "病假(天)": 0.75, "婚假(天)": 1, "产假(天)": 1.5,
        "陪产假(天)": 2, "丧假(天)": 0.25, "工伤假(天)": 3,
    }
    workbook = load_workbook(BytesIO(build_export_xlsx(after)))
    for key, title in [("payrollSheet", "输出给薪资"), ("payrollSheet2", "输出给薪资2")]:
        assert after[key]["columns"] == before[key]["columns"]
        assert after[key]["rowCount"] == before[key]["rowCount"]
        rows = after[key]["rows"]
        for column, value in expected.items():
            assert sum(float(row[column] or 0) for row in rows) == value
            assert sum(bool(row[column]) for row in rows) == 1
        # 除六个展示列外，逐行核对所有列（含请假总时长、旷工、出勤与薪资）不变。
        assert [
            {k: v for k, v in row.items() if k not in expected} for row in rows
        ] == [
            {k: v for k, v in row.items() if k not in expected} for row in before[key]["rows"]
        ]
        ws = workbook[title]
        headers = [cell.value for cell in ws[1]]
        for column, value in expected.items():
            assert sum(float(row[headers.index(column)] or 0) for row in ws.iter_rows(min_row=2, values_only=True)) == value
    assert after["monthlyHours"] == before["monthlyHours"]


def test_display_leave_only_employee_is_visible_without_affecting_absence(db_session):
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-01", leave_05="16.00000",
        leave_07="0.00000", leave_09=None,
    ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-02", leave_05="80.00000",
        source_withdrawn_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
    ))
    db_session.commit()
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    for key in ("payrollSheet", "payrollSheet2"):
        assert meta[key]["rowCount"] == 1
        row = meta[key]["rows"][0]
        assert row["病假(天)"] == 2
        assert row["婚假(天)"] == row["产假(天)"] == ""
        assert row["请假总时长(天)"] in ("", 0)
        assert row["旷工天数"] == 0  # 无原始旷工，展示假期不改变 dev 的既有口径。


def test_display_leave_totals_exclude_other_months_employees_and_invalid_dates(db_session):
    for employee, date, hours in [
        ("T0001", "2026-04-01", "2.00000"),
        ("T0001", "2026-04-02", "4.00000"),
        ("T0001", "2026-03-31", "80.00000"),
        ("T0003", "2026-04-01", "80.00000"),
        ("T0001", "2026-04-31", "80.00000"),
        ("T0001", "2026-04-3", "80.00000"),
    ]:
        db_session.add(ExternalAttendanceResultRaw(user_code=employee, kq_date=date, leave_05=hours))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-04", leave_05="80.00000",
        source_withdrawn_at=datetime(2026, 4, 5, tzinfo=timezone.utc),
    ))
    db_session.commit()
    totals = export_service._payroll_display_leave_totals(db_session, ["T0001"], "2026-04")
    assert list(totals) == ["T0001"]
    assert totals["T0001"]["病假(天)"] == 0.75


def test_export_xlsx_combined_sheets(db_session):
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    data = build_export_xlsx(meta)
    assert len(data) > 100
    wb = load_workbook(BytesIO(data))
    assert len(wb.sheetnames) == 9
    assert wb.sheetnames[0] == "导出说明"
    for name in ("输出给薪资", "输出给薪资2", "班次有效工时", "加班明细", "加班汇总", "按人汇总"):
        assert name in wb.sheetnames
    for name in ("不核算公休工资名单", "纸质版", "工伤"):
        assert name not in wb.sheetnames


def test_export_xlsx_writes_employee_id_as_text_without_apostrophe():
    meta = {
        "sheets": ["输出给薪资"],
        "payrollSheet": {
            "columns": ["工号", "姓名"],
            "rows": [{"工号": "001234", "姓名": "文本工号员工"}],
        },
    }

    ws = load_workbook(BytesIO(build_export_xlsx(meta)))["输出给薪资"]
    employee_id_cell = ws["A2"]
    assert employee_id_cell.value == "001234"
    assert employee_id_cell.data_type == "s"
    assert employee_id_cell.number_format == "@"


def test_payroll_sheet_omits_zero_attendance_without_overtime(db_session):
    """实际出勤为 0 且无加班时，预览和 Excel 都不输出该薪资行。"""
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")

    assert meta["payrollSheet"]["rowCount"] == 0
    assert meta["payrollSheet"]["rows"] == []

    wb = load_workbook(BytesIO(build_export_xlsx(meta)))
    ws = wb["输出给薪资"]
    assert ws.max_row == 1


def test_payroll2_zero_rows_visibility_switch_applies_to_preview_and_excel(db_session):
    """薪资2默认隐藏零出勤零加班行，勾选后预览与 Excel 同时显示。"""
    hidden = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
    )
    assert hidden["payrollSheet2"]["rowCount"] == 0
    assert hidden["payrollSheet2"]["rows"] == []
    hidden_ws = load_workbook(BytesIO(build_export_xlsx(hidden)))["输出给薪资2"]
    assert hidden_ws.max_row == 1

    shown = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_include_zero_rows=True,
    )
    assert shown["payrollSheet2"]["rowCount"] == 1
    assert shown["payrollSheet2"]["rows"][0]["实际出勤天数"] == 0
    assert shown["payrollSheet2"]["rows"][0]["加班合计"] == 0
    assert shown["payrollSheet2"]["rows"][0]["旷工天数"] == 0
    warning = next(
        item for item in shown["warnings"]
        if item["type"] == "absence_owner_undetermined"
    )
    assert warning["employeeIds"] == ["T0001"]
    shown_ws = load_workbook(BytesIO(build_export_xlsx(shown)))["输出给薪资2"]
    assert shown_ws.max_row == 2


def test_payroll_ratio_mapping():
    assert payroll_ratio("正式") == 1.0
    assert payroll_ratio("实习") == 0.7
    assert payroll_ratio("待岗") == 0.8
    assert payroll_ratio("未知") == 1.0  # 默认正式


def test_export_meta_includes_ratio_column(db_session):
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    cols = meta["payrollSheet"]["columns"]
    assert "薪资比例系数" in cols
    assert "用工类型" in cols


def test_payroll_export_excludes_identity_helper_columns(db_session):
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")

    for sheet_name in ("payrollSheet", "payrollSheet2"):
        cols = meta[sheet_name]["columns"]
        assert "身份证号" not in cols
        assert "HS+工号" not in cols


def test_expected_attendance_days_calendar_days_minus_four(db_session):
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    row = meta["payrollSheet"]["sampleRow"]
    assert row["应出勤天数"] == 26  # 2026-04 has 30 calendar days; expected = 30 - 4


def test_current_month_expected_days_prorates_rest_days_at_month_start(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 4, 12)
            return value.replace(tzinfo=tz) if tz else value

    monkeypatch.setattr(export_service, "datetime", FixedDatetime)
    assert _expected_attendance_days("2026-08") == 3


def test_current_month_expected_days_excludes_today(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 17, 12)
            return value.replace(tzinfo=tz) if tz else value

    monkeypatch.setattr(export_service, "datetime", FixedDatetime)
    assert _expected_attendance_days("2026-08") == 14  # 16 elapsed days - 2 prorated rest days


def test_current_month_expected_days_month_end_boundaries():
    assert _expected_attendance_days(
        "2026-08", today=datetime(2026, 8, 30).date()
    ) == 25
    assert _expected_attendance_days(
        "2026-08", today=datetime(2026, 8, 31).date()
    ) == 26
    assert _expected_attendance_days(
        "2026-08", today=datetime(2026, 9, 1).date()
    ) == 27


def test_current_month_export_warns_that_payroll_is_not_closed(monkeypatch, db_session):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 31, 12)
            return value.replace(tzinfo=tz) if tz else value

    monkeypatch.setattr(export_service, "datetime", FixedDatetime)
    meta = build_export_meta(
        db_session, month="2026-08", furnaces=["35号工作点"], emp_id="T0001"
    )

    warning = next(item for item in meta["warnings"] if item["type"] == "in_progress_month")
    assert warning["cutoffDate"] == "2026-08-30"
    assert "不可作为正式薪资依据" in warning["message"]
    workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
    notes = [
        str(value)
        for row in workbook["导出说明"].iter_rows(values_only=True)
        for value in row
        if value
    ]
    assert any("不可作为正式薪资依据" in value for value in notes)


def test_export_health_warns_when_employee_absence_owner_is_outside_scope(db_session):
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    f11 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for day in range(1, 7):
        date = f"2026-04-{day:02d}"
        _add_work_record(db_session, f35, date, "出料工")
        _add_result(db_session, date, work_hours=8)
    for day in range(7, 11):
        date = f"2026-04-{day:02d}"
        _add_work_record(db_session, f11, date, "出料工")
        _add_result(db_session, date, work_hours=8)
    db_session.commit()

    _status, checks = export_health_checks(
        db_session, month="2026-04", furnace_nos=["11号工作点"]
    )
    warning = next(
        item for item in checks if item["type"] == "absence_owned_outside_scope"
    )
    assert warning["employeeIds"] == ["T0001"]
    assert "主工作点在范围外" in warning["message"]

    _status, owner_checks = export_health_checks(
        db_session, month="2026-04", furnace_nos=["35号工作点"]
    )
    assert not any(
        item["type"] == "absence_owned_outside_scope" for item in owner_checks
    )


def test_export_health_owner_check_uses_bounded_query_count(db_session):
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    f11 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for day in range(1, 11):
        date = f"2026-04-{day:02d}"
        _add_work_record(db_session, f35 if day <= 6 else f11, date, "出料工")
        _add_result(db_session, date, work_hours=8)
    db_session.commit()

    statements: list[str] = []
    bind = db_session.get_bind()
    listener = lambda *_args: statements.append(str(_args[2]))
    event.listen(bind, "before_cursor_execute", listener)
    try:
        export_health_checks(db_session, month="2026-04", furnace_nos=["11号工作点"])
    finally:
        event.remove(bind, "before_cursor_execute", listener)

    # 主工作点计算固定批量加载记录/打卡/劳勤结果/对账，不随员工日数量线性发 SQL。
    assert len(statements) < 25


def test_intern_ratio_07(db_session):
    _add_scan(db_session, "T0003", "实习", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0003")
    row = meta["payrollSheet"]["sampleRow"]
    assert row["用工类型"] == "实习"
    assert row["薪资比例系数"] == 0.7


def test_standby_ratio_08(db_session):
    _add_scan(db_session, "T0005", "待岗", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0005")
    row = meta["payrollSheet"]["sampleRow"]
    assert row["用工类型"] == "待岗"
    assert row["薪资比例系数"] == 0.8


def test_formal_ratio_10(db_session):
    _add_scan(db_session, "T0001", "正式", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    row = meta["payrollSheet"]["sampleRow"]
    assert row["用工类型"] == "正式"
    assert row["薪资比例系数"] == 1.0


def test_employ_type_from_actual_scan_not_hardcoded(db_session):
    """用工类型取自实际 scan，而非恒为正式。"""
    _add_scan(db_session, "T0003", "实习", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0003")
    assert meta["payrollSheet"]["sampleRow"]["用工类型"] != "正式"


def test_fold_amounts_applies_ratio():
    """金额列有数值时按系数折算（折算值 = base * ratio）。"""
    row = {"薪资标准": 1000, "承包工资": 5000.0, "其他": "x"}
    fold_amounts(row, 0.7)
    assert row["薪资标准"] == 700.0
    assert row["承包工资"] == 3500.0
    assert row["其他"] == "x"  # 非金额列不动


def test_fold_amounts_keeps_empty_unchanged():
    row = {"薪资标准": "", "承包工资": ""}
    fold_amounts(row, 0.8)
    assert row["薪资标准"] == ""
    assert row["承包工资"] == ""


def test_fold_amounts_keeps_none_and_bool_unchanged():
    """空值(None)与布尔不参与折算（bool 是 int 子类，须显式排除）。"""
    row = {"薪资标准": None, "承包工资": True}
    fold_amounts(row, 0.7)
    assert row["薪资标准"] is None
    assert row["承包工资"] is True


def test_fold_amounts_through_export_meta_applies_to_numeric_amount(db_session):
    """端到端：当金额列为数值时，build_export_meta 输出已按系数折算（实习 0.7）。"""
    _add_scan(db_session, "T0003", "实习", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0003")
    row = dict(meta["payrollSheet"]["sampleRow"])
    # 模拟薪资列已被填入基数后再次折算，验证折算管道接通且系数正确
    row["薪资标准"] = 1000
    fold_amounts(row, row["薪资比例系数"])
    assert row["薪资标准"] == 700.0


def test_payroll_splits_rows_by_squad_and_post(db_session):
    """输出给薪资同一员工按工作点/班组/岗位/用工类型拆行。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, squad, post in [
        ("2026-04-21", "3班", "副班组长"),
        ("2026-04-22", "2班", "物料转运工"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad=squad, post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    rows = meta["payrollSheet"]["rows"]
    target = {(r["部门"], r["岗位"]): r for r in rows}

    assert target[("35号工作点2班", "物料转运工")]["实际出勤天数"] == 1
    assert target[("35号工作点3班", "副班组长")]["实际出勤天数"] == 1
    assert target[("35号工作点2班", "物料转运工")]["旷工天数"] == 1.25
    assert target[("35号工作点3班", "副班组长")]["旷工天数"] == 0


def test_payroll_absence_hours_not_duplicated_by_multiple_posts(db_session):
    """原始旷工按员工汇总；同日多岗位不得重复计算。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    days = set()
    for day, squad, post in [
        ("2026-04-21", "1班", "出料工"),
        ("2026-04-21", "2班", "物料转运工"),
        ("2026-04-22", "3班", "副班组长"),
    ]:
        days.add(day)
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad=squad, post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
    for day in days:
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    rows = meta["payrollSheet"]["rows"]

    assert sum(r["旷工天数"] for r in rows) == 1.25
    assert sum(r["旷工天数"] > 0 for r in rows) == 1


def test_payroll_absence_days_merge_furnace_squad_post_and_ignore_overtime(db_session):
    """跨工作点跨岗位仍只承接一次原始旷工，独立加班不抵扣。"""
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    f11 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for furnace, day, squad, post in [
        (f35, "2026-04-21", "1班", "出料工"),
        (f11, "2026-04-22", "3班", "出料工"),
        (f35, "2026-04-22", "2班", "物料转运工"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=furnace.id, squad=squad, post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
    db_session.add(AttendanceRecord(
        emp_id="T0001", furnace_id=f35.id, squad="1班", post="供气工",
        employ_type="正式", work_hours=8.0, record_kind="加班",
        recon_dingtalk=True, source="加班", status="active",
        natual_date="2026-04-23", shift_slot="早班",
    ))
    for day in ("2026-04-21", "2026-04-22"):
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点", "11号工作点"], emp_id="T0001")
    rows = meta["payrollSheet"]["rows"]

    # 独立加班缺少有效打卡，对账后实际出勤与有效加班都为 0，该行不进薪资表。
    assert len(rows) == 3
    assert all(r["实际出勤天数"] > 0 or r["加班合计"] > 0 for r in rows)
    assert sum(r["旷工天数"] for r in rows) == 1.25
    assert sum(r["旷工天数"] > 0 for r in rows) == 1


def test_payroll2_merges_posts_into_dynamic_columns(db_session):
    """输出给薪资2 按员工+部门+用工类型合并，将多个岗位横向展开。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, post in [
        ("2026-04-21", "出料工"),
        ("2026-04-22", "物料转运工"),
        ("2026-04-23", "物料转运工"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad="1班", post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.add(AttendanceRecord(
        emp_id="T0001", furnace_id=f.id, squad="1班", post="供气工",
        employ_type="正式", work_hours=4.0, record_kind="加班",
        recon_dingtalk=True, source="加班", status="active",
        natual_date="2026-04-24", shift_slot="早班",
    ))
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    sheet = meta["payrollSheet2"]

    assert sheet["rowCount"] == 1
    row = sheet["rows"][0]
    post_days = {
        row[f"岗位{i}"]: row[f"出勤天数{i}"]
        for i in range(1, 3)
    }
    assert post_days == {"物料转运工": 2, "出料工": 1}
    assert "供气工" not in post_days
    assert row["实际出勤天数"] == 3
    assert row["旷工天数"] == 1.25
    expected_salary_columns = [
        "核对",
        "上月薪资标准",
        "本月调整",
        "差异",
        "薪资标准",
        "承包工资",
    ]
    overtime_total_index = sheet["columns"].index("加班合计")
    assert sheet["columns"][overtime_total_index + 1:overtime_total_index + 7] == expected_salary_columns
    assert all(row[column] == "" for column in expected_salary_columns)

    ws = load_workbook(BytesIO(build_export_xlsx(meta)))["输出给薪资2"]
    headers = [cell.value for cell in ws[1]]
    assert headers[overtime_total_index + 1:overtime_total_index + 7] == expected_salary_columns
    assert all(
        ws.cell(row=2, column=overtime_total_index + offset).value is None
        for offset in range(2, 8)
    )


def test_payroll2_inserts_empty_columns_between_absence_and_attendance(db_session):
    expected_empty_columns = [
        "请假总时长(天)",
        "年假(天)",
        "调休(天)",
        "事假(天)",
        "病假(天)",
        "婚假(天)",
        "产假(天)",
        "陪产假(天)",
        "丧假(天)",
        "工伤假(天)",
        "出差天数",
        "平时加班",
        "周末加班",
        "节假日加班",
    ]
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_include_zero_rows=True,
    )
    sheet = meta["payrollSheet2"]
    absence_index = sheet["columns"].index("旷工天数")

    assert sheet["columns"][absence_index + 1:absence_index + 15] == expected_empty_columns
    assert sheet["columns"][absence_index + 15] == "出勤合计"
    assert sheet["columns"][-4:] == [
        "节假日出勤天数",
        "公休未休天数（天）",
        "用工类型",
        "薪资比例系数",
    ]
    assert sheet["rows"][0]["节假日出勤天数"] == ""
    assert sheet["rows"][0]["公休未休天数（天）"] == ""
    assert all(sheet["rows"][0][column] == (0 if column == "出差天数" else "") for column in expected_empty_columns)

    ws = load_workbook(BytesIO(build_export_xlsx(meta)))["输出给薪资2"]
    headers = [cell.value for cell in ws[1]]
    assert headers[absence_index + 1:absence_index + 15] == expected_empty_columns
    assert headers[absence_index + 15] == "出勤合计"
    assert headers[-4:] == [
        "节假日出勤天数",
        "公休未休天数（天）",
        "用工类型",
        "薪资比例系数",
    ]
    assert ws.cell(row=2, column=len(headers) - 3).value is None
    assert ws.cell(row=2, column=len(headers) - 2).value is None
    assert all(
        ws.cell(row=2, column=absence_index + offset).value ==
        (0 if headers[absence_index + offset - 1] == "出差天数" else None)
        for offset in range(2, 16)
    )


def test_payroll2_group_by_post_splits_rows(db_session):
    """薪资2勾选岗位维度时岗位紧跟部门，薪资属性仍置于末尾。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, post in [
        ("2026-04-21", "出料工"),
        ("2026-04-22", "物料转运工"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad="1班", post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )
    sheet = meta["payrollSheet2"]

    assert sheet["rowCount"] == 2
    assert "岗位1" not in sheet["columns"]
    assert sheet["columns"].index("岗位") == sheet["columns"].index("部门") + 1
    assert sheet["columns"][-4:] == [
        "节假日出勤天数",
        "公休未休天数（天）",
        "用工类型",
        "薪资比例系数",
    ]
    assert "行名" in sheet["columns"]
    assert "合并条目" not in sheet["columns"]
    assert {row["岗位"] for row in sheet["rows"]} == {"出料工", "物料转运工"}
    assert {row["实际出勤天数"] for row in sheet["rows"]} == {1}
    assert {row["行名"] for row in sheet["rows"]} == {
        "T0001/35号工作点/1班/出料工/正式",
        "T0001/35号工作点/1班/物料转运工/正式",
    }


def test_leave_segment_with_same_day_attendance_uses_latest_day_post(db_session):
    """连续请假段含实际出勤时归到该段最后一个有出勤的岗位，不再复制每行。"""
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day, post in [
        ("2026-04-21", "出料工"),
        ("2026-04-22", "物料转运工"),
    ]:
        db_session.add(
            AttendanceRecord(
                emp_id="T0001",
                furnace_id=furnace.id,
                squad="1班",
                post=post,
                employ_type="正式",
                work_hours=4.0,
                record_kind="出勤",
                recon_dingtalk=True,
                source="扫码",
                status="active",
                natual_date=day,
            )
        )
    db_session.add_all(
        [
            ExternalAttendanceResultRaw(
                user_code="T0001",
                user_name="T0001",
                kq_date="2026-04-21",
                status_name="正常",
                cqgs="4.00000",
                leave_01="4.00000",
            ),
            ExternalAttendanceResultRaw(
                user_code="T0001",
                user_name="T0001",
                kq_date="2026-04-22",
                status_name="正常",
                cqgs="4.00000",
                leave_03="4.00000",
            ),
        ]
    )
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )
    rows = meta["payrollSheet2"]["rows"]
    assert len(rows) == 2
    by_post = {row["岗位"]: row for row in rows}
    assert by_post["出料工"]["请假总时长(天)"] == ""
    assert by_post["物料转运工"]["请假总时长(天)"] == 1
    assert by_post["物料转运工"]["年假(天)"] == 0.5
    assert by_post["物料转运工"]["调休(天)"] == ""
    assert by_post["物料转运工"]["事假(天)"] == 0.5
    assert by_post["物料转运工"]["旷工天数"] == 1.25
    assert by_post["出料工"]["旷工天数"] == 0


def test_payroll2_preserves_source_absence_without_recomputing():
    sheet = _build_payroll2_sheet(
        [
            {
                "工号": "E001",
                "姓名": "测试员工",
                "部门": "35号工作点1班",
                "岗位": "出料工",
                "用工类型": "正式",
                "薪资比例系数": 1,
                "应出勤天数": 2,
                "实际出勤天数": 3,
                "旷工天数": 1.25,
                "请假总时长(天)": 1,
                "年假(天)": 1,
                "出勤合计": 3,
                "出勤合计（不含年休、调休）": 3,
                "加班合计": 0,
            }
        ],
        ["工号", "工作点", "班组", "岗位", "用工类型"],
    )

    assert sheet["rows"][0]["旷工天数"] == 1.25


def test_half_shift_does_not_create_absence_without_source_hours(db_session):
    """原始记录无旷工小时数时，半班与整月出勤缺口均不推算旷工。"""
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day in ("2026-04-21", "2026-04-22"):
        _add_work_record(db_session, furnace, day, "出料工", hours=4.0)
        _add_result(db_session, day, work_hours=4.0)
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )

    payroll_rows = meta["payrollSheet"]["rows"]
    assert payroll_rows[0]["实际出勤天数"] == 1  # 展示仍为折算天数（0.5×2）
    assert payroll_rows[0]["旷工天数"] == 0     # 未提供原始旷工小时数

    payroll2_rows = meta["payrollSheet2"]["rows"]
    assert payroll2_rows[0]["实际出勤天数"] == 1
    assert payroll2_rows[0]["旷工天数"] == 0

    monthly_hours = meta.get("monthlyHours")
    if monthly_hours:
        hour_row = next(
            row for row in monthly_hours["rows"] if str(row[4]) == "T0001"
        )
        assert hour_row[-2] == 0  # 班次有效工时表同口径


def test_multiple_leave_segments_follow_adjacent_posts_in_all_exports(db_session):
    """月初、月中、月底三段请假分别找复工岗位或请假前最后岗位。"""
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day, post in (
        ("2026-04-06", "物料转运工"),
        ("2026-04-16", "出料工"),
        ("2026-04-28", "物料转运工"),
    ):
        _add_work_record(db_session, furnace, day, post)
        _add_result(db_session, day, work_hours=8)
    for day in range(1, 6):
        _add_result(db_session, f"2026-04-{day:02d}", leave_03=8)
    for day in range(12, 16):
        _add_result(db_session, f"2026-04-{day:02d}", leave_01=8)
    for day in range(29, 31):
        _add_result(db_session, f"2026-04-{day:02d}", leave_02=8)
    db_session.commit()

    # 28日满班、29日全天请假，源旷工应全部裁掉。
    _add_raw_absence(db_session)
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )

    payroll1 = {row["岗位"]: row for row in meta["payrollSheet"]["rows"]}
    payroll2 = {row["岗位"]: row for row in meta["payrollSheet2"]["rows"]}
    for rows in (payroll1, payroll2):
        assert rows["物料转运工"]["实际出勤天数"] == 2
        assert rows["物料转运工"]["请假总时长(天)"] == 7
        assert rows["物料转运工"]["年假(天)"] in ("", 0)
        assert rows["物料转运工"]["调休(天)"] == 2
        assert rows["物料转运工"]["事假(天)"] == 5
        assert rows["出料工"]["实际出勤天数"] == 1
        assert rows["出料工"]["请假总时长(天)"] == 4
        assert rows["出料工"]["年假(天)"] == 4
        assert rows["出料工"]["调休(天)"] in ("", 0)
        assert rows["出料工"]["事假(天)"] in ("", 0)
        assert rows["物料转运工"]["旷工天数"] == 0
        assert rows["出料工"]["旷工天数"] == 0

    calendar_rows = {
        row[5]: row
        for row in meta["monthlyHours"]["rows"]
        if row[4] == "T0001"
    }
    assert calendar_rows["物料转运工"][-4] == 2
    assert calendar_rows["出料工"][-4] == 1
    assert calendar_rows["物料转运工"][-3] == 0
    assert calendar_rows["出料工"][-3] == 4
    assert calendar_rows["物料转运工"][-2] == 0
    assert calendar_rows["出料工"][-2] == 0

    workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
    sheet = workbook["输出给薪资"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    exported = {
        sheet.cell(row=index, column=headers["岗位"]).value: {
            "leave": sheet.cell(
                row=index, column=headers["请假总时长(天)"]
            ).value,
            "absence": sheet.cell(row=index, column=headers["旷工天数"]).value,
        }
        for index in range(2, sheet.max_row + 1)
    }
    assert exported == {
        "出料工": {"leave": 4, "absence": 0},
        "物料转运工": {"leave": 7, "absence": 0},
    }


def test_invalid_attendance_result_date_is_ignored_instead_of_breaking_export(db_session):
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    _add_work_record(db_session, furnace, "2026-04-02", "出料工")
    _add_result(db_session, "2026-04-02", work_hours=8)
    _add_result(db_session, "2026-04-1", leave_01=8)
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=[furnace.furnace_no],
        furnace_ids=[furnace.id],
        emp_id="T0001",
    )

    assert meta["payrollSheet"]["rowCount"] == 1
    assert meta["payrollSheet"]["rows"][0]["请假总时长(天)"] in ("", 0)


def test_leave_only_employee_keeps_leave_and_source_absence_in_all_payroll_sheets(
    db_session,
):
    for day in range(1, 11):
        _add_result(db_session, f"2026-04-{day:02d}", leave_01=8)
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
    )

    for sheet_name in ("payrollSheet", "payrollSheet2"):
        assert meta[sheet_name]["rowCount"] == 1
        row = meta[sheet_name]["rows"][0]
        assert row["实际出勤天数"] == 0
        assert row["请假总时长(天)"] == 10
        assert row["年假(天)"] == 10
        assert row["旷工天数"] == 1.25
    warning = next(
        item
        for item in meta["warnings"]
        if item["type"] == "absence_owner_undetermined"
    )
    assert warning["unallocatedLeaveDays"] == 10
    assert "无法归属到岗位的请假 10 天" in warning["message"]


def test_leave_only_calendar_supplement_uses_source_absence(
    db_session,
):
    selected = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    outside = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "11号工作点")
    )
    _add_work_record(db_session, outside, "2026-04-02", "出料工")
    _add_result(db_session, "2026-04-02", work_hours=8)
    _add_result(db_session, "2026-04-03", leave_01=8)
    db_session.commit()

    _add_raw_absence(db_session)
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=[selected.furnace_no],
        furnace_ids=[selected.id],
        emp_id="T0001",
    )

    payroll = meta["payrollSheet"]["rows"][0]
    calendar = meta["monthlyHours"]["rows"][0]
    # 表内实际出勤仍按所选工作点展示；旷工取自整月原始考勤结果。
    assert payroll["实际出勤天数"] == 0
    assert payroll["请假总时长(天)"] == 1
    assert payroll["旷工天数"] == 1.25
    assert calendar[-4] == 0
    assert calendar[-3] == 1
    assert calendar[-2] == 1.25
    warning = next(
        item
        for item in meta["warnings"]
        if item["type"] == "monthly_hours_attendance_outside_scope"
    )
    assert warning["outsideActualDays"] == 1
    assert "范围外有有效出勤 1 天" in warning["message"]


def test_main_export_uses_bulk_validity_without_materializing_reconciliation(
    db_session, monkeypatch
):
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day in ("2026-04-01", "2026-04-02"):
        _add_work_record(db_session, furnace, day, "出料工")
        _add_result(db_session, day, work_hours=8)
    db_session.commit()

    def legacy_validity_must_not_run(*_args, **_kwargs):
        raise AssertionError("主导出路径不应逐员工调用 _valid_day_fractions")

    recon_dates: list[str] = []
    monkeypatch.setattr(
        export_service, "_valid_day_fractions", legacy_validity_must_not_run
    )
    from app.services import recon_service

    original_recon = recon_service.run_recon_for_date

    def record_recon(db, date, *, commit=True):
        recon_dates.append(date)
        return original_recon(db, date, commit=commit)

    monkeypatch.setattr(recon_service, "run_recon_for_date", record_recon)
    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=[furnace.furnace_no],
        furnace_ids=[furnace.id],
        emp_id="T0001",
        include_monthly_hours=False,
        include_overtime_detail=False,
    )

    assert recon_dates == []
    assert meta["payrollSheet"]["sampleRow"]["实际出勤天数"] == 2
    assert db_session.query(ReconResult).count() == 0


def test_monthly_leave_and_absence_belong_only_to_dominant_furnace_batch(db_session):
    f35 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    f11 = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for furnace, day in (
        (f35, "2026-04-01"),
        (f35, "2026-04-02"),
        (f11, "2026-04-04"),
    ):
        _add_work_record(db_session, furnace, day, "出料工")
        _add_result(db_session, day, work_hours=8)
    _add_result(db_session, "2026-04-03", leave_01=8)
    db_session.commit()

    _add_raw_absence(db_session)
    owner = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=[f35.furnace_no],
        furnace_ids=[f35.id],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )
    other = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=[f11.furnace_no],
        furnace_ids=[f11.id],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )

    for sheet_name in ("payrollSheet", "payrollSheet2"):
        owner_row = owner[sheet_name]["rows"][0]
        other_row = other[sheet_name]["rows"][0]
        assert owner_row["请假总时长(天)"] == 1
        assert owner_row["年假(天)"] == 1
        assert owner_row["旷工天数"] == 1.25
        assert other_row["请假总时长(天)"] in ("", 0)
        assert other_row["年假(天)"] in ("", 0)
        assert other_row["旷工天数"] == 0

    warning = next(
        item for item in other["warnings"]
        if item["type"] == "absence_owned_outside_scope"
    )
    assert warning["employeeIds"] == ["T0001"]
    workbook = load_workbook(BytesIO(build_export_xlsx(other)))
    notes = [
        str(value)
        for row in workbook["导出说明"].iter_rows(values_only=True)
        for value in row
        if value
    ]
    assert any("主工作点在范围外" in value for value in notes)

    owner_calendar = owner["monthlyHours"]["rows"][0]
    other_calendar = other["monthlyHours"]["rows"][0]
    assert owner_calendar[-3:-1] == [1, 1.25]
    assert other_calendar[-3:-1] == [0, 0]


def test_same_day_leave_uses_most_hours_then_stable_post_tie(db_session):
    """半天请假优先当天岗位；多岗位先比工时，再按固定岗位顺序打破并列。"""
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day, post, hours in (
        ("2026-04-10", "出料工", 2),
        ("2026-04-10", "物料转运工", 4),
        ("2026-04-12", "物料转运工", 4),
        ("2026-04-12", "出料工", 4),
    ):
        _add_work_record(db_session, furnace, day, post, hours=hours)
    _add_result(db_session, "2026-04-10", work_hours=4, leave_02=4)
    _add_result(db_session, "2026-04-12", work_hours=4, leave_03=4)
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )
    by_post = {row["岗位"]: row for row in meta["payrollSheet2"]["rows"]}

    assert by_post["物料转运工"]["调休(天)"] == 0.5
    assert by_post["物料转运工"]["事假(天)"] in ("", 0)
    assert by_post["出料工"]["调休(天)"] in ("", 0)
    assert by_post["出料工"]["事假(天)"] == 0.5


def test_wang_example_assigns_18_day_leave_to_first_post_after_leave(db_session):
    """王君圣示例：1-18 日事假归到 19 日复工岗位，另一岗位不重复请假。"""
    furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    for day in range(1, 19):
        _add_result(db_session, f"2026-07-{day:02d}", leave_03=8)
    push_driver_days = [*range(19, 28), 29, 30, 31]
    for day in push_driver_days:
        date_text = f"2026-07-{day:02d}"
        _add_work_record(db_session, furnace, date_text, "物料转运工", emp="T0001", squad="3班")
        _add_result(db_session, date_text, work_hours=8)
    _add_work_record(
        db_session, furnace, "2026-07-28", "出料工", emp="T0001", squad="3班"
    )
    _add_result(db_session, "2026-07-28", work_hours=8)
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-07",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点", "班组", "岗位", "用工类型"],
    )
    rows = {row["岗位"]: row for row in meta["payrollSheet2"]["rows"]}

    assert rows["物料转运工"]["实际出勤天数"] == 12
    assert rows["物料转运工"]["请假总时长(天)"] == 18
    assert rows["物料转运工"]["事假(天)"] == 18
    assert rows["出料工"]["实际出勤天数"] == 1
    assert rows["出料工"]["请假总时长(天)"] == ""
    assert {row["旷工天数"] for row in rows.values()} == {0}
    assert {
        row[-2]
        for row in meta["monthlyHours"]["rows"]
        if row[4] == "T0001"
    } == {0}


def test_leave_without_any_attendance_uses_unassigned_employee_row(db_session):
    for day in range(1, 4):
        _add_result(db_session, f"2026-04-{day:02d}", leave_01=8)
    db_session.commit()

    kwargs = {
        "month": "2026-04",
        "furnaces": ["35号工作点"],
        "emp_id": "T0001",
        "payroll2_include_zero_rows": True,
    }
    _add_raw_absence(db_session)
    first = build_export_meta(db_session, **kwargs)
    second = build_export_meta(db_session, **kwargs)

    payroll1 = first["payrollSheet"]["rows"]
    payroll2 = first["payrollSheet2"]["rows"]
    assert len(payroll1) == len(payroll2) == 1
    for row in (*payroll1, *payroll2):
        assert row["部门"] == ""
        assert row.get("岗位", "") == ""
        assert row["请假总时长(天)"] == 3
        assert row["旷工天数"] == 1.25
    calendar_rows = first["monthlyHours"]["rows"]
    assert len(calendar_rows) == 1
    assert calendar_rows[0][:6] == ["", "", "", "T0001", "T0001", ""]
    assert calendar_rows[0][-5:] == [26, 0, 3, 1.25, ""]
    assert second["payrollSheet2"]["rows"] == payroll2


def test_payroll2_group_by_furnace_keeps_squad_in_post_slots(db_session):
    """只按工号+工作点合并时，岗位展开项必须带班组，避免看不出在哪个班组工作。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, squad, post in [
        ("2026-04-21", "1班", "出料工"),
        ("2026-04-22", "3班", "副班组长"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad=squad, post=post,
            employ_type="正式", work_hours=8.0, record_kind="出勤",
            recon_dingtalk=True, source="扫码", status="active",
            natual_date=day,
        ))
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001",
            user_name="T0001",
            kq_date=day,
            status_name="正常",
            cqgs="8.00000",
        ))
    db_session.commit()

    meta = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_group_by=["工号", "工作点"],
    )
    row = meta["payrollSheet2"]["rows"][0]

    assert row["行名"] == "T0001/35号工作点"
    assert row["部门"] == "35号工作点"
    assert {row["岗位1"], row["岗位2"]} == {"1班 / 出料工", "3班 / 副班组长"}
    assert {row["出勤天数1"], row["出勤天数2"]} == {1}


def test_payroll2_merge_overtime_only_rows_only_within_same_furnace_squad():
    """零出勤加班只并入同工作点班组的出勤行；其他班组的加班行保留。"""

    def payroll_row(
        emp_id: str,
        department: str,
        post: str,
        actual_days: float,
        overtime_hours: float,
    ) -> dict:
        row = {
            "工号": emp_id,
            "姓名": emp_id,
            "部门": department,
            "岗位": post,
            "用工类型": "正式",
            "薪资比例系数": 1,
            "应出勤天数": 26,
            "实际出勤天数": actual_days,
            "旷工天数": 0,
            "出勤合计": actual_days,
            "出勤合计（不含年休、调休）": actual_days,
            "加班合计": overtime_hours,
        }
        if overtime_hours:
            row["加班1"] = overtime_hours
            row["加班标准1"] = post
        return row

    sheet = _build_payroll2_sheet(
        [
            # 1班没有出勤，只有加班，必须保留。
            payroll_row("E001", "35号工作点1班", "副班组长", 0, 1),
            # 2班副班组长有出勤，2班供气工的零出勤加班应并入该行。
            payroll_row("E001", "35号工作点2班", "副班组长", 2, 2),
            payroll_row("E001", "35号工作点2班", "供气工", 0, 3),
            # 没有任何有效出勤目标，开启合并后仍须保留。
            payroll_row("E002", "36号工作点1班", "加料操作工", 0, 4),
            # 目标行没有已有加班时，应写入加班1，不能因全表预建空列而跳到加班2。
            payroll_row("E003", "37号工作点1班", "现场操作工", 1, 0),
            payroll_row("E003", "37号工作点1班", "加料操作工", 0, 5),
        ],
        ["工号", "工作点", "班组", "岗位", "用工类型"],
        merge_same_squad_overtime_only_rows=True,
    )

    assert sheet["rowCount"] == 4
    salary_columns = ["核对", "上月薪资标准", "本月调整", "差异", "薪资标准", "承包工资"]
    overtime_total_index = sheet["columns"].index("加班合计")
    assert sheet["columns"][overtime_total_index + 1:overtime_total_index + 7] == salary_columns
    assert overtime_total_index + 7 <= sheet["columns"].index("加班1")
    assert all(
        row[column] == ""
        for row in sheet["rows"]
        for column in salary_columns
    )
    merged = next(row for row in sheet["rows"] if row["行名"] == "E001/35号工作点/2班/副班组长/正式")
    assert merged["加班合计"] == 5
    assert (merged["加班1"], merged["加班标准1"]) == (2, "副班组长")
    assert (merged["加班2"], merged["加班标准2"]) == (3, "供气工")
    kept = next(row for row in sheet["rows"] if row["行名"] == "E001/35号工作点/1班/副班组长/正式")
    assert kept["实际出勤天数"] == 0
    assert kept["加班合计"] == 1
    unmatched = next(row for row in sheet["rows"] if row["工号"] == "E002")
    assert unmatched["实际出勤天数"] == 0
    empty_target = next(row for row in sheet["rows"] if row["行名"] == "E003/37号工作点/1班/现场操作工/正式")
    assert (empty_target["加班1"], empty_target["加班标准1"]) == (5, "加料操作工")
    assert unmatched["加班合计"] == 4
    assert (empty_target["加班2"], empty_target["加班标准2"]) == ("", "")


def test_mode_employ_type_uses_majority_not_first(db_session):
    """用工类型取众数：两条实习 vs 一条正式 → 实习（非取首条/非硬编码正式）。"""
    _add_scan(db_session, "T0003", "正式", "2026-04-10")
    _add_scan(db_session, "T0003", "实习", "2026-04-11")
    _add_scan(db_session, "T0003", "实习", "2026-04-12")
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0003")
    row = meta["payrollSheet"]["sampleRow"]
    assert row["用工类型"] == "实习"
    assert row["薪资比例系数"] == 0.7


def test_overtime_excludes_regular_8h_shift(db_session):
    """加班合计只算超 8h 部分 + 纯加班记录；整班 8h 不计加班。

    回归：原实现把每条出勤记录的 work_hours 全额累加进 post_hours，
    导致 12 天整班(8h)的工人加班合计 = 96h（虚高）。修复后：
      - 出勤 8h → OT 0
      - 出勤 12h → OT 4（仅超出部分）
      - 加班记录 3h → OT 3（全段）
    合计 = 7.0，而非 8+12+3=23。
    """
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    common = dict(
        furnace_id=f.id, post="出料工", employ_type="正式",
        source="扫码", status="active",
    )
    db_session.add(AttendanceRecord(
        emp_id="T0001", natual_date="2026-04-13", squad="3班",
        work_hours=8.0, record_kind="出勤", recon_dingtalk=True, **common,
    ))
    db_session.add(AttendanceRecord(
        emp_id="T0001", natual_date="2026-04-14", squad="3班",
        work_hours=12.0, record_kind="出勤", recon_dingtalk=True, **common,
    ))
    db_session.add(AttendanceRecord(
        emp_id="T0001", natual_date="2026-04-15", squad="1班",
        work_hours=3.0, record_kind="加班", recon_dingtalk=True,
        furnace_id=f.id, post="供气工", employ_type="正式", source="扫码", status="active",
        shift_slot="晚班",
    ))
    for h, direct in ((2, "进"), (10, "出")):
        db_session.add(LaoqinPunch(
            emp_id="T0001",
            punch_time=datetime(2026, 4, 15, h),
            inout_type=direct,
            natual_date="2026-04-15",
        ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001",
        user_name="T0001",
        kq_date="2026-04-13",
        status_name="正常",
        cqgs="8.00000",
    ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001",
        user_name="T0001",
        kq_date="2026-04-14",
        status_name="正常",
        cqgs="12.00000",
    ))
    db_session.commit()
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    rows = meta["payrollSheet"]["rows"]
    out_row = next(r for r in rows if r["岗位"] == "出料工")
    oxy_row = next(r for r in rows if r["岗位"] == "供气工")
    assert out_row["实际出勤天数"] == 2
    assert out_row["加班合计"] == 4.0
    assert out_row["加班1"] == 4.0
    assert out_row["加班标准1"] == "出料工"
    assert oxy_row["实际出勤天数"] == 0
    assert oxy_row["加班合计"] == 3.0
    assert oxy_row["加班1"] == 3.0
    assert oxy_row["加班标准1"] == "供气工"
    assert any(
        row["实际出勤天数"] == 0 and row["加班合计"] == 3.0
        for row in meta["payrollSheet2"]["rows"]
    )

    merged = build_export_meta(
        db_session,
        month="2026-04",
        furnaces=["35号工作点"],
        emp_id="T0001",
        payroll2_merge_overtime_only_rows=True,
    )
    merged_rows = merged["payrollSheet2"]["rows"]
    # 原开关保留宽松规则，可将同员工的零出勤加班并入其他班组出勤行。
    assert not any(
        row["实际出勤天数"] == 0 and row["加班合计"] > 0
        for row in merged_rows
    )
    merged_out_row = next(row for row in merged_rows if row["实际出勤天数"] == 2)
    assert merged_out_row["加班合计"] == 7
    assert {
        (merged_out_row["加班1"], merged_out_row["加班标准1"]),
        (merged_out_row["加班2"], merged_out_row["加班标准2"]),
    } == {(4, "出料工"), (3, "供气工")}


def test_payroll_overtime_excludes_invalid_independent_overtime(db_session):
    """独立加班无效不计加班，但不影响同日正常出勤天数。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    db_session.add(AttendanceRecord(
        emp_id="T0001", furnace_id=f.id, squad="3班", post="出料工",
        employ_type="正式", work_hours=8.0, record_kind="出勤",
        recon_dingtalk=True, source="扫码", status="active",
        natual_date="2026-04-16",
    ))
    db_session.add(AttendanceRecord(
        emp_id="T0001", furnace_id=f.id, squad="1班", post="供气工",
        employ_type="正式", work_hours=6.0, record_kind="加班",
        recon_dingtalk=True, source="加班", status="active",
        natual_date="2026-04-16",
    ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001",
        user_name="T0001",
        kq_date="2026-04-16",
        status_name="正常",
        cqgs="8.00000",
    ))
    db_session.add(ReconResult(
        emp_id="T0001",
        date="2026-04-16",
        status="工作点有劳勤无",
        hr_status="待值班长确认",
        chief_status="未确认",
    ))
    db_session.commit()

    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    row = next(r for r in meta["payrollSheet"]["rows"] if r["岗位"] == "出料工")
    assert row["实际出勤天数"] == 1
    assert row["加班合计"] == 0
    assert "加班1" not in row
    assert "加班1" not in meta["payrollSheet"]["columns"]


def test_payroll_splits_overtime_rows_and_tripartite_overtime_slots_are_unbounded(db_session):
    """输出给薪资按加班班组/岗位拆行；三方对账仍按实际加班条数动态铺开。"""
    f = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for post, hours, squad in [
        ("出料工", 8.0, "1班"),
        ("供气工", 6.0, "2班"),
        ("加料操作工", 4.0, "3班"),
        ("物料转运工", 2.0, "4班"),
        ("巡检工", 1.0, "5班"),
    ]:
        db_session.add(AttendanceRecord(
            emp_id="T0001", furnace_id=f.id, squad=squad, post=post,
            employ_type="正式", work_hours=hours, record_kind="加班",
            recon_dingtalk=True, source="加班", status="active",
            natual_date="2026-04-20", shift_slot="晚班",
        ))
    for h, direct in ((2, "进"), (10, "出")):
        db_session.add(LaoqinPunch(
            emp_id="T0001",
            punch_time=datetime(2026, 4, 20, h),
            inout_type=direct,
            natual_date="2026-04-20",
        ))
    db_session.commit()

    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    payroll_rows = meta["payrollSheet"]["rows"]
    assert len(payroll_rows) == 5
    assert sorted((r["岗位"], r["加班合计"]) for r in payroll_rows) == [
        ("供气工", 6.0),
        ("出料工", 8.0),
        ("加料操作工", 4.0),
        ("巡检工", 1.0),
        ("物料转运工", 2.0),
    ]
    assert "加班1" in meta["payrollSheet"]["columns"]
    assert "加班标准1" in meta["payrollSheet"]["columns"]

    tripartite = meta["extraSheets"]["三方对账（劳勤/工作点/补录）"]
    assert "加班5" in tripartite["columns"]
    assert "加班标准5" in tripartite["columns"]


def test_preview_meta_is_readonly_no_recon_persisted(db_session):
    """预览(persist=False)不落库：返回正确 meta，但 ReconResult 不持久化。

    预览端点用 persist=False 调 build_export_meta（只 flush 不 commit），
    再由路由 rollback 丢弃 in-session 对账，保证预览只读无写副作用。
    """
    from app.models import ReconResult

    _add_scan(db_session, "T0001", "正式", "2026-04-12")  # recon_dingtalk 默认 True，触发对账
    meta = build_export_meta(
        db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001", persist=False
    )
    # meta 仍正确产出；对账判无效后该员工出勤和加班均为 0，不进入薪资表。
    assert meta["payrollSheet"]["rowCount"] == 0
    assert meta["payrollSheet"]["rows"] == []
    # 模拟预览路由：调用方 rollback 丢弃 in-session 对账变更
    db_session.rollback()
    # 对账未落库：ReconResult 无行
    assert db_session.query(ReconResult).count() == 0


def test_absence_empty_and_zero_export_as_numeric_zero_in_all_sheets():
    columns = ["姓名", "其它列", "旷工天数"]
    values = [None, "", " ", "-", 0, "0", 1.25]
    object_rows = [{"姓名": str(i), "其它列": "", "旷工天数": v} for i, v in enumerate(values)]
    object_rows.append({"姓名": "missing", "其它列": ""})
    grid_rows = [[str(i), "", v] for i, v in enumerate(values)] + [["missing", ""]]
    names = ["输出给薪资", "输出给薪资2", "班次有效工时"]
    meta = {
        "sheets": names,
        "payrollSheet": {"columns": columns, "rows": object_rows},
        "payrollSheet2": {"columns": columns, "rows": object_rows},
        "monthlyHours": {"columns": columns, "rows": grid_rows},
    }
    exports = [build_export_xlsx(meta)] + [export_service.build_export_sheet_xlsx(meta, name) for name in names]
    for data in exports:
        workbook = load_workbook(BytesIO(data))
        for sheet in workbook:
            assert [sheet.cell(row, 3).value for row in range(2, 10)] == [0, 0, 0, 0, 0, 0, 1.25, 0]
            assert all(sheet.cell(row, 3).data_type == "n" for row in range(2, 10))
            assert all(sheet.cell(row, 2).value is None for row in range(2, 10))


def test_business_trip_only_employee_preview_and_xlsx(db_session):
    # 刘殿忠案例：六天各8小时，整月合计为6天，即使没有工作点出勤也保留。
    for day in range(1, 7):
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001", kq_date=f"2026-04-{day:02d}", business_02="8.00000"
        ))
    db_session.commit()
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    for name in ("payrollSheet", "payrollSheet2"):
        assert meta[name]["rowCount"] == 1
        row = meta[name]["rows"][0]
        assert row["出差天数"] == 6
        assert row["请假总时长(天)"] in ("", 0)
        assert row["加班合计"] == 0
    workbook = load_workbook(BytesIO(build_export_xlsx(meta)))
    for name in ("输出给薪资", "输出给薪资2"):
        sheet = workbook[name]
        headers = {cell.value: cell.column for cell in sheet[1]}
        assert sheet.cell(2, headers["出差天数"]).value == 6


def test_business_trip_round_once_and_exclude_withdrawn_invalid_other_month(db_session):
    for day, hours in enumerate(["0.03", "0.03", "0.03", "NaN", "Infinity", "-8", "bad", "0"], 1):
        db_session.add(ExternalAttendanceResultRaw(
            user_code="T0001", kq_date=f"2026-04-{day:02d}", business_02=hours
        ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-09", business_02="80",
        source_withdrawn_at=datetime.now(timezone.utc),
    ))
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-03-09", business_02="80"
    ))
    db_session.commit()
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001")
    for name in ("payrollSheet", "payrollSheet2"):
        assert meta[name]["rows"][0]["出差天数"] == 0.01


def test_business_trip_not_duplicated_across_posts_or_overtime_merge(db_session):
    furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    for day, post in [("2026-04-10", "出料工"), ("2026-04-11", "物料转运工")]:
        _add_work_record(db_session, furnace, day, post)
        _add_result(db_session, day, work_hours=8)
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-04-01", business_02="12"
    ))
    db_session.commit()
    meta = build_export_meta(db_session, month="2026-04", furnaces=["35号工作点"], emp_id="T0001",
                             payroll2_group_by=["工号", "工作点", "班组", "岗位"])
    for name in ("payrollSheet", "payrollSheet2"):
        rows = meta[name]["rows"]
        assert len(rows) == 2
        assert sum(row["出差天数"] for row in rows) == 1.5
    # 出差恰好位于被合并的加班行时也不能丢失。
    rows = [dict(row) for row in meta["payrollSheet"]["rows"]]
    for row in rows:
        row["出差天数"] = 0
    rows[1].update({"实际出勤天数": 0, "加班合计": 2, "加班1": 2, "加班标准1": "物料转运工", "出差天数": 1.5})
    merged = _build_payroll2_sheet(rows, ["工号", "岗位"], merge_overtime_only_rows=True)
    assert merged["rowCount"] == 1
    assert merged["rows"][0]["出差天数"] == 1.5


def test_business_trip_belongs_only_to_dominant_furnace_batch(db_session):
    owner = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    other = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "11号工作点"))
    for furnace, day in [(owner, "2026-04-10"), (owner, "2026-04-11"), (other, "2026-04-12")]:
        _add_work_record(db_session, furnace, day, "出料工")
        _add_result(db_session, day, work_hours=8)
    db_session.add(ExternalAttendanceResultRaw(user_code="T0001", kq_date="2026-04-01", business_02="48"))
    db_session.commit()
    for furnace, expected in [(owner, 6), (other, 0)]:
        meta = build_export_meta(db_session, month="2026-04", furnaces=[furnace.furnace_no],
                                 furnace_ids=[furnace.id], emp_id="T0001")
        for name in ("payrollSheet", "payrollSheet2"):
            assert sum(row["出差天数"] for row in meta[name]["rows"]) == expected
