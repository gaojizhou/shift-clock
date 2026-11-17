"""Aggregate per-day attendance status for H5 calendar."""

from __future__ import annotations

import calendar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AttendanceRecord, Employee
from app.scan_active import ATTENDANCE_KIND_OVERTIME, scan_is_active
from app.services.recon_service import _status_with_overtime, get_recon_for
from app.services.report_snapshot import ReportSnapshot
from app.services.scan_detail import ScanDetailContext, day_scan_detail


def _month_bounds(month: str) -> tuple[str, list[int]]:
    # 校验 YYYY-MM：非法输入抛 ValueError（端点转 400），避免 int()/monthrange 抛未捕获异常→500
    m = month or ""
    if len(m) != 7 or m[4] != "-" or not m[:4].isdigit() or not m[5:7].isdigit():
        raise ValueError("月份格式应为 YYYY-MM")
    year, mon = int(m[:4]), int(m[5:7])
    if not 1 <= mon <= 12:
        raise ValueError("月份超出范围（01-12）")
    last = calendar.monthrange(year, mon)[1]
    return month, list(range(1, last + 1))


def day_status(
    db: Session, *, emp_id: str, natual_date: str, context: ReportSnapshot | None = None
) -> str:
    records = context.records_by_key.get((emp_id, natual_date), []) if context else []
    pending = (
        next((r for r in records if r.status == "待审"), None)
        if context
        else db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == natual_date,
                AttendanceRecord.status == "待审",
            )
        )
    )
    if pending:
        return "待审"

    # 有效出勤（扫码/代签/补录，status=active 且 record_kind=出勤）即为正常
    scans = (
        [r for r in records if r.status == "active" and r.record_kind == "出勤"]
        if context
        else db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == natual_date,
                scan_is_active(),
            )
        ).all()
    )
    if scans:
        return "正常"

    recon = (
        {
            "status": _status_with_overtime(
                context.base(emp_id, natual_date)["status"],
                any(
                    context.overtime_status(r) == "加班需核对"
                    for r in records
                    if r.status == "active" and r.record_kind == ATTENDANCE_KIND_OVERTIME
                ),
            )
        }
        if context
        else get_recon_for(db, emp_id, natual_date)
    )
    if recon["status"] == "时间错位":
        return "异常"
    if recon["status"] == "工作点有劳勤无":
        return "未到"
    if recon["status"] == "劳勤有工作点无":
        return "异常"
    if recon["status"] in ("无数据", "未排班"):
        return "无数据"

    return "未到"


def last_scan_context(db: Session, emp_id: str) -> dict | None:
    row = db.scalar(
        select(AttendanceRecord)
        .where(AttendanceRecord.emp_id == emp_id, scan_is_active())
        .order_by(AttendanceRecord.scan_time.desc())
        .limit(1)
    )
    if not row:
        return None
    from app.models import Furnace

    f = db.get(Furnace, row.furnace_id)
    return {
        "region": f.region if f else "",
        "furnace": f.furnace_no if f else "",
        "squad": row.squad,
        "post": row.post,
    }


def get_attendance(db: Session, *, userid: str, month: str) -> dict:
    month_prefix, days = _month_bounds(month)
    emp = db.scalar(select(Employee).where(Employee.emp_id == userid))
    context = ReportSnapshot.for_month(db, month_prefix, {userid})
    details = ScanDetailContext(db, userid, f"{month_prefix}-01", f"{month_prefix}-{days[-1]:02d}")
    day_items = []
    for d in days:
        natual_date = f"{month_prefix}-{d:02d}"
        st = day_status(db, emp_id=userid, natual_date=natual_date, context=context)
        detail = day_scan_detail(db, emp_id=userid, natual_date=natual_date, context=details)
        day_items.append(
            {
                "day": d,
                "date": natual_date,
                "status": st,
                "canMakeup": st in ("未到", "异常", "无数据"),
                "detail": detail,
            }
        )
    ctx = last_scan_context(db, userid)
    return {
        "userid": userid,
        "name": emp.name if emp else userid,
        "month": month,
        "days": day_items,
        "lastScan": ctx,
    }
