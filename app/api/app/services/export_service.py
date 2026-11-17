from io import BytesIO
import calendar
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_DOWN
from datetime import date as calendar_date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.payroll import (
    EMPLOY_TYPE_RATIO,
    EXPORT_SHEET_NAMES,
    EXTRA_COLUMNS,
    PAYROLL_COLUMNS,
    fold_amounts,
    mode_department,
    payroll_ratio,
    top_overtime_slots,
)
from app.models import (
    AttendanceRecord,
    ChiefConfirm,
    Employee,
    Furnace,
    ExternalAttendanceResultRaw,
    QuotaRule,
    ReconResult,
)
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active
from app.services.anomaly_service import derive_validity, get_overtime_validity
from app.services.recon_service import get_base_recon_for
from app.services.report_snapshot import ReportSnapshot
from app.services.absence_override_service import apply_absence_overrides, get_absence_override_values
from app.services.report_jobs import append_report_row, bounded_rows, check_report_deadline

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")  # 月份须 01–12，拦截 2026-13/00/99
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExportInputError(ValueError):
    """导出入参非法（月份格式错误、工作点不存在等）——上层应转 400，而非静默回退样本。"""


def _safe_sheet_title(name: str) -> str:
    title = re.sub(r'[\\/*?:\[\]]', "_", name)
    return title[:31]


def _mode_employ_type(emp_scans: list) -> str:
    """该员工当月用工类型取众数（无 scan 默认正式）。"""
    if not emp_scans:
        return "正式"
    counts: dict[str, int] = {}
    for s in emp_scans:
        counts[s.employ_type] = counts.get(s.employ_type, 0) + 1
    return max(counts, key=counts.get)


def _valid_day_fractions(db: Session, emp_id: str, emp_scans: list) -> dict[str, float]:
    """按自然日聚合出勤占比，并据 HR/值班长四态确认剔除被判「无效」的日子。
    同日多条取最大占比；无对账行=自动一致计入；对账判无效（HR/值班长不通过、
    问题日未获通过）则不计入算薪。接通 HR 四态确认 → 实际出勤天数口径。"""
    day_frac: dict[str, float] = {}
    for s in emp_scans:
        # work_hours → 出勤天占比；>8h(同意编制加班) 的出勤天占比封顶 1.0，OT 部分另计加班列
        frac = min((s.work_hours if s.work_hours is not None else 8.0), 8.0) / 8.0
        day_frac[s.natual_date] = max(day_frac.get(s.natual_date, 0.0), frac)
    if not day_frac:
        return {}
    recons = db.scalars(
        select(ReconResult).where(
            ReconResult.emp_id == emp_id,
            ReconResult.date.in_(list(day_frac.keys())),
        )
    ).all()
    recon_by_date = {r.date: r for r in recons}
    valid: dict[str, float] = {}
    for date, frac in day_frac.items():
        r = recon_by_date.get(date)
        base = get_base_recon_for(db, emp_id, date)
        ok, _reason = derive_validity(
            status=base["status"],
            hr_status=r.hr_status if r else "未确认",
            chief_status=r.chief_status if r else "未确认",
            pending_makeup=False,
        )
        if not ok:
            continue
        valid[date] = frac
    return valid


def _valid_day_fractions_bulk(
    db: Session, records: list[AttendanceRecord], context: ReportSnapshot | None = None
) -> dict[str, dict[str, float]]:
    """批量计算员工有效出勤占比，避免健康检查按员工日逐条回查数据库。"""
    if not records:
        return {}
    employee_ids = {record.emp_id for record in records}
    dates = {record.natual_date for record in records}
    fractions: dict[str, dict[str, float]] = {emp_id: {} for emp_id in employee_ids}
    for record in records:
        hours = record.work_hours if record.work_hours is not None else 8.0
        day_values = fractions[record.emp_id]
        day_values[record.natual_date] = max(
            day_values.get(record.natual_date, 0.0), min(hours, 8.0) / 8.0
        )

    context = context or ReportSnapshot(
        db, date_from=min(dates), date_to=max(dates), employee_ids=employee_ids,
    )

    for emp_id, day_values in fractions.items():
        for natural_date in list(day_values):
            ok, _reason = context.validity(emp_id, natural_date)
            if not ok:
                del day_values[natural_date]
    return fractions


def _validity_by_date(db: Session, emp_id: str, dates: set[str]) -> dict[str, bool]:
    """Return normal attendance validity per date, excluding independent overtime checks."""
    if not dates:
        return {}
    recons = db.scalars(
        select(ReconResult).where(
            ReconResult.emp_id == emp_id,
            ReconResult.date.in_(list(dates)),
        )
    ).all()
    valid = {d: True for d in dates}
    recon_by_date = {r.date: r for r in recons}
    for date in dates:
        r = recon_by_date.get(date)
        base = get_base_recon_for(db, emp_id, date)
        ok, _reason = derive_validity(
            status=base["status"],
            hr_status=r.hr_status if r else "未确认",
            chief_status=r.chief_status if r else "未确认",
            pending_makeup=False,
        )
        valid[date] = ok
    return valid


def _overtime_record_valid(
    db: Session,
    record: AttendanceRecord,
    recon_by_date: dict[tuple[str, str], ReconResult] | None = None,
    context: ReportSnapshot | None = None,
) -> bool:
    """独立加班有效性：独立打卡覆盖、人工确认或同班组有效正常班均可通过。"""
    if context is not None:
        return context.overtime_validity(record)[1]
    r = (recon_by_date or {}).get((record.emp_id, record.natual_date))
    _, ok, _reason = get_overtime_validity(db, record, r)
    return ok


def _shanghai_today() -> calendar_date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _attendance_cutoff(month: str, today: calendar_date) -> calendar_date:
    year, month_no = (int(value) for value in month.split("-"))
    return min(calendar_date(year, month_no, calendar.monthrange(year, month_no)[1]),
               today - timedelta(days=1))


def _expected_attendance_days(
    month: str, *, today: calendar_date | None = None
) -> int:
    """应出勤天数：完整月扣 4 天公休；进行中月份只计算到昨天。"""
    try:
        y, m = (int(value) for value in month.split("-"))
        ndays = calendar.monthrange(y, m)[1]
        cutoff = _attendance_cutoff(month, today or _shanghai_today())
        elapsed_days = max(0, (cutoff - calendar_date(y, m, 1)).days + 1)
        prorated_rest_days = round(4 * elapsed_days / ndays)
        return max(0, elapsed_days - prorated_rest_days)
    except (ValueError, IndexError):
        return 26


def _nonnegative_hours(value) -> Decimal:
    try:
        hours = Decimal(str(value or "0").strip())
    except InvalidOperation:
        return Decimal(0)
    return hours if hours.is_finite() and hours > 0 else Decimal(0)


def _clipped_daily_leave_hours(result, attendance_hours) -> list[Decimal]:
    """请假分配和旷工共用同一日口径：请假最多占有效出勤后的剩余小时。"""
    values = [_nonnegative_hours(getattr(result, name, 0))
              for name in ("leave_01", "leave_02", "leave_03")]
    remaining = max(Decimal(0), Decimal(8) - _nonnegative_hours(attendance_hours))
    total = sum(values, Decimal(0))
    if total > remaining:
        values = [value * remaining / total for value in values]
    return values


def _remaining_absence_hours(context, employee_id, date_key, valid_fractions_by_emp) -> Decimal:
    attendance_hours = min(Decimal(8), Decimal(8) * _nonnegative_hours(
        valid_fractions_by_emp.get(employee_id, {}).get(date_key, 0)
    ))
    leave_hours = sum(_clipped_daily_leave_hours(
        context.results.get((employee_id, date_key)), attendance_hours
    ), Decimal(0))
    return max(Decimal(0), Decimal(8) - attendance_hours - leave_hours)


def _monthly_absence_limits(context, month, valid_fractions_by_emp, employee_ids, *, today=None):
    """允许手填的整月余额：每个已过去的自然日最多8h，扣除有效出勤和请假。"""
    start = calendar_date.fromisoformat(month + "-01")
    cutoff = _attendance_cutoff(month, today or _shanghai_today())
    dates = [(start + timedelta(days=i)).isoformat()
             for i in range(max(0, (cutoff - start).days + 1))]
    limits = {}
    for employee_id in employee_ids:
        check_report_deadline()
        hours = sum((_remaining_absence_hours(context, employee_id, day, valid_fractions_by_emp)
                     for day in dates), Decimal(0))
        # 输入最多两位小数，向下取整确保不会突破真实余额。
        limits[employee_id] = (hours / Decimal(8)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return limits


def _monthly_absence_days(
    context: ReportSnapshot,
    month: str,
    valid_fractions_by_emp: dict[str, dict[str, float]],
    *,
    today: calendar_date | None = None,
) -> dict[str, float]:
    """源旷工按员工日裁剪，合计后除以 8；不以缺勤差额补造旷工。"""
    cutoff = _attendance_cutoff(month, today or _shanghai_today())
    hours_by_emp: dict[str, Decimal] = {}
    for (employee_id, date_key), result in context.results.items():
        check_report_deadline()
        if not _ISO_DATE_RE.fullmatch(str(date_key or "")):
            continue
        try:
            day = calendar_date.fromisoformat(date_key)
        except ValueError:
            continue
        if day.strftime("%Y-%m") != month or day > cutoff:
            continue
        remaining = _remaining_absence_hours(context, employee_id, date_key, valid_fractions_by_emp)
        hours = min(_nonnegative_hours(result.kggs), remaining)
        if hours > 0:
            hours_by_emp[employee_id] = hours_by_emp.get(employee_id, Decimal(0)) + hours
    return {
        employee_id: float((hours / Decimal(8)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for employee_id, hours in hours_by_emp.items()
    }


def _build_payroll_row(
    db: Session,
    emp_id: str,
    emp_scans: list,
    furnace_map: dict,
    month: str,
    absence_days: float = 0,
    valid_day_fractions: dict[str, float] | None = None,
    context: ReportSnapshot | None = None,
) -> dict:
    check_report_deadline()
    employee = (context.employees.get(emp_id) if context is not None else
                db.scalar(select(Employee).where(Employee.emp_id == emp_id)))
    # 分流出勤 / 加班记录
    work_scans = [s for s in emp_scans if getattr(s, "record_kind", "出勤") == "出勤"]
    ot_scans = [s for s in emp_scans if getattr(s, "record_kind", "出勤") == "加班"]
    valid_fracs = (
        _valid_day_fractions(db, emp_id, work_scans)
        if valid_day_fractions is None
        else valid_day_fractions
    )
    valid_dates = (
        _validity_by_date(
            db,
            emp_id,
            {s.natual_date for s in [*work_scans, *ot_scans]},
        )
        if valid_day_fractions is None
        else {
            scan.natual_date: scan.natual_date in valid_fracs
            for scan in work_scans
        }
    )
    recon_by_date = context.recons if context is not None else {
        (r.emp_id, r.date): r for r in db.scalars(select(ReconResult).where(
            ReconResult.emp_id == emp_id,
            ReconResult.date.in_({s.natual_date for s in [*work_scans, *ot_scans]}),
        ))
    }
    # 加班工时（按岗位）：出勤记录只取超 8h 的 OT 部分（整班 8h 不算加班，否则加班合计虚高）；
    # 加班记录(record_kind=加班)全段计入。无效日不计加班。
    post_hours: dict[str, float] = {}
    for s in work_scans:
        if not valid_dates.get(s.natual_date, True):
            continue
        wh = s.work_hours if s.work_hours is not None else 8.0
        ot = max(0.0, wh - 8.0)  # 整班 8h → 0；>8h 取超出部分
        if ot > 0:
            post_hours[s.post] = post_hours.get(s.post, 0.0) + ot
    for s in ot_scans:
        if not _overtime_record_valid(db, s, recon_by_date, context):
            continue
        wh = s.work_hours if s.work_hours is not None else 8.0
        post_hours[s.post] = post_hours.get(s.post, 0.0) + wh
    slots = top_overtime_slots(list(post_hours.items()))

    dept_inputs = [
        {"furnace_no": furnace_map[s.furnace_id].furnace_no, "squad": s.squad}
        for s in (work_scans or ot_scans)
        if s.furnace_id in furnace_map
    ]
    department = mode_department(dept_inputs)
    actual_days = sum(valid_fracs.values())
    expected_days = _expected_attendance_days(month)
    overtime_total = round(sum(post_hours.values()), 2)  # 加班合计=各岗工时之和

    row: dict = {col: "" for col in PAYROLL_COLUMNS}
    row["工号"] = emp_id
    row["姓名"] = employee.name if employee else emp_id
    row["部门"] = department
    row["岗位"] = (
        work_scans[0].post
        if work_scans
        else (ot_scans[0].post if ot_scans else "")
    )
    row["应出勤天数"] = expected_days
    row["实际出勤天数"] = actual_days if actual_days else 0
    # 完整薪资表生成后统一分配原始考勤结果中的员工月度旷工。
    row["旷工天数"] = absence_days
    row["出勤合计"] = actual_days if actual_days else 0
    row["出勤合计（不含年休、调休）"] = actual_days if actual_days else 0
    row["加班合计"] = overtime_total
    for i, slot in enumerate(slots, start=1):
        row[f"加班{i}"] = slot["hours"]
        row[f"加班标准{i}"] = slot["post"]

    # 用工类型取自该员工实际出勤 scan（按出现次数取众数），并按薪资比例系数折算金额列
    employ_type = _mode_employ_type(work_scans or ot_scans)
    ratio = payroll_ratio(employ_type)
    row["用工类型"] = employ_type
    row["薪资比例系数"] = ratio
    fold_amounts(row, ratio)
    return row


def _payroll_group_key(record: AttendanceRecord) -> tuple[int, str, str, str]:
    return (
        record.furnace_id,
        record.squad or "",
        record.post or "",
        record.employ_type or "正式",
    )


def _dominant_furnace_by_emp(
    records: list[AttendanceRecord],
    furnace_map: dict[int, Furnace],
    valid_fractions_by_emp: dict[str, dict[str, float]] | None = None,
) -> dict[str, int]:
    """按有效出勤天数选员工当月唯一主工作点，供月度总量跨批导出归属。"""
    fractions: dict[tuple[str, int, str], float] = {}
    for record in records:
        if record.record_kind != "出勤":
            continue
        valid_days = (
            valid_fractions_by_emp.get(record.emp_id, {})
            if valid_fractions_by_emp is not None
            else None
        )
        if valid_days is not None and record.natual_date not in valid_days:
            continue
        fraction = min(
            record.work_hours if record.work_hours is not None else 8.0,
            8.0,
        ) / 8.0
        key = (record.emp_id, record.furnace_id, record.natual_date)
        fractions[key] = max(fractions.get(key, 0.0), fraction)

    totals: dict[str, dict[int, float]] = {}
    for (employee_id, furnace_id, _day), fraction in fractions.items():
        employee_totals = totals.setdefault(employee_id, {})
        employee_totals[furnace_id] = employee_totals.get(furnace_id, 0.0) + fraction

    owners: dict[str, int] = {}
    for employee_id, furnace_totals in totals.items():
        owners[employee_id] = min(
            furnace_totals,
            key=lambda furnace_id: (
                -furnace_totals[furnace_id],
                furnace_map.get(furnace_id).region if furnace_map.get(furnace_id) else "",
                _furnace_sort_key(
                    furnace_map.get(furnace_id).furnace_no
                    if furnace_map.get(furnace_id)
                    else ""
                ),
                furnace_id,
            ),
        )
    return owners


def _build_payroll_rows(
    db: Session,
    emp_id: str,
    emp_scans: list,
    furnace_map: dict,
    month: str,
    leave_by_group: dict[tuple[int, str, str, str], dict[str, float]] | None = None,
    valid_day_fractions: dict[str, float] | None = None,
    context: ReportSnapshot | None = None,
) -> list[dict]:
    """输出给薪资按员工 + 工作点 + 班组 + 岗位 + 用工类型拆行。"""
    if not emp_scans:
        return [
            _build_payroll_row(
                db,
                emp_id,
                [],
                furnace_map,
                month,
                0,
                valid_day_fractions={},
                context=context,
            )
        ]

    grouped: dict[tuple[int, str, str, str], list[AttendanceRecord]] = {}
    for scan in emp_scans:
        grouped.setdefault(_payroll_group_key(scan), []).append(scan)

    rows: list[dict] = []
    for group_key, group_scans in sorted(
        grouped.items(),
        key=lambda item: (
            furnace_map.get(item[0][0]).region if furnace_map.get(item[0][0]) else "",
            _furnace_sort_key(
                furnace_map.get(item[0][0]).furnace_no
                if furnace_map.get(item[0][0])
                else ""
            ),
            item[0][1],
            item[0][2],
            item[0][3],
        ),
    ):
        group_valid_fractions: dict[str, float] | None = None
        if valid_day_fractions is not None:
            group_valid_fractions = {}
            for scan in group_scans:
                if (
                    scan.record_kind != "出勤"
                    or scan.natual_date not in valid_day_fractions
                ):
                    continue
                fraction = min(
                    scan.work_hours if scan.work_hours is not None else 8.0,
                    8.0,
                ) / 8.0
                group_valid_fractions[scan.natual_date] = max(
                    group_valid_fractions.get(scan.natual_date, 0.0), fraction
                )
        row = _build_payroll_row(
            db,
            emp_id,
            group_scans,
            furnace_map,
            month,
            0,
            valid_day_fractions=group_valid_fractions,
            context=context,
        )
        leave = (leave_by_group or {}).get(group_key)
        if leave:
            for column in PAYROLL2_LEAVE_COLUMNS:
                row[column] = leave.get(column, "")
        rows.append(row)
    return rows


def _apply_employee_absence(
    payroll_rows: list[dict],
    leave_totals_by_emp: dict[str, dict[str, float]],
    absence_days_by_emp: dict[str, float],
    eligible_employee_ids: set[str] | None = None,
) -> None:
    """按员工计算旷工总量，并且只落在一个主要岗位行，避免拆行后重复。"""
    rows_by_emp: dict[str, list[dict]] = {}
    for row in payroll_rows:
        employee_id = str(row.get("工号") or "")
        rows_by_emp.setdefault(employee_id, []).append(row)
    for employee_id, rows in rows_by_emp.items():
        if eligible_employee_ids is not None and employee_id not in eligible_employee_ids:
            _assign_absence_to_primary_row(rows, 0)
            continue
        _assign_unallocated_leave_to_primary_row(
            rows, leave_totals_by_emp.get(employee_id, {})
        )
        absence_days = absence_days_by_emp.get(employee_id, 0)
        _assign_absence_to_primary_row(rows, absence_days)


def _assign_unallocated_leave_to_primary_row(
    rows: list[dict], leave_totals: dict[str, float]
) -> None:
    """无法按出勤岗位锚定的请假仍落到员工主要行，保证请假与旷工口径闭合。"""
    if not rows:
        return
    _, target = max(
        enumerate(rows),
        key=lambda item: (
            _as_float(item[1].get("实际出勤天数")),
            _as_float(item[1].get("请假总时长(天)")),
            -item[0],
        ),
    )
    for column in PAYROLL2_LEAVE_COLUMNS:
        assigned = sum(_as_float(row.get(column)) for row in rows)
        missing = max(0.0, _as_float(leave_totals.get(column)) - assigned)
        if missing:
            target[column] = _sum_number([_as_float(target.get(column)), missing])


def _assign_absence_to_primary_row(rows: list[dict], absence_days: float) -> None:
    """员工级旷工无可归岗日期，只在主要岗位行展示一次。

    主要岗位按实际出勤天数最多确定；并列时优先已承接请假的行，仍并列则沿用
    上游稳定排序的第一行。这样各拆分行相加仍等于员工月度旷工总量。
    """
    if not rows:
        return
    for row in rows:
        row["旷工天数"] = 0
    _, target = max(
        enumerate(rows),
        key=lambda item: (
            _as_float(item[1].get("实际出勤天数")),
            _as_float(item[1].get("请假总时长(天)")),
            -item[0],
        ),
    )
    target["旷工天数"] = absence_days


def _assign_business_trip_to_primary_row(rows: list[dict], days: float) -> None:
    """员工月度出差只展示一次；岗位拆分、重新分组后仍保持总量。"""
    if not rows:
        return
    for row in rows:
        row["出差天数"] = 0
    _, target = max(enumerate(rows), key=lambda item: (
        _as_float(item[1].get("实际出勤天数")),
        _as_float(item[1].get("请假总时长(天)")),
        -item[0],
    ))
    target["出差天数"] = days


def _apply_monthly_business_trip(rows, context, month, eligible_employee_ids) -> None:
    """business_02 为出差小时数，按月先合计再除8，最多两位小数。"""
    hours_by_emp: dict[str, Decimal] = {}
    for result in context.results.values():
        check_report_deadline()
        # ReportSnapshot 已在查询时排除源端撤回记录。
        if result.user_code not in eligible_employee_ids:
            continue
        try:
            day = calendar_date.fromisoformat(str(result.kq_date))
        except (ValueError, TypeError):
            continue
        if day.strftime("%Y-%m") != month:
            continue
        hours_by_emp[result.user_code] = hours_by_emp.get(
            result.user_code, Decimal(0)
        ) + _nonnegative_hours(result.business_02)
    rows_by_emp: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_emp.setdefault(str(row.get("工号") or ""), []).append(row)
    for employee_id, employee_rows in rows_by_emp.items():
        days = (hours_by_emp.get(employee_id, Decimal(0)) / Decimal(8)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        _assign_business_trip_to_primary_row(employee_rows, float(days))


def _has_payroll_attendance_or_overtime(row: dict) -> bool:
    """薪资表保留有实际出勤、加班、请假或原始旷工的行。"""
    return (
        _as_float(row.get("实际出勤天数")) > 0
        or _as_float(row.get("旷工天数")) > 0
        or _as_float(row.get("加班合计")) > 0
        or _as_float(row.get("出差天数")) > 0
        or any(_as_float(row.get(column)) > 0 for column in PAYROLL2_LEAVE_COLUMNS)
    )


def _filter_payroll_rows(rows: list[dict], retained_employee_ids: set[str]) -> list[dict]:
    """自动旷工或手动值（含0）存在时至少保留一个可见行，预览与下载共用。"""
    kept = [row for row in rows if _has_payroll_attendance_or_overtime(row) or _has_display_leave(row)]
    present = {str(row.get("工号") or "") for row in kept}
    extra_ids = set()
    for row in rows:
        employee_id = str(row.get("工号") or "")
        if employee_id in retained_employee_ids and employee_id not in present:
            extra_ids.add(id(row))
            present.add(employee_id)
    return [row for row in rows if _has_payroll_attendance_or_overtime(row) or _has_display_leave(row) or id(row) in extra_ids]


ANOMALY_SHEET_COLUMNS = ["类型", "工号", "姓名", "日期", "对账状态", "HR确认", "值班长确认", "是否有效", "理由"]
TRIPARTITE_BASE_COLUMNS = ["工号", "姓名", "日期", "工作点", "岗位", "班次", "扫码", "钉钉打卡", "补录"]
TRIPARTITE_TAIL_COLUMNS = ["对账结论"]


def _overtime_slot_columns(count: int) -> list[str]:
    cols: list[str] = []
    for i in range(1, count + 1):
        cols.extend([f"加班{i}", f"加班标准{i}"])
    return cols


def _payroll_columns(overtime_slot_count: int) -> list[str]:
    insert_after = "承包工资"
    base = list(PAYROLL_COLUMNS)
    try:
        idx = base.index(insert_after) + 1
    except ValueError:
        idx = len(base)
    return base[:idx] + _overtime_slot_columns(overtime_slot_count) + base[idx:] + list(EXTRA_COLUMNS)


def _payroll_overtime_slot_count(rows: list[dict]) -> int:
    count = 0
    for row in rows:
        i = 1
        while f"加班{i}" in row or f"加班标准{i}" in row:
            count = max(count, i)
            i += 1
    return count


PAYROLL2_EMPTY_COLUMNS = [
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


PAYROLL2_AFTER_OVERTIME_TOTAL_EMPTY_COLUMNS = [
    "核对",
    "上月薪资标准",
    "本月调整",
    "差异",
    "薪资标准",
    "承包工资",
]


PAYROLL2_BEFORE_EMPLOY_TYPE_EMPTY_COLUMNS = [
    "节假日出勤天数",
    "公休未休天数（天）",
]


PAYROLL2_BASE_COLUMNS = [
    "工号",
    "姓名",
    "部门",
    "用工类型",
    "薪资比例系数",
    "应出勤天数",
    "实际出勤天数",
    "旷工天数",
    *PAYROLL2_EMPTY_COLUMNS,
    "出勤合计",
    "出勤合计（不含年休、调休）",
    "加班合计",
    *PAYROLL2_AFTER_OVERTIME_TOTAL_EMPTY_COLUMNS,
]


def _as_float(value) -> float:
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum_number(values: list[float]) -> float | int:
    # Python 3.11 has float.is_integer(), but not int.is_integer().  sum() and
    # round() preserve int when every input is integral, so normalize first.
    total = round(float(sum(values)), 2)
    return int(total) if total.is_integer() else total


PAYROLL2_LEAVE_COLUMNS = (
    "请假总时长(天)",
    "年假(天)",
    "调休(天)",
    "事假(天)",
)

# 仅展示的预留列：上游单位为小时，月度合计 / 8 转为天。
# 不加入 PAYROLL2_LEAVE_COLUMNS，避免进入有效请假、旷工抵扣或薪资计算。
PAYROLL2_DISPLAY_LEAVE_FIELDS = {
    "病假(天)": "leave_05",
    "婚假(天)": "leave_07",
    "产假(天)": "leave_09",
    "陪产假(天)": "leave_10",
    "丧假(天)": "leave_12",
    "工伤假(天)": "leave_13",
}


def _payroll_display_leave_totals(
    db: Session, emp_ids: list[str], month: str, context: ReportSnapshot | None = None
) -> dict[str, dict[str, float | int]]:
    """只汇总展示用假期，不做出勤重叠裁剪或参与有效请假分配。"""
    if not emp_ids:
        return {}
    hours_by_emp: dict[str, dict[str, list[float]]] = {}
    results = (context.results.values() if context is not None else db.scalars(
        select(ExternalAttendanceResultRaw).where(
            ExternalAttendanceResultRaw.user_code.in_(emp_ids),
            ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
            ExternalAttendanceResultRaw.kq_date.startswith(month),
        )
    ).all())
    employee_ids = set(emp_ids)
    for result in results:
        check_report_deadline()
        if result.user_code not in employee_ids or not result.kq_date.startswith(month):
            continue
        if not _ISO_DATE_RE.fullmatch(result.kq_date):
            continue
        try:
            calendar_date.fromisoformat(result.kq_date)
        except ValueError:
            continue
        values = hours_by_emp.setdefault(result.user_code, {})
        for column, field in PAYROLL2_DISPLAY_LEAVE_FIELDS.items():
            values.setdefault(column, []).append(_as_float(getattr(result, field)))
    return {
        employee_id: {
            column: _sum_number([sum(hours) / 8])
            for column, hours in values.items()
        }
        for employee_id, values in hours_by_emp.items()
    }


def _apply_display_leave(
    rows: list[dict], totals_by_emp: dict[str, dict[str, float | int]]
) -> None:
    """按员工只填主要行一次；分组改变时重新选择展示行，不参与业务计算。"""
    rows_by_emp: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_emp.setdefault(str(row.get("工号") or ""), []).append(row)
    for employee_id, employee_rows in rows_by_emp.items():
        values = totals_by_emp.get(employee_id)
        if not values:
            continue
        target = max(
            employee_rows,
            key=lambda row: (
                _as_float(row.get("实际出勤天数")),
                _as_float(row.get("请假总时长(天)")),
            ),
        )
        for column in PAYROLL2_DISPLAY_LEAVE_FIELDS:
            target[column] = values.get(column) or ""


def _has_display_leave(row: dict) -> bool:
    return any(_as_float(row.get(column)) > 0 for column in PAYROLL2_DISPLAY_LEAVE_FIELDS)



def _leave_values(values: list[float]) -> dict[str, float]:
    days = [_sum_number([value / 8]) for value in values]
    return {
        "请假总时长(天)": _sum_number(days),
        "年假(天)": days[0],
        "调休(天)": days[1],
        "事假(天)": days[2],
    }


def _add_leave_values(target: dict[str, float], values: dict[str, float]) -> None:
    for column in PAYROLL2_LEAVE_COLUMNS:
        target[column] = _as_float(target.get(column)) + _as_float(values.get(column))


def _payroll_leave_allocation(
    db: Session,
    emp_ids: list[str],
    month: str,
    work_records: list[AttendanceRecord],
    furnace_map: dict[int, Furnace],
    valid_fractions_by_emp: dict[str, dict[str, float]] | None = None,
    owner_furnace_by_emp: dict[str, int] | None = None,
    context: ReportSnapshot | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[tuple[int, str, str, str], dict[str, float]]]]:
    """按连续请假段将 01/02/03 分配到相邻的实际出勤岗位。"""
    if not emp_ids:
        return {}, {}

    # 每个员工每天的有效岗位候选；同日同岗位多条记录先合并工时。
    candidates: dict[str, dict[str, dict[tuple[int, str, str, str], dict[str, float | int]]]] = {}
    records_by_emp: dict[str, list[AttendanceRecord]] = {}
    for record in work_records:
        owner_furnace = (owner_furnace_by_emp or {}).get(record.emp_id)
        if (
            record.emp_id in emp_ids
            and record.record_kind == "出勤"
            and (owner_furnace is None or record.furnace_id == owner_furnace)
        ):
            records_by_emp.setdefault(record.emp_id, []).append(record)
    for employee_id, records in records_by_emp.items():
        valid = (
            {date: True for date in valid_fractions_by_emp.get(employee_id, {})}
            if valid_fractions_by_emp is not None
            else _validity_by_date(db, employee_id, {record.natual_date for record in records})
        )
        for record in records:
            # A supplied shared-validity set is authoritative: dates absent from it
            # were rejected by reconciliation and must not receive leave allocation.
            if not valid.get(record.natual_date, valid_fractions_by_emp is None):
                continue
            group_key = _payroll_group_key(record)
            day_groups = candidates.setdefault(employee_id, {}).setdefault(record.natual_date, {})
            group = day_groups.setdefault(
                group_key,
                {"hours": 0.0, "record_id": record.id or 0},
            )
            group["hours"] = _as_float(group["hours"]) + min(
                record.work_hours if record.work_hours is not None else 8.0,
                8.0,
            )
            group["record_id"] = min(int(group["record_id"]), record.id or 0)

    def best_group(
        employee_id: str, day: str
    ) -> tuple[int, str, str, str] | None:
        groups = candidates.get(employee_id, {}).get(day, {})
        if not groups:
            return None

        def rank(item):
            group_key, info = item
            furnace = furnace_map.get(group_key[0])
            return (
                -_as_float(info["hours"]),
                furnace.region if furnace else "",
                _furnace_sort_key(furnace.furnace_no if furnace else ""),
                group_key[1],
                group_key[2],
                int(info["record_id"]),
            )

        return sorted(groups.items(), key=rank)[0][0]

    daily_leave: dict[str, dict[str, list[float]]] = {}
    results = (list(context.results.values()) if context is not None else db.scalars(
        select(ExternalAttendanceResultRaw).where(
            ExternalAttendanceResultRaw.user_code.in_(emp_ids),
            ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
            ExternalAttendanceResultRaw.kq_date.startswith(month),
        )
    ).all())
    employee_id_set = set(emp_ids)
    for result in results:
        check_report_deadline()
        if result.user_code not in employee_id_set:
            continue
        date_key = str(result.kq_date or "")
        if not _ISO_DATE_RE.fullmatch(date_key):
            continue
        try:
            calendar_date.fromisoformat(date_key)
        except ValueError:
            continue
        if valid_fractions_by_emp is not None:
            attendance_hours = 8.0 * valid_fractions_by_emp.get(
                result.user_code, {}
            ).get(result.kq_date, 0.0)
        else:
            attendance_hours = max(
                [_as_float(info["hours"]) for info in candidates.get(
                    result.user_code, {}
                ).get(result.kq_date, {}).values()] or [0.0]
            )
        values = [float(value) for value in _clipped_daily_leave_hours(result, attendance_hours)]
        if any(values):
            daily_leave.setdefault(result.user_code, {})[result.kq_date] = values

    totals: dict[str, dict[str, float]] = {
        employee_id: _leave_values(
            [sum(values[index] for values in days.values()) for index in range(3)]
        )
        for employee_id, days in daily_leave.items()
    }
    allocations: dict[
        str, dict[tuple[int, str, str, str], dict[str, float]]
    ] = {}
    for employee_id, days in daily_leave.items():
        ordered_days = sorted(days, key=calendar_date.fromisoformat)
        segments: list[list[str]] = []
        for day in ordered_days:
            if not segments or (
                calendar_date.fromisoformat(day)
                - calendar_date.fromisoformat(segments[-1][-1])
            ).days != 1:
                segments.append([day])
            else:
                segments[-1].append(day)

        attendance_days = sorted(
            candidates.get(employee_id, {}), key=calendar_date.fromisoformat
        )
        for segment in segments:
            # 半天出勤半天请假：优先当天岗位；多日都有出勤时取最接近复工的最后一天。
            same_day = [day for day in segment if day in candidates.get(employee_id, {})]
            target_day = same_day[-1] if same_day else next(
                (day for day in attendance_days if day > segment[-1]),
                None,
            )
            if target_day is None:
                target_day = next(
                    (day for day in reversed(attendance_days) if day < segment[0]),
                    None,
                )
            target_group = best_group(employee_id, target_day) if target_day else None
            if target_group is None:
                continue
            segment_hours = [sum(days[day][i] for day in segment) for i in range(3)]
            target = allocations.setdefault(employee_id, {}).setdefault(target_group, {})
            _add_leave_values(target, _leave_values(segment_hours))

    for employee_allocations in allocations.values():
        for values in employee_allocations.values():
            for column in PAYROLL2_LEAVE_COLUMNS:
                values[column] = _sum_number([_as_float(values.get(column))])
    return totals, allocations


PAYROLL2_GROUP_FIELDS = ("工号", "工作点", "班组", "岗位", "用工类型")
DEFAULT_PAYROLL2_GROUP_BY = ("工号", "工作点", "班组", "用工类型")


def _normalize_payroll2_group_by(group_by: list[str] | None) -> list[str]:
    fields = [f for f in (group_by or list(DEFAULT_PAYROLL2_GROUP_BY)) if f in PAYROLL2_GROUP_FIELDS]
    if "工号" not in fields:
        fields.insert(0, "工号")
    return list(dict.fromkeys(fields))


def _split_department(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    match = re.match(r"^(.+?)([^号工作点]*班)$", value)
    if match:
        return match.group(1), match.group(2)
    return value, ""


def _payroll2_row_dims(row: dict) -> dict[str, str]:
    furnace, squad = _split_department(str(row.get("部门") or ""))
    return {
        "工号": str(row.get("工号") or ""),
        "工作点": furnace,
        "班组": squad,
        "岗位": str(row.get("岗位") or ""),
        "用工类型": str(row.get("用工类型") or ""),
    }


def _payroll2_columns(
    post_slot_count: int,
    overtime_slot_count: int,
    *,
    group_posts: bool = False,
) -> list[str]:
    trailing_columns = [
        *PAYROLL2_BEFORE_EMPLOY_TYPE_EMPTY_COLUMNS,
        "用工类型",
        "薪资比例系数",
    ]
    base_columns = [
        col
        for col in PAYROLL2_BASE_COLUMNS
        if col not in ("用工类型", "薪资比例系数")
    ]
    if group_posts:
        # 岗位作为合并维度时，每行只有一个明确岗位；紧跟部门展示。
        base_columns.insert(base_columns.index("部门") + 1, "岗位")
    post_cols: list[str] = []
    for i in range(1, post_slot_count + 1):
        post_cols.extend([f"岗位{i}", f"出勤天数{i}"])
    return (
        ["行名"]
        + base_columns
        + post_cols
        + _overtime_slot_columns(overtime_slot_count)
        + trailing_columns
    )


def _payroll2_post_label(dims: dict[str, str], group_fields: list[str]) -> str:
    parts: list[str] = []
    if "工作点" not in group_fields and dims.get("工作点"):
        parts.append(dims["工作点"])
    if "班组" not in group_fields and dims.get("班组"):
        parts.append(dims["班组"])
    if dims.get("岗位"):
        parts.append(dims["岗位"])
    return " / ".join(parts)


def _payroll2_department_label(dims: dict[str, str], group_fields: list[str], fallback: str) -> str:
    if "工作点" in group_fields and "班组" in group_fields:
        return f"{dims.get('工作点', '')}{dims.get('班组', '')}" or fallback
    if "工作点" in group_fields:
        return dims.get("工作点") or fallback
    if "班组" in group_fields:
        return dims.get("班组") or fallback
    return fallback


def _payroll2_merge_target(
    source: dict, row_items: list[dict], *, same_squad_only: bool = False
) -> dict | None:
    """为零出勤加班行寻找有效出勤目标；可选严格限制在同工作点班组。"""
    source_dims = source["dims"]
    emp_id = source_dims.get("工号")
    furnace = source_dims.get("工作点")
    squad = source_dims.get("班组")
    employ_type = source_dims.get("用工类型")
    if not emp_id or (same_squad_only and (not furnace or not squad)):
        return None
    candidates = [
        item for item in row_items
        if item is not source
        and item["dims"].get("工号") == emp_id
        and _as_float(item["row"].get("实际出勤天数")) > 0
    ]
    if same_squad_only:
        return next((
            item for item in candidates
            if item["dims"].get("工作点") == furnace
            and item["dims"].get("班组") == squad
            and item["dims"].get("用工类型") == employ_type
        ), None)

    post = source_dims.get("岗位")
    matchers = (
        lambda dims: bool(furnace and post) and dims.get("工作点") == furnace
        and dims.get("岗位") == post and dims.get("用工类型") == employ_type,
        lambda dims: bool(furnace) and dims.get("工作点") == furnace
        and dims.get("用工类型") == employ_type,
        lambda dims: dims.get("用工类型") == employ_type,
        lambda _dims: True,
    )
    for matches in matchers:
        target = next((item for item in candidates if matches(item["dims"])), None)
        if target is not None:
            return target
    return None


def _append_payroll2_overtime(target: dict, source: dict) -> None:
    """把来源行的动态加班槽追加到目标行，并累加加班合计。"""
    last_used_slot = 0
    target_slot = 1
    while f"加班{target_slot}" in target or f"加班标准{target_slot}" in target:
        if target.get(f"加班{target_slot}") not in ("", None) or target.get(
            f"加班标准{target_slot}"
        ) not in ("", None):
            last_used_slot = target_slot
        target_slot += 1
    # 行对象会预先带上全表动态列的空键，不能仅按键数量判断已有槽位。
    next_slot = last_used_slot + 1
    source_slot = 1
    while f"加班{source_slot}" in source or f"加班标准{source_slot}" in source:
        hours = source.get(f"加班{source_slot}", "")
        post = source.get(f"加班标准{source_slot}", "")
        if hours not in ("", None) or post not in ("", None):
            target[f"加班{next_slot}"] = hours
            target[f"加班标准{next_slot}"] = post
            next_slot += 1
        source_slot += 1
    target["加班合计"] = _sum_number(
        [_as_float(target.get("加班合计")), _as_float(source.get("加班合计"))]
    )


def _merge_payroll2_overtime_only_rows(
    row_items: list[dict], *, same_squad_only: bool = False
) -> list[dict]:
    """合并零出勤但有加班的行；无有效出勤目标时保留来源行。"""
    removed: set[int] = set()
    for source in row_items:
        row = source["row"]
        if (
            _as_float(row.get("实际出勤天数")) != 0
            or _as_float(row.get("加班合计")) <= 0
        ):
            continue
        target = _payroll2_merge_target(
            source, row_items, same_squad_only=same_squad_only
        )
        if target is None:
            continue
        _append_payroll2_overtime(target["row"], row)
        removed.add(id(source))
    return [item for item in row_items if id(item) not in removed]


def _build_payroll2_sheet(
    payroll_rows: list[dict],
    group_by: list[str] | None = None,
    *,
    display_leave_totals_by_emp: dict[str, dict[str, float | int]] | None = None,
    include_zero_rows: bool = False,
    merge_overtime_only_rows: bool = False,
    merge_same_squad_overtime_only_rows: bool = False,
    absence_eligible_employee_ids: set[str] | None = None,
    absence_row_employee_ids: set[str] | None = None,
) -> dict:
    """输出给薪资2：按员工+部门+用工类型合并，将多个岗位横向展开。"""
    group_fields = _normalize_payroll2_group_by(group_by)
    group_posts = "岗位" in group_fields
    groups: dict[tuple[str, ...], dict] = {}
    order: list[tuple[str, ...]] = []
    for row in payroll_rows:
        dims = _payroll2_row_dims(row)
        key = tuple(dims[field] for field in group_fields)
        if key not in groups:
            order.append(key)
            groups[key] = {
                "base": {col: row.get(col, "") for col in PAYROLL2_BASE_COLUMNS},
                "dims": dims,
                "row_name": "/".join(dims[field] or "-" for field in group_fields),
                "post_days": {},
                "overtime": {},
                "actual_days": [],
                "attendance_total": [],
                "attendance_total_no_leave": [],
                "overtime_total": [],
                "leave": {column: [] for column in PAYROLL2_LEAVE_COLUMNS},
        }
        g = groups[key]
        post = _payroll2_post_label(dims, group_fields)
        post_days = _as_float(row.get("实际出勤天数"))
        if post and not group_posts and post_days > 0:
            g["post_days"][post] = g["post_days"].get(post, 0.0) + post_days
        g["actual_days"].append(_as_float(row.get("实际出勤天数")))
        g["attendance_total"].append(_as_float(row.get("出勤合计")))
        g["attendance_total_no_leave"].append(_as_float(row.get("出勤合计（不含年休、调休）")))
        g["overtime_total"].append(_as_float(row.get("加班合计")))
        for column in PAYROLL2_LEAVE_COLUMNS:
            g["leave"][column].append(_as_float(row.get(column)))
        i = 1
        while f"加班{i}" in row or f"加班标准{i}" in row:
            post_name = str(row.get(f"加班标准{i}") or "")
            if post_name:
                g["overtime"][post_name] = g["overtime"].get(post_name, 0.0) + _as_float(row.get(f"加班{i}"))
            i += 1

    post_slot_count = 0 if group_posts else max((len(g["post_days"]) for g in groups.values()), default=0)
    overtime_slot_count = max((len(g["overtime"]) for g in groups.values()), default=0)
    columns = _payroll2_columns(
        post_slot_count, overtime_slot_count, group_posts=group_posts
    )
    row_items: list[dict] = []
    for key in order:
        g = groups[key]
        out = {col: "" for col in columns}
        out.update(g["base"])
        out["行名"] = g["row_name"]
        out["部门"] = _payroll2_department_label(g["dims"], group_fields, str(out.get("部门") or ""))
        for col in (
            *PAYROLL2_EMPTY_COLUMNS,
            *PAYROLL2_AFTER_OVERTIME_TOTAL_EMPTY_COLUMNS,
            *PAYROLL2_BEFORE_EMPLOY_TYPE_EMPTY_COLUMNS,
        ):
            out[col] = ""
        if group_posts:
            out["岗位"] = g["dims"].get("岗位", "")
        out["实际出勤天数"] = _sum_number(g["actual_days"])
        out["出勤合计"] = _sum_number(g["attendance_total"])
        out["出勤合计（不含年休、调休）"] = _sum_number(g["attendance_total_no_leave"])
        out["加班合计"] = _sum_number(g["overtime_total"])
        for column in PAYROLL2_LEAVE_COLUMNS:
            leave_days = _sum_number(g["leave"][column])
            out[column] = leave_days if leave_days else ""
        for i, (post, days) in enumerate(
            sorted(g["post_days"].items(), key=lambda item: (-item[1], item[0])),
            start=1,
        ):
            out[f"岗位{i}"] = post
            out[f"出勤天数{i}"] = _sum_number([days])
        for i, (post, hours) in enumerate(
            sorted(g["overtime"].items(), key=lambda item: (-item[1], item[0])),
            start=1,
        ):
            out[f"加班{i}"] = _sum_number([hours])
            out[f"加班标准{i}"] = post
        row_items.append({"row": out, "dims": g["dims"]})
    if merge_same_squad_overtime_only_rows:
        row_items = _merge_payroll2_overtime_only_rows(
            row_items, same_squad_only=True
        )
    if merge_overtime_only_rows:
        row_items = _merge_payroll2_overtime_only_rows(row_items)

    # 从薪资明细承接出差总量，避免分组或合并加班行后丢失。
    trip_days_by_emp: dict[str, float] = {}
    for row in payroll_rows:
        employee_id = str(row.get("工号") or "")
        trip_days_by_emp[employee_id] = trip_days_by_emp.get(employee_id, 0) + _as_float(row.get("出差天数"))

    # 从薪资明细承接原始旷工总量，重新分组后仍只展示一次。
    absence_days_by_emp: dict[str, float] = {}
    for row in payroll_rows:
        employee_id = str(row.get("工号") or "")
        absence_days_by_emp[employee_id] = (
            absence_days_by_emp.get(employee_id, 0) + _as_float(row.get("旷工天数"))
        )
    employee_items: dict[str, list[dict]] = {}
    for item in row_items:
        employee_items.setdefault(item["dims"].get("工号", ""), []).append(item)
    for employee_id, items in employee_items.items():
        _assign_business_trip_to_primary_row(
            [item["row"] for item in items],
            _sum_number([trip_days_by_emp.get(employee_id, 0)])
            if absence_eligible_employee_ids is None or employee_id in absence_eligible_employee_ids else 0,
        )
        if (
            absence_eligible_employee_ids is not None
            and employee_id not in absence_eligible_employee_ids
        ):
            _assign_absence_to_primary_row([item["row"] for item in items], 0)
            continue
        absence_days = absence_days_by_emp.get(employee_id, 0)
        _assign_absence_to_primary_row(
            [item["row"] for item in items], absence_days
        )
    # 所有业务计算及拆行合并完成后再填展示列。每位员工只写主要行一次，
    # 不改变有效请假总量、旷工行选择或加班合并判断。
    rows = [item["row"] for item in row_items]
    _apply_display_leave(rows, display_leave_totals_by_emp or {})
    if not include_zero_rows:
        rows = _filter_payroll_rows(rows, absence_row_employee_ids or set())
    overtime_slot_count = _payroll_overtime_slot_count(rows)
    columns = _payroll2_columns(
        post_slot_count, overtime_slot_count, group_posts=group_posts
    )
    rows = [{col: row.get(col, "") for col in columns} for row in rows]
    return {
        "name": "输出给薪资2",
        "groupBy": group_fields,
        "columnCount": len(columns),
        "columns": columns,
        "sampleRow": rows[0] if rows else {col: "" for col in columns},
        "rows": rows,
        "rowCount": len(rows),
    }


def _anomaly_sheet_rows(db: Session, emp_ids: list[str], month: str, context: ReportSnapshot) -> list[list]:
    """打卡异常表：基础出勤与独立加班分开输出，避免加班状态污染基础出勤。"""
    if not emp_ids:
        return []
    names = {key: value.name for key, value in context.employees.items()}
    recons = sorted(context.recons.values(), key=lambda r: (r.date, r.emp_id))
    recon_by_date = context.recons
    out: list[list] = []
    for r in recons:
        base = context.base(r.emp_id, r.date)
        ok, reason = derive_validity(
            status=base["status"], hr_status=r.hr_status, chief_status=r.chief_status, pending_makeup=False
        )
        if base["status"] != "自动一致" or not ok:
            out.append([
                "正常出勤", r.emp_id, names.get(r.emp_id, r.emp_id), r.date, base["status"],
                r.hr_status, r.chief_status, "有效" if ok else "无效", reason,
            ])
    overtimes = [r for r in context.records if r.status == "active"
                 and r.record_kind == ATTENDANCE_KIND_OVERTIME]
    for ot in sorted(overtimes, key=lambda x: (x.natual_date, x.emp_id, x.id)):
        r = recon_by_date.get((ot.emp_id, ot.natual_date))
        status, ok, reason = context.overtime_validity(ot)
        if status != "自动一致" or not ok:
            out.append([
                "加班", ot.emp_id, names.get(ot.emp_id, ot.emp_id), ot.natual_date, status,
                r.hr_status if r else "未确认",
                r.chief_status if r else "未确认",
                "有效" if ok else "无效",
                reason,
            ])
    return out


def _tripartite_sheet(
    db: Session, emp_ids: list[str], month: str, furnace_map: dict, context: ReportSnapshot
) -> dict:
    """三方对账表：每个(员工,日期)并排展示 工作点扫码 / 钉钉打卡 / 补录 三源 + 对账结论。"""
    if not emp_ids:
        return {"columns": [*TRIPARTITE_BASE_COLUMNS, *TRIPARTITE_TAIL_COLUMNS], "rows": []}
    names = {key: value.name for key, value in context.employees.items()}
    scans = [r for r in context.records if r.status == "active" and r.record_kind == "出勤"]
    punches = [p for (_, day), values in context.punches_by_key.items()
               if day.startswith(month) for p in values]
    makeups = [r for r in context.records if r.source in ("补录", "代签")
               and r.status in ("待审", "拒绝")]
    recons = context.recons
    overtimes = [r for r in context.records if r.status == "active"
                 and r.record_kind == ATTENDANCE_KIND_OVERTIME]
    ot_by: dict[tuple, list] = {}
    for o in overtimes:
        ot_by.setdefault((o.emp_id, o.natual_date), []).append(o)

    scan_by = {(s.emp_id, s.natual_date): s for s in scans}
    punch_cnt: dict[tuple, int] = {}
    for p in punches:
        punch_cnt[(p.emp_id, p.natual_date)] = punch_cnt.get((p.emp_id, p.natual_date), 0) + 1
    makeup_by = {(m.emp_id, m.natual_date): m for m in makeups}

    keys = sorted(set(scan_by) | set(punch_cnt.keys()) | set(makeup_by) | set(ot_by))
    raw_rows: list[tuple[list, list, str]] = []
    max_ot = 0
    for emp, date in keys:
        s = scan_by.get((emp, date))
        f = furnace_map.get(s.furnace_id) if s else None
        pc = punch_cnt.get((emp, date), 0)
        m = makeup_by.get((emp, date))
        ots = ot_by.get((emp, date), [])
        max_ot = max(max_ot, len(ots))
        ot_cells: list = []
        for o in sorted(
            ots,
            key=lambda x: (
                _furnace_sort_key(furnace_map.get(x.furnace_id).furnace_no if furnace_map.get(x.furnace_id) else ""),
                x.squad or "",
                x.post or "",
            ),
        ):
            of = furnace_map.get(o.furnace_id)
            fn = of.furnace_no if of else ""
            squad = o.squad or "不分班"
            label = f"{fn}{squad}{o.post}"
            ot_cells.extend([o.work_hours if o.work_hours is not None else 8.0, label])
        base_status = context.base(emp, date)["status"] if (emp, date) in recons else "—"
        raw_rows.append(([
            emp, names.get(emp, emp), date,
            (f.furnace_no if f else "") if s else "",
            s.post if s else "",
            (s.squad or "不分班") if s else "",
            "有" if s else "无",
            f"{pc}次" if pc else "无",
            (f"{m.source or '补录'}/{m.status}" if m else "无"),
        ], ot_cells, base_status))
    columns = [*TRIPARTITE_BASE_COLUMNS, *_overtime_slot_columns(max_ot), *TRIPARTITE_TAIL_COLUMNS]
    rows: list[list] = []
    for base, ot_cells, recon in raw_rows:
        padded = ot_cells + [""] * (max_ot * 2 - len(ot_cells))
        rows.append([*base, *padded, recon])
    return {"columns": columns, "rows": rows}


# 劳勤工作台「班次有效工时」月度合并导出列：
# 同 工号+工时类型+地区+工作点编号+岗位 合并为一条
MONTHLY_HOURS_COLUMNS = [
    "工号", "姓名", "工时类型", "地区", "工作点编号", "岗位", "总工时", "折合天数（8小时/天）"
]


def build_monthly_valid_hours(
    db: Session,
    *,
    month: str,
    restrict_furnace_ids: set[int] | None = None,
) -> dict:
    """劳勤工作台月度导出：选定月份所有「班次有效」的出勤工时，按
    (工号 + 工时类型 + 地区 + 工作点编号 + 岗位) 合并为一条，工时累加为总工时。

    - 班次有效：该(员工, 自然日)对账结论经 derive_validity 判为有效（无效日不计）。
      口径与算薪导出 [_valid_day_fractions] 一致：从批量预载的源数据计算，不修改对账表。
    - 总工时：该组所有有效出勤班次 work_hours 之和（半班 4h / 整班 8h，原样累加）。
    - 折合天数：总工时 / 8。
    - 地区/工作点编号：取自该出勤记录归属工作点(furnace_id → Furnace.region/furnace_no)。
    - restrict_furnace_ids：值班长仅导本人工作点；HR/统计/管理员=全量(None)。
    只取出勤记录(scan_is_active：active 且 record_kind=出勤)，加班记录不计入。"""
    if not _MONTH_RE.match(month or ""):
        raise ExportInputError(f"非法月份：{month!r}，应为 YYYY-MM")

    q = select(AttendanceRecord).where(
        AttendanceRecord.natual_date.startswith(month),
        scan_is_active(),
    )
    if restrict_furnace_ids is not None:
        if not restrict_furnace_ids:
            return {"columns": MONTHLY_HOURS_COLUMNS, "rows": [], "month": month}
        q = q.where(AttendanceRecord.furnace_id.in_(restrict_furnace_ids))
    scans = bounded_rows(db, q)
    context = ReportSnapshot.for_month(db, month, {r.emp_id for r in scans})

    # 出勤记录归属工作点 → 地区/工作点编号
    furnace_ids = {s.furnace_id for s in scans}
    furnaces = (
        {f.id: f for f in db.scalars(select(Furnace).where(Furnace.id.in_(furnace_ids)))}
        if furnace_ids
        else {}
    )

    valid_day = {key: context.validity(*key)[0] for key in context.recons}

    # 合并：键=(工号, 工时类型, 地区, 工作点编号, 岗位)，值=有效班次工时累加
    agg: dict[tuple, float] = {}
    for s in scans:
        # 无对账行 = 自动一致计入（只读计算后通常都有行；缺行时按有效兜底，与算薪口径一致）
        if valid_day.get((s.emp_id, s.natual_date), True) is False:
            continue
        f = furnaces.get(s.furnace_id)
        region = f.region if f else ""
        furnace_no = f.furnace_no if f else ""
        key = (s.emp_id, s.employ_type, region, furnace_no, s.post)
        agg[key] = agg.get(key, 0.0) + (s.work_hours if s.work_hours is not None else 8.0)

    emp_ids = {k[0] for k in agg}
    names = (
        {e.emp_id: e.name for e in db.scalars(select(Employee).where(Employee.emp_id.in_(emp_ids)))}
        if emp_ids
        else {}
    )
    rows: list[list] = []
    for (emp_id, employ_type, region, furnace_no, post), hours in sorted(agg.items()):
        total = round(hours, 2)
        rows.append([
            emp_id,
            names.get(emp_id, emp_id),
            employ_type,
            region,
            furnace_no,
            post,
            total,
            round(total / 8.0, 2),
        ])
    return {"columns": MONTHLY_HOURS_COLUMNS, "rows": rows, "month": month}


def build_monthly_hours_xlsx(meta: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "班次有效工时"
    append_report_row(ws, meta["columns"])
    for row in meta["rows"]:
        append_report_row(ws, row)
    if not meta["rows"]:
        append_report_row(ws, ["（当月无班次有效数据）"])
    _format_employee_id_columns_as_text(ws)
    buf = BytesIO()
    check_report_deadline()
    wb.save(buf)
    check_report_deadline()
    return buf.getvalue()


# 月度「班次有效工时（考勤表式）」日历导出固定列（日期列在两端固定列之间动态插入）：
_CALENDAR_HEAD = ["地区", "工作点编号", "班组", "姓名", "工号", "岗位"]
_CALENDAR_TAIL = ["应出勤天数", "全月出勤", "年休假统计天数", "旷工天数", "加班合计"]


def build_monthly_calendar(
    db: Session,
    *,
    month: str,
    restrict_furnace_ids: set[int] | None = None,
    persist: bool = True,
    context: ReportSnapshot | None = None,
    absence_days_by_emp: dict[str, float] | None = None,
    absence_row_employee_ids: set[str] | None = None,
    use_absence_overrides: bool = True,
) -> dict:
    """劳勤工作台月度「考勤表式」导出：一人(工作点×岗位)一行，列出 1~月末 每日有效工时。

    - 行键：(地区, 工作点编号, 工号, 岗位)；同一员工跨工作点/跨岗各成一行（与原 build_monthly_valid_hours 一致）。
    - 每日格子：该(员工, 工作点, 岗位, 日)所有「班次有效」出勤班次的 work_hours 之和 ÷ 8 = 编制位
      （整班 8h→1、半班 4h→0.5），无有效=空。班次有效=对账 derive_validity 判定（与算薪同口径）。
    - 全月：当月有效工时合计 ÷ 8（折合天数）。
    - 年休假统计天数：按每日年假工时 ÷ 8，并沿用请假分段规则归属到对应岗位。
    - 加班：Σ(有效出勤班次中 work_hours-8 的超出部分) + Σ(record_kind=加班 班次全部工时)。
      即「单班超 8h 的延时」+「单独标记为加班的班次」两部分之和。
    - restrict_furnace_ids：值班长仅本人工作点；HR/统计/管理员=全量(None)。
    - persist 保留调用兼容；预览和下载均只读计算，不再物化对账。"""
    if not _MONTH_RE.match(month or ""):
        raise ExportInputError(f"非法月份：{month!r}，应为 YYYY-MM")

    y, m = (int(x) for x in month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    day_cols = [str(d) for d in range(1, days_in_month + 1)]
    columns = _CALENDAR_HEAD + day_cols + _CALENDAR_TAIL
    today = _shanghai_today()
    expected_days = _expected_attendance_days(month, today=today)
    warnings: list[dict] = []
    if month == today.strftime("%Y-%m"):
        cutoff = today - timedelta(days=1)
        warnings.append({
            "type": "in_progress_month",
            "cutoffDate": cutoff.isoformat(),
            "message": (
                f"{month} 尚未结束：应出勤天数 {expected_days} 仅统计到 "
                f"{cutoff.isoformat()}，数据尚未闭合，不可作为正式薪资依据"
            ),
        })

    # 出勤 + 加班记录（按 record_kind 区分），active；加班记录不被 scan_is_active 计入，单独取。
    base = select(AttendanceRecord).where(
        AttendanceRecord.natual_date.startswith(month),
        AttendanceRecord.status == "active",
        AttendanceRecord.record_kind.in_(["出勤", ATTENDANCE_KIND_OVERTIME]),
    )
    if restrict_furnace_ids is not None:
        if not restrict_furnace_ids:
            return {
                "columns": columns,
                "rows": [],
                "month": month,
                "daysInMonth": days_in_month,
                "warnings": warnings,
            }
        base = base.where(AttendanceRecord.furnace_id.in_(restrict_furnace_ids))
    records = ([r for r in context.records if r.status == "active"
                and r.record_kind in ("出勤", ATTENDANCE_KIND_OVERTIME)
                and (restrict_furnace_ids is None or r.furnace_id in restrict_furnace_ids)]
               if context is not None else bounded_rows(db, base))
    context = context or ReportSnapshot.for_month(db, month, {r.emp_id for r in records})

    furnace_ids = {r.furnace_id for r in records}
    furnaces = (
        {f.id: f for f in db.scalars(select(Furnace).where(Furnace.id.in_(furnace_ids)))}
        if furnace_ids
        else {}
    )

    # Both preview and download use the same in-memory reconciliation.
    valid_day = {key: context.validity(*key)[0] for key in context.recons}
    recon_by_date = context.recons

    def _is_valid(emp_id: str, date: str) -> bool:
        # 缺对账行按有效兜底（只读计算后通常都有行），与 build_monthly_valid_hours 一致
        return valid_day.get((emp_id, date), True)

    # 行键加入班组，避免同一员工跨班组记录被合并后无法核对来源。
    rows_by: dict[tuple, dict] = {}
    for r in records:
        check_report_deadline()
        if r.record_kind == ATTENDANCE_KIND_OVERTIME:
            if not _overtime_record_valid(db, r, recon_by_date, context):
                continue
        elif not _is_valid(r.emp_id, r.natual_date):
            continue
        f = furnaces.get(r.furnace_id)
        region = f.region if f else ""
        furnace_no = f.furnace_no if f else ""
        squad = r.squad or "不分班"
        key = (region, furnace_no, squad, r.emp_id, r.post)
        bucket = rows_by.setdefault(
            key, {"name": None, "days": {}, "overtime": 0.0}
        )
        wh = r.work_hours if r.work_hours is not None else 8.0
        day_no = int(r.natual_date.split("-")[2])
        if r.record_kind == ATTENDANCE_KIND_OVERTIME:
            # 单独加班班次：整条工时计入加班，不占日历出勤格（避免与同日出勤格语义混淆）
            bucket["overtime"] += wh
        else:
            # 出勤班次：日历格累加编制位（工时/8）；超 8h 的延时部分计入加班
            # 同一岗位同一天的重复记录只取最大出勤占比，与薪资表一致。
            bucket["days"][day_no] = max(
                bucket["days"].get(day_no, 0.0), min(wh, 8.0) / 8.0
            )
            if wh > 8.0:
                bucket["overtime"] += wh - 8.0

    emp_ids = {r.emp_id for r in records}
    names = {key: value.name for key, value in context.employees.items()}
    # 员工级请假与旷工必须先按全月全工作点确定唯一主工作点，再投影到本次导出范围。
    # 否则同一员工按工作点分批导出时，每个文件都会重复出现整月总量。
    all_work_records: list[AttendanceRecord] = []
    valid_fractions_by_emp: dict[str, dict[str, float]] = {}
    if emp_ids:
        all_work_records = [r for r in context.records if r.status == "active"
                            and r.record_kind == "出勤" and r.emp_id in emp_ids]
        for r in all_work_records:
            if not _is_valid(r.emp_id, r.natual_date):
                continue
            wh = r.work_hours if r.work_hours is not None else 8.0
            daymap = valid_fractions_by_emp.setdefault(r.emp_id, {})
            daymap[r.natual_date] = max(
                daymap.get(r.natual_date, 0.0), min(wh, 8.0) / 8.0
            )
    if absence_days_by_emp is None:
        absence_days_by_emp = _monthly_absence_days(context, month, valid_fractions_by_emp, today=today)
        saved = get_absence_override_values(db, month, emp_ids)
        absence_row_employee_ids = set(absence_days_by_emp) | set(saved)
        if use_absence_overrides:
            limits = _monthly_absence_limits(context, month, valid_fractions_by_emp, saved, today=today)
            absence_days_by_emp = apply_absence_overrides(absence_days_by_emp, saved, limits)
    if absence_row_employee_ids is None:
        absence_row_employee_ids = set(absence_days_by_emp)
    all_furnace_ids = {record.furnace_id for record in all_work_records}
    all_furnaces = {
        furnace.id: furnace
        for furnace in db.scalars(
            select(Furnace).where(Furnace.id.in_(all_furnace_ids))
        ).all()
    } if all_furnace_ids else {}
    owner_furnace_by_emp = _dominant_furnace_by_emp(
        all_work_records, all_furnaces, valid_fractions_by_emp
    )
    owned_employee_ids = {
        employee_id
        for employee_id, owner_furnace_id in owner_furnace_by_emp.items()
        if owner_furnace_id in furnaces
    }
    # 主工作点未确定（整月无有效出勤、只有加班记录等）的员工与「输出给薪资」同口径：
    # 也在本文件承接原始旷工（承接行取该员工在本文件排序最前的行）。
    owned_employee_ids |= set(emp_ids) - set(owner_furnace_by_emp)
    leave_totals_by_emp, leave_by_emp_group = _payroll_leave_allocation(
        db,
        sorted(emp_ids),
        month,
        all_work_records,
        all_furnaces,
        valid_fractions_by_emp,
        owner_furnace_by_emp,
        context=context,
    )

    leave_days_by_row: dict[tuple, float] = {}
    annual_leave_days_by_row: dict[tuple, float] = {}
    for employee_id, group_values in leave_by_emp_group.items():
        for (furnace_id, squad, post, _employ_type), values in group_values.items():
            furnace = furnaces.get(furnace_id)
            if furnace is None:
                continue
            key = (
                furnace.region,
                furnace.furnace_no,
                squad or "不分班",
                employee_id,
                post,
            )
            leave_days_by_row[key] = leave_days_by_row.get(key, 0.0) + _as_float(
                values.get("请假总时长(天)")
            )
            annual_leave_days_by_row[key] = annual_leave_days_by_row.get(
                key, 0.0
            ) + _as_float(values.get("年假(天)"))

    sorted_row_keys = sorted(
        rows_by, key=lambda k: (k[0], _furnace_sort_key(k[1]), k[2], k[4], k[3])
    )
    absence_target_by_emp: dict[str, tuple] = {}
    for key in sorted_row_keys:
        employee_id = key[3]
        if employee_id not in owned_employee_ids:
            continue
        current = absence_target_by_emp.get(employee_id)
        rank = (sum(rows_by[key]["days"].values()), leave_days_by_row.get(key, 0.0))
        current_rank = (
            sum(rows_by[current]["days"].values()),
            leave_days_by_row.get(current, 0.0),
        ) if current is not None else None
        if current_rank is None or rank > current_rank:
            absence_target_by_emp[employee_id] = key

    rows: list[list] = []
    for region, furnace_no, squad, emp_id, post in sorted_row_keys:
        bucket = rows_by[(region, furnace_no, squad, emp_id, post)]
        day_cells = [
            (round(bucket["days"][d], 2) if d in bucket["days"] else "")
            for d in range(1, days_in_month + 1)
        ]
        month_total = round(sum(bucket["days"].values()), 2)  # 折合天数(编制位合计)
        annual_leave_days = _sum_number(
            [annual_leave_days_by_row.get((region, furnace_no, squad, emp_id, post), 0.0)]
        )
        absence_days = 0
        if absence_target_by_emp.get(emp_id) == (
            region,
            furnace_no,
            squad,
            emp_id,
            post,
        ):
            absence_days = absence_days_by_emp.get(emp_id, 0)
        overtime = round(bucket["overtime"], 2)
        rows.append(
            [region, furnace_no, squad, names.get(emp_id, emp_id), emp_id, post]
            + day_cells
            + [expected_days, month_total, annual_leave_days, absence_days, overtime or ""]
        )
    # 无有效出勤行时，手动值（含0）和原始旷工仍需要承接行，单表与合并导出一致。
    present = {row[4] for row in rows}
    for employee_id in sorted(owned_employee_ids - present):
        leave = leave_totals_by_emp.get(employee_id, {})
        if employee_id not in absence_row_employee_ids and _as_float(leave.get("请假总时长(天)")) <= 0:
            continue
        rows.append(["", "", "", names.get(employee_id, employee_id), employee_id, ""]
                    + [""] * days_in_month
                    + [expected_days, 0, _as_float(leave.get("年假(天)")),
                       absence_days_by_emp.get(employee_id, 0), ""])
    return {
        "columns": columns,
        "rows": rows,
        "month": month,
        "daysInMonth": days_in_month,
        "warnings": warnings,
    }


def _furnace_sort_key(furnace_no: str):
    """工作点编号排序：先按数字（35号工作点<45号工作点），再按原串。"""
    digits = "".join(ch for ch in (furnace_no or "") if ch.isdigit())
    return (int(digits) if digits else 0, furnace_no or "")


def build_monthly_calendar_xlsx(meta: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"班次有效工时{meta['month']}"
    append_report_row(ws, meta["columns"])
    for row in meta["rows"]:
        append_report_row(ws, row)
    if not meta["rows"]:
        append_report_row(ws, ["（当月无班次有效数据）"])
    for warning in meta.get("warnings") or []:
        append_report_row(ws, ["口径说明", warning.get("message", "")])
    _format_employee_id_columns_as_text(ws)
    # 冻结前 6 列（固定信息）+ 首行，便于横向翻看日期
    ws.freeze_panes = "G2"
    buf = BytesIO()
    check_report_deadline()
    wb.save(buf)
    check_report_deadline()
    return buf.getvalue()


# 加班明细导出列（对齐客户 Excel「加班明细表」分页：序号/工号/姓名/日期/时间/原因/岗位/部门/签名）
_OVERTIME_COLUMNS = [
    "序号", "工号", "加班员工姓名", "加班日期", "加班时间(小时)",
    "加班原因", "加班岗位", "月加班部门", "加班现场负责人签名",
]
# 加班汇总列（按 工作点编号×加班岗位×班次 分组求和）
_OVERTIME_SUMMARY_COLUMNS = ["工作点编号", "加班岗位", "班次", "加班合计(小时)"]


def build_overtime_detail(
    db: Session,
    *,
    month: str,
    restrict_furnace_ids: set[int] | None = None,
    persist: bool = True,
    context: ReportSnapshot | None = None,
) -> dict:
    """月度「加班明细表」：一行一条加班，对齐客户 Excel 加班明细分页。

    加班来源两类（均仅取对账有效日，与算薪/班次有效工时同口径）：
    - 出勤记录 work_hours>8 的【延时部分】（加班时间 = wh-8）；
    - record_kind=加班 的独立加班记录（加班时间 = wh 全段）。
    列：序号/工号/姓名/加班日期/加班时间(小时)/加班原因/加班岗位/月加班部门(工作点编号+班次)/现场负责人签名(留空)。
    persist 保留调用兼容；预览与下载均只读计算。
    """
    if not _MONTH_RE.match(month or ""):
        raise ExportInputError(f"非法月份：{month!r}，应为 YYYY-MM")

    base = select(AttendanceRecord).where(
        AttendanceRecord.natual_date.startswith(month),
        AttendanceRecord.status == "active",
        AttendanceRecord.record_kind.in_(["出勤", ATTENDANCE_KIND_OVERTIME]),
    )
    if restrict_furnace_ids is not None:
        if not restrict_furnace_ids:
            return {"columns": _OVERTIME_COLUMNS, "rows": [], "month": month}
        base = base.where(AttendanceRecord.furnace_id.in_(restrict_furnace_ids))
    records = ([r for r in context.records if r.status == "active"
                and r.record_kind in ("出勤", ATTENDANCE_KIND_OVERTIME)
                and (restrict_furnace_ids is None or r.furnace_id in restrict_furnace_ids)]
               if context is not None else bounded_rows(db, base))
    context = context or ReportSnapshot.for_month(db, month, {r.emp_id for r in records})

    furnace_ids = {r.furnace_id for r in records}
    furnaces = (
        {f.id: f for f in db.scalars(select(Furnace).where(Furnace.id.in_(furnace_ids)))}
        if furnace_ids
        else {}
    )

    valid_day = {key: context.validity(*key)[0] for key in context.recons}
    recon_by_date = context.recons
    names = {key: value.name for key, value in context.employees.items()}

    # 收集加班条目
    items = []
    for r in records:
        check_report_deadline()
        if r.record_kind == ATTENDANCE_KIND_OVERTIME:
            if not _overtime_record_valid(db, r, recon_by_date, context):
                continue
        elif valid_day.get((r.emp_id, r.natual_date), True) is False:
            continue
        wh = r.work_hours if r.work_hours is not None else 8.0
        if r.record_kind == ATTENDANCE_KIND_OVERTIME:
            ot = wh
        elif wh > 8.0:
            ot = wh - 8.0  # 出勤延时部分
        else:
            continue  # 普通整班/半班，无加班
        if ot <= 0:
            continue
        f = furnaces.get(r.furnace_id)
        furnace_no = f.furnace_no if f else ""
        squad = r.squad or ""
        dept = f"{furnace_no}{squad}" if squad else furnace_no  # 月加班部门 = 工作点编号+班次
        items.append({
            "emp_id": r.emp_id, "name": names.get(r.emp_id, r.emp_id),
            "date": r.natual_date, "ot": round(ot, 2), "reason": r.reason or "",
            "post": r.post, "dept": dept,
            "furnace_no": furnace_no, "squad": squad,
        })

    # 排序：日期 → 部门 → 工号
    items.sort(key=lambda x: (x["date"], x["dept"], x["emp_id"]))
    rows = []
    for i, it in enumerate(items, start=1):
        rows.append([
            i, it["emp_id"], it["name"], it["date"],
            it["ot"], it["reason"], it["post"], it["dept"], "",
        ])

    # 汇总：按 (工作点编号, 加班岗位, 班次) 分组求和加班工时
    agg: dict[tuple, float] = {}
    for it in items:
        key = (it["furnace_no"], it["post"], it["squad"])
        agg[key] = agg.get(key, 0.0) + it["ot"]
    summary_rows = [
        [fn, post, sq or "不分班", round(h, 2)]
        for (fn, post, sq), h in sorted(
            agg.items(), key=lambda kv: (_furnace_sort_key(kv[0][0]), kv[0][2], kv[0][1])
        )
    ]

    # 按人汇总：同一人当月加班按 (工作点编号+班次+岗位) 合并求和，横向铺开（小时1/岗位1、小时2/岗位2…）
    # 岗位格 = 工作点编号+班次+岗位 拼接（如「45号工作点早班副班组长」；不分班则工作点编号+岗位）。
    per_emp: dict[str, dict] = {}
    for it in items:
        e = per_emp.setdefault(it["emp_id"], {"name": it["name"], "groups": {}})
        label = f"{it['furnace_no']}{it['squad']}{it['post']}" if it["squad"] else f"{it['furnace_no']}{it['post']}"
        e["groups"][label] = e["groups"].get(label, 0.0) + it["ot"]
    max_groups = max((len(v["groups"]) for v in per_emp.values()), default=0)
    by_person_columns = ["序号", "工号", "加班员工姓名"]
    for i in range(1, max_groups + 1):
        by_person_columns += [f"加班小时{i}", f"加班岗位{i}"]
    by_person_rows = []
    for idx, (emp_id, v) in enumerate(
        sorted(per_emp.items(), key=lambda kv: kv[0]), start=1
    ):
        row = [idx, emp_id, v["name"]]
        # 组内按岗位标签排序，保证稳定
        for label, h in sorted(v["groups"].items()):
            row += [round(h, 2), label]
        # 补齐到 max_groups 对（空格）
        row += [""] * ((max_groups - len(v["groups"])) * 2)
        by_person_rows.append(row)

    return {
        "columns": _OVERTIME_COLUMNS, "rows": rows, "month": month,
        "summaryColumns": _OVERTIME_SUMMARY_COLUMNS, "summaryRows": summary_rows,
        "byPersonColumns": by_person_columns, "byPersonRows": by_person_rows,
    }


def build_overtime_detail_xlsx(meta: dict) -> bytes:
    wb = Workbook()
    # Sheet1：加班明细（逐条）
    ws = wb.active
    ws.title = f"加班明细{meta['month']}"
    append_report_row(ws, meta["columns"])
    for row in meta["rows"]:
        append_report_row(ws, row)
    if not meta["rows"]:
        append_report_row(ws, ["（当月无加班明细）"])
    _format_employee_id_columns_as_text(ws)
    ws.freeze_panes = "A2"
    # Sheet2：加班汇总（工作点编号×加班岗位×班次 求和）
    ws2 = wb.create_sheet(title="加班汇总")
    append_report_row(ws2, meta.get("summaryColumns") or _OVERTIME_SUMMARY_COLUMNS)
    for row in meta.get("summaryRows", []):
        append_report_row(ws2, row)
    if not meta.get("summaryRows"):
        append_report_row(ws2, ["（当月无加班汇总）"])
    _format_employee_id_columns_as_text(ws2)
    ws2.freeze_panes = "A2"
    # Sheet3：按人汇总（同一人当月加班横向铺开：小时1/岗位1、小时2/岗位2…）
    ws3 = wb.create_sheet(title="按人汇总")
    append_report_row(ws3, meta.get("byPersonColumns") or ["序号", "工号", "加班员工姓名"])
    for row in meta.get("byPersonRows", []):
        append_report_row(ws3, row)
    if not meta.get("byPersonRows"):
        append_report_row(ws3, ["（当月无加班）"])
    _format_employee_id_columns_as_text(ws3)
    ws3.freeze_panes = "D2"  # 冻结前3列(序号/工号/姓名)+首行
    buf = BytesIO()
    check_report_deadline()
    wb.save(buf)
    check_report_deadline()
    return buf.getvalue()


def build_export_meta(
    db: Session,
    *,
    month: str,
    furnaces: list[str],
    emp_id: str | None,
    furnace_ids: list[int] | None = None,
    persist: bool = True,
    use_absence_overrides: bool = True,
    include_monthly_hours: bool = True,
    include_overtime_detail: bool = True,
    payroll2_group_by: list[str] | None = None,
    payroll2_include_zero_rows: bool = False,
    payroll2_merge_overtime_only_rows: bool = False,
    payroll2_merge_same_squad_overtime_only_rows: bool = False,
):
    # 入参校验：非法月份/不存在工作点直接报错，不再静默回退到 T0001/任意工作点（产出伪造薪资行）
    if not _MONTH_RE.match(month or ""):
        raise ExportInputError(f"非法月份：{month!r}，应为 YYYY-MM")
    # 优先按 furnace_id（全局唯一）定位工作点，避免跨地区同名工作点编号把他地区员工一并导入(P0)；
    # 未传 id 时兼容旧按工作点编号查（测试/老前端）。
    if furnace_ids:
        furnace_rows = db.scalars(select(Furnace).where(Furnace.id.in_(furnace_ids))).all()
    else:
        furnace_rows = db.scalars(
            select(Furnace).where(Furnace.furnace_no.in_(furnaces))
        ).all()
    if not furnace_rows:
        raise ExportInputError(f"未找到指定工作点：{furnace_ids or furnaces}")
    furnace_ids = [f.id for f in furnace_rows]
    furnace_map = {f.id: f for f in furnace_rows}

    scans = bounded_rows(db,
            select(AttendanceRecord).where(
                AttendanceRecord.furnace_id.in_(furnace_ids),
                *([AttendanceRecord.emp_id == emp_id] if emp_id else []),
                AttendanceRecord.natual_date.startswith(month),
                scan_is_active(),
            )
    )
    # 加班记录（record_kind=加班）：单独取出并入按人分组，供算薪加班列体现。
    # 也参与对账物化日期集合，否则无效异常日的独立加班可能绕过有效性过滤。
    ot_scans = bounded_rows(db,
            select(AttendanceRecord).where(
                AttendanceRecord.furnace_id.in_(furnace_ids),
                *([AttendanceRecord.emp_id == emp_id] if emp_id else []),
                AttendanceRecord.natual_date.startswith(month),
                AttendanceRecord.status == "active",
                AttendanceRecord.record_kind == ATTENDANCE_KIND_OVERTIME,
            )
    )
    by_emp: dict[str, list] = {}
    for s in [*scans, *ot_scans]:
        by_emp.setdefault(s.emp_id, []).append(s)

    # 指定 emp_id → 仅导该员工；否则导出当月该批工作点全部出勤员工（花名册全量，非单人样表）
    emp_ids = [emp_id] if emp_id else sorted(by_emp.keys())
    # 旷工使用员工整月原始考勤结果，部分工作点导出仍沿用唯一主工作点归属。
    context = ReportSnapshot.for_month(db, month, set(emp_ids))
    all_work_scans = [r for r in context.records
                      if r.status == "active" and r.record_kind == "出勤"]
    valid_fractions_by_emp = _valid_day_fractions_bulk(db, all_work_scans, context)
    today = _shanghai_today()
    absence_days_by_emp = _monthly_absence_days(context, month, valid_fractions_by_emp, today=today)
    saved = get_absence_override_values(db, month, emp_ids)
    absence_row_employee_ids = set(absence_days_by_emp) | set(saved)
    # 行保留条件与数值分开；页面始终显示自动值，下载采用经当前上限校验的手动值。
    if use_absence_overrides:
        limits = _monthly_absence_limits(context, month, valid_fractions_by_emp, saved, today=today)
        absence_days_by_emp = apply_absence_overrides(absence_days_by_emp, saved, limits)
    actual_days_by_emp = {
        employee_id: sum(day_values.values())
        for employee_id, day_values in valid_fractions_by_emp.items()
    }
    all_furnace_ids = {row.furnace_id for row in all_work_scans}
    all_furnace_map = {
        furnace.id: furnace
        for furnace in db.scalars(
            select(Furnace).where(Furnace.id.in_(all_furnace_ids))
        ).all()
    } if all_furnace_ids else {}
    owner_furnace_by_emp = _dominant_furnace_by_emp(
        all_work_scans, all_furnace_map, valid_fractions_by_emp
    )
    outside_owner_ids = sorted(
        employee_id
        for employee_id in emp_ids
        if by_emp.get(employee_id)
        and owner_furnace_by_emp.get(employee_id) is not None
        and owner_furnace_by_emp[employee_id] not in furnace_ids
    )
    unknown_owner_ids = sorted(
        employee_id
        for employee_id in emp_ids
        if owner_furnace_by_emp.get(employee_id) is None
    )
    # 只有“已确定主工作点且主工作点在本次范围外”才不在本文件承接旷工。
    # 无有效出勤日时仍承接原始旷工小时数。
    absence_eligible_employee_ids = set(emp_ids) - set(outside_owner_ids)
    export_warnings = []
    if month == today.strftime("%Y-%m"):
        cutoff = today - timedelta(days=1)
        export_warnings.append({
            "type": "in_progress_month",
            "cutoffDate": cutoff.isoformat(),
            "message": (
                f"{month} 尚未结束：应出勤天数仅统计到 {cutoff.isoformat()}，"
                "数据尚未闭合，不可作为正式薪资依据"
            ),
        })
    if outside_owner_ids:
        export_warnings.append({
            "type": "absence_owned_outside_scope",
            "count": len(outside_owner_ids),
            "employeeIds": outside_owner_ids,
            "message": (
                f"本次范围内有 {len(outside_owner_ids)} 名员工的主工作点在范围外；"
                "其整月旷工/请假不在本文件承接"
            ),
        })
    leave_totals_by_emp, leave_by_emp_group = _payroll_leave_allocation(
        db,
        emp_ids,
        month,
        all_work_scans,
        all_furnace_map,
        valid_fractions_by_emp,
        owner_furnace_by_emp,
        context=context,
    )
    if unknown_owner_ids:
        unallocated_leave_days = _sum_number(
            [
                _as_float(
                    leave_totals_by_emp.get(employee_id, {}).get("请假总时长(天)")
                )
                for employee_id in unknown_owner_ids
            ]
        )
        export_warnings.append({
            "type": "absence_owner_undetermined",
            "count": len(unknown_owner_ids),
            "employeeIds": unknown_owner_ids,
            "unallocatedLeaveDays": unallocated_leave_days,
            "message": (
                f"本次范围内有 {len(unknown_owner_ids)} 名员工全月无有效出勤日，"
                f"其中存在无法归属到岗位的请假 {unallocated_leave_days} 天；"
                "请假计入员工主要行，旷工按源小时数逐日以8小时减有效出勤和请假的剩余小时为上限，统计至昨天，合计除以8计算"
            ),
        })
    all_payroll_rows = [
        row
        for e in emp_ids
        for row in _build_payroll_rows(
            db,
            e,
            by_emp.get(e, []),
            furnace_map,
            month,
            leave_by_emp_group.get(e),
            valid_day_fractions=valid_fractions_by_emp.get(e, {}),
            context=context,
        )
    ]
    _apply_employee_absence(
        all_payroll_rows,
        leave_totals_by_emp,
        absence_days_by_emp,
        absence_eligible_employee_ids,
    )
    display_leave_totals = _payroll_display_leave_totals(
        db, sorted(absence_eligible_employee_ids), month, context
    )
    _apply_display_leave(all_payroll_rows, display_leave_totals)
    _apply_monthly_business_trip(
        all_payroll_rows, context, month, absence_eligible_employee_ids
    )
    # 输出给薪资：出勤、加班、请假、出差和原始旷工均为 0 的行不展示。
    # 输出给薪资2 在聚合完成后按页面开关决定是否保留此类空行。
    retained_employee_ids = absence_row_employee_ids & absence_eligible_employee_ids
    rows = _filter_payroll_rows(all_payroll_rows, retained_employee_ids)
    overtime_slot_count = _payroll_overtime_slot_count(rows)
    columns = _payroll_columns(overtime_slot_count)
    # sampleRow 保留原始首行用于兼容旧调用；实际展示和导出必须以 rows 为准。
    sample_row = all_payroll_rows[0] if all_payroll_rows else {col: "" for col in columns}
    payroll_sheet2 = _build_payroll2_sheet(
        all_payroll_rows,
        payroll2_group_by,
        display_leave_totals_by_emp=display_leave_totals,
        include_zero_rows=payroll2_include_zero_rows,
        merge_overtime_only_rows=payroll2_merge_overtime_only_rows,
        merge_same_squad_overtime_only_rows=payroll2_merge_same_squad_overtime_only_rows,
        absence_eligible_employee_ids=absence_eligible_employee_ids,
        absence_row_employee_ids=retained_employee_ids,
    )

    # 打卡异常 + 三方对账两张表数据源齐全，逐表填充（原仅占位说明行=交付空表）
    tripartite_sheet = _tripartite_sheet(db, emp_ids, month, furnace_map, context)
    extra_sheets = {
        "打卡异常": {
            "columns": ANOMALY_SHEET_COLUMNS,
            "rows": _anomaly_sheet_rows(db, emp_ids, month, context),
        },
        "三方对账（劳勤/工作点/补录）": tripartite_sheet,
    }
    restrict_furnace_ids = set(furnace_ids)
    monthly_hours = (
        build_monthly_calendar(
            db,
            month=month,
            restrict_furnace_ids=restrict_furnace_ids,
            persist=persist,
            context=context,
            absence_days_by_emp=absence_days_by_emp,
            absence_row_employee_ids=absence_row_employee_ids,
            use_absence_overrides=use_absence_overrides,
        )
        if include_monthly_hours
        else None
    )
    if monthly_hours is not None:
        present_emp_ids = {
            str(row[4]) for row in monthly_hours["rows"]
        }
        expected_days = _expected_attendance_days(month, today=today)
        for supplement_emp in sorted(absence_eligible_employee_ids):
            leave_days = _as_float(
                leave_totals_by_emp.get(supplement_emp, {}).get("请假总时长(天)")
            )
            absence_days = absence_days_by_emp.get(supplement_emp, 0)
            if leave_days <= 0 and supplement_emp not in absence_row_employee_ids:
                continue
            if supplement_emp in present_emp_ids:
                continue
            annual_leave_days = _as_float(
                leave_totals_by_emp.get(supplement_emp, {}).get("年假(天)")
            )
            outside_actual_days = actual_days_by_emp.get(supplement_emp, 0.0)
            if outside_actual_days > 0:
                export_warnings.append({
                    "type": "monthly_hours_attendance_outside_scope",
                    "employeeIds": [supplement_emp],
                    "outsideActualDays": _sum_number([outside_actual_days]),
                    "message": (
                        f"员工 {supplement_emp} 在本次工作点范围外有有效出勤 "
                        f"{_sum_number([outside_actual_days])} 天；"
                        "班次有效工时的补充行旷工取自整月原始考勤结果"
                    ),
                })
            employee = context.employees.get(supplement_emp)
            monthly_hours["rows"].append(
                ["", "", "", employee.name if employee else supplement_emp, supplement_emp, ""]
                + [""] * monthly_hours["daysInMonth"]
                + [
                    expected_days,
                    0,
                    _sum_number([annual_leave_days]),
                    absence_days,
                    "",
                ]
            )
    overtime_detail = (
        build_overtime_detail(
            db,
            month=month,
            restrict_furnace_ids=restrict_furnace_ids,
            persist=persist,
            context=context,
        )
        if include_overtime_detail
        else None
    )
    return {
        "sheets": ["导出说明", "输出给薪资", "输出给薪资2", *list(EXPORT_SHEET_NAMES)[1:], "班次有效工时", "加班明细", "加班汇总", "按人汇总"],
        "furnaceCount": len(furnace_rows),
        "warnings": export_warnings,
        "payrollSheet": {
            "name": "输出给薪资",
            "columnCount": len(columns),
            "columns": columns,
            # sampleRow 保留（首行）兼容旧调用；rows 为全员花名册
            "sampleRow": sample_row,
            "rows": rows,
            "rowCount": len(rows),
        },
        "payrollSheet2": payroll_sheet2,
        "extraSheets": extra_sheets,
        "monthlyHours": monthly_hours,
        "overtimeDetail": overtime_detail,
        # 计费规则透出到页面，便于核对（金额由下游薪资系统按此口径核算）
        "billing": {
            "ratios": EMPLOY_TYPE_RATIO,  # {正式:1.0, 实习:0.7, 待岗:0.8}
            "rules": [
                "金额 = 基准金额 × 薪资比例系数（金额本体由下游薪资系统核算）",
                "用工类型系数：正式 100%、实习 70%、待岗 80%",
                "应出勤天数：完整月为日历天数 - 4；进行中月份只计算到昨天并同比例扣减公休。旷工天数：优先采用员工当月已保存的手动值（含0），未录入时按原始旷工小时数按日以8小时减有效出勤和请假的剩余小时为上限（每日合计不超过8小时，仅统计到昨天），再月度合计 ÷ 8，四舍五入最多保留两位小数；实际出勤天数：整班(8h)=1 天、半班(4h)=0.5 天，单条工时按 8h 封顶折算",
                "加班：出勤工时超过 8h 的部分 + 跨岗位加班，按实际岗位数动态展开到「加班N/加班标准N」与「加班合计」",
                "无效日（对账未通过 / HR·值班长判不通过）不计入出勤天数与加班",
            ],
        },
    }


def _format_employee_id_columns_as_text(ws) -> None:
    """将所有「工号」数据单元格写成原始字符串，并设置为 Excel 文本格式。"""
    employee_id_columns = [
        cell.column for cell in ws[1] if cell.value == "工号"
    ]
    for column in employee_id_columns:
        for row in range(2, ws.max_row + 1):
            check_report_deadline()
            cell = ws.cell(row=row, column=column)
            if cell.value not in (None, ""):
                cell.value = str(cell.value)
            cell.number_format = "@"


def _export_cell_value(column, value):
    # Excel 旷工空值写为数字 0；不改变其它列的空白或已有非零天数。
    if column == "旷工天数" and (value is None or str(value).strip() in ("", "-", "0")):
        return 0
    return value


def _append_sheet_rows(ws, columns: list, rows: list, empty_text: str, *, freeze_panes: str | None = "A2"):
    append_report_row(ws, columns)
    absence_columns = [i for i, column in enumerate(columns) if column == "旷工天数"]
    for row in rows:
        values = list(row)
        for i in absence_columns:
            if i >= len(values):
                values.extend([None] * (i + 1 - len(values)))
            values[i] = _export_cell_value("旷工天数", values[i])
        append_report_row(ws, values)
    if not rows:
        append_report_row(ws, [empty_text])
    if freeze_panes:
        ws.freeze_panes = freeze_panes


def _append_export_sheet(wb: Workbook, meta: dict, name: str):
    ws = wb.create_sheet(title=_safe_sheet_title(name))
    if name == "导出说明":
        append_report_row(ws, ["导出说明"])
        append_report_row(ws, [
            "口径说明",
            "旷工天数优先采用员工当月已保存的手动值（含0）；未录入时=原始旷工小时数按日以8小时减有效出勤和请假的剩余小时为上限（每日合计不超过8小时，仅统计到昨天），再月度合计÷8，四舍五入最多保留两位小数",
        ])
        warnings = meta.get("warnings") or []
        if warnings:
            for warning in warnings:
                append_report_row(ws, ["警告", warning.get("message", "")])
        else:
            append_report_row(ws, ["状态", "本次导出未发现范围归属警告"])
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 80
    elif name == "输出给薪资":
        cols = meta["payrollSheet"]["columns"]
        append_report_row(ws, cols)
        # 新 meta 明确提供空 rows 时只写表头；仅旧 meta 缺少 rows 字段时回退 sampleRow。
        sheet = meta["payrollSheet"]
        data_rows = sheet["rows"] if "rows" in sheet else [sheet["sampleRow"]]
        for r in data_rows:
            append_report_row(ws, [_export_cell_value(c, r.get(c, "")) for c in cols])
    elif name == "输出给薪资2":
        cols = meta["payrollSheet2"]["columns"]
        append_report_row(ws, cols)
        sheet = meta["payrollSheet2"]
        data_rows = sheet["rows"] if "rows" in sheet else [sheet["sampleRow"]]
        for r in data_rows:
            append_report_row(ws, [_export_cell_value(c, r.get(c, "")) for c in cols])
    elif name in meta.get("extraSheets", {}):
        sheet = meta["extraSheets"][name]
        _append_sheet_rows(ws, sheet["columns"], sheet["rows"], "（当月无数据）")
    elif name == "班次有效工时":
        sheet = meta.get("monthlyHours") or {}
        _append_sheet_rows(
            ws,
            sheet.get("columns") or [],
            sheet.get("rows") or [],
            "（当月无班次有效数据）",
            freeze_panes="G2",
        )
    elif name == "加班明细":
        sheet = meta.get("overtimeDetail") or {}
        _append_sheet_rows(
            ws,
            sheet.get("columns") or [],
            sheet.get("rows") or [],
            "（当月无加班明细）",
        )
    elif name == "加班汇总":
        sheet = meta.get("overtimeDetail") or {}
        _append_sheet_rows(
            ws,
            sheet.get("summaryColumns") or _OVERTIME_SUMMARY_COLUMNS,
            sheet.get("summaryRows") or [],
            "（当月无加班汇总）",
        )
    elif name == "按人汇总":
        sheet = meta.get("overtimeDetail") or {}
        _append_sheet_rows(
            ws,
            sheet.get("byPersonColumns") or ["序号", "工号", "加班员工姓名"],
            sheet.get("byPersonRows") or [],
            "（当月无加班）",
            freeze_panes="D2",
        )
    else:
        append_report_row(ws, ["说明", f"{name} — 三班倒打卡导出"])
    _format_employee_id_columns_as_text(ws)


def build_export_xlsx(meta: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name in meta["sheets"]:
        _append_export_sheet(wb, meta, name)
    buf = BytesIO()
    check_report_deadline()
    wb.save(buf)
    check_report_deadline()
    return buf.getvalue()


def build_export_sheet_xlsx(meta: dict, sheet_name: str) -> bytes:
    if sheet_name not in meta["sheets"]:
        raise ExportInputError(f"未知工作表：{sheet_name}")
    wb = Workbook()
    wb.remove(wb.active)
    _append_export_sheet(wb, meta, sheet_name)
    buf = BytesIO()
    check_report_deadline()
    wb.save(buf)
    check_report_deadline()
    return buf.getvalue()


def export_health_checks(
    db: Session, *, month: str, furnace_nos: list[str], region: str | None = None
) -> tuple[str, list[dict]]:
    checks: list[dict] = []
    selected_furnace_ids: set[int] = set()
    for fn in furnace_nos:
        furnace_stmt = select(Furnace).where(Furnace.furnace_no == fn)
        if region:
            furnace_stmt = furnace_stmt.where(Furnace.region == region)
        matches = bounded_rows(db, furnace_stmt)
        if not matches:
            checks.append({"furnace": fn, "type": "missing_furnace", "message": f"{fn} 工作点不存在"})
            continue
        for f in matches:
            selected_furnace_ids.add(f.id)
            label = f"{fn}" if region else f"{f.region} {fn}"
            rules = bounded_rows(db, select(QuotaRule).where(QuotaRule.furnace_id == f.id))
            if not rules:
                checks.append({
                    "furnace": fn,
                    "region": f.region,
                    "furnaceId": f.id,
                    "type": "quota_missing",
                    "message": f"{label} 编制缺失",
                })

            locked_days = db.scalar(
                select(func.count())
                .select_from(ChiefConfirm)
                .where(
                    ChiefConfirm.furnace_id == f.id,
                    ChiefConfirm.natual_date.startswith(month),
                    ChiefConfirm.locked.is_(True),
                )
            )
            if not locked_days:
                checks.append(
                    {
                        "furnace": fn,
                        "region": f.region,
                        "furnaceId": f.id,
                        "type": "unconfirmed_days",
                        "message": f"{label} 本月尚无值班长锁定确认",
                    }
                )

            # 不再用打卡首尾差推算工时，因此导出前必须明确提示劳勤结果未覆盖的员工日。
            furnace_days = set(bounded_rows(db,
                select(AttendanceRecord.emp_id, AttendanceRecord.natual_date).where(
                    AttendanceRecord.furnace_id == f.id,
                    AttendanceRecord.natual_date.startswith(month),
                    AttendanceRecord.record_kind == "出勤",
                    scan_is_active(),
                ), scalars=False,
            ))
            if furnace_days:
                emp_ids = {employee_id for employee_id, _ in furnace_days}
                raw_days = set(bounded_rows(db,
                    select(ExternalAttendanceResultRaw.user_code, ExternalAttendanceResultRaw.kq_date).where(
                        ExternalAttendanceResultRaw.user_code.in_(emp_ids),
                        ExternalAttendanceResultRaw.source_withdrawn_at.is_(None),
                        ExternalAttendanceResultRaw.kq_date.startswith(month),
                        ExternalAttendanceResultRaw.cqgs.is_not(None),
                        func.trim(ExternalAttendanceResultRaw.cqgs) != "",
                    ), scalars=False,
                ))
                missing_result_days = furnace_days - raw_days
                if missing_result_days:
                    checks.append({
                        "furnace": fn,
                        "region": f.region,
                        "furnaceId": f.id,
                        "type": "attendance_result_missing",
                        "message": f"{label} 有 {len(missing_result_days)} 个员工日未拉取劳勤结果",
                    })

            anomalies = db.scalar(
                select(func.count())
                .select_from(ReconResult)
                .where(
                    ReconResult.date.startswith(month),
                    ReconResult.status.in_(("劳勤有工作点无", "工作点有劳勤无", "时间错位")),
                )
            )
            if anomalies and anomalies > 0:
                checks.append(
                    {
                        "furnace": fn,
                        "region": f.region,
                        "furnaceId": f.id,
                        "type": "recon_pending",
                        "message": f"{month} 仍有 {anomalies} 条未闭环对账异常",
                    }
                )

    # 员工级旷工/请假只在其当月主工作点所在文件承接。若本次范围包含该员工的
    # 跨工作点记录、却不含主工作点，必须显式提示，避免把文件里的 0 误当成“无旷工”。
    if selected_furnace_ids:
        scoped_records = bounded_rows(db,
            select(AttendanceRecord).where(
                AttendanceRecord.furnace_id.in_(selected_furnace_ids),
                AttendanceRecord.natual_date.startswith(month),
                AttendanceRecord.record_kind == "出勤",
                scan_is_active(),
            )
        )
        scoped_emp_ids = {row.emp_id for row in scoped_records}
        if scoped_emp_ids:
            all_records = bounded_rows(db,
                select(AttendanceRecord).where(
                    AttendanceRecord.emp_id.in_(scoped_emp_ids),
                    AttendanceRecord.natual_date.startswith(month),
                    AttendanceRecord.record_kind == "出勤",
                    scan_is_active(),
                )
            )
            fractions = _valid_day_fractions_bulk(db, list(all_records))
            furnace_ids = {row.furnace_id for row in all_records}
            furnace_map = {
                row.id: row
                for row in db.scalars(
                    select(Furnace).where(Furnace.id.in_(furnace_ids))
                )
            }
            owners = _dominant_furnace_by_emp(all_records, furnace_map, fractions)
            outside_owner_ids = sorted(
                emp_id
                for emp_id in scoped_emp_ids
                if owners.get(emp_id) is not None
                and owners[emp_id] not in selected_furnace_ids
            )
            unknown_owner_ids = sorted(
                emp_id for emp_id in scoped_emp_ids if owners.get(emp_id) is None
            )
            if outside_owner_ids:
                checks.append({
                    "type": "absence_owned_outside_scope",
                    "count": len(outside_owner_ids),
                    "employeeIds": outside_owner_ids,
                    "message": (
                        f"本次范围内有 {len(outside_owner_ids)} 名员工的主工作点在范围外；"
                        "其整月旷工/请假不在本文件承接"
                    ),
                })
            if unknown_owner_ids:
                checks.append({
                    "type": "absence_owner_undetermined",
                    "count": len(unknown_owner_ids),
                    "employeeIds": unknown_owner_ids,
                    "message": (
                        f"本次范围内有 {len(unknown_owner_ids)} 名员工全月无有效出勤日；"
                        "其旷工将按源小时数逐日以8小时减有效出勤和请假的剩余小时为上限，统计至昨天，合计除以8计算"
                    ),
                })

    status = "ok" if not checks else "warn"
    if not checks:
        checks.append({"type": "ok", "message": f"{month} 导出预检通过"})
    return status, checks
