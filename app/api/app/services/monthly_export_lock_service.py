import calendar
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ConflictError, DomainError
from app.models import AttendanceRecord, AuditLog, Employee, Furnace, MonthlyExportLock

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_YEAR_RE = re.compile(r"^\d{4}$")
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-\d{2}$")


def normalize_month(month: str) -> str:
    m = (month or "").strip()
    if not _MONTH_RE.match(m):
        raise DomainError("月份格式应为 YYYY-MM")
    return m


def month_of_date(date: str) -> str:
    d = (date or "").strip()
    if not _DATE_RE.match(d):
        raise DomainError("日期格式应为 YYYY-MM-DD")
    try:
        y, m, day = int(d[:4]), int(d[5:7]), int(d[8:10])
        if day < 1 or day > calendar.monthrange(y, m)[1]:
            raise ValueError
    except ValueError:
        raise DomainError("日期格式应为 YYYY-MM-DD")
    return d[:7]


def _resolve_furnace_ids(db: Session, furnace_ids: list[int] | None) -> list[int]:
    if furnace_ids is None:
        return list(db.scalars(select(Furnace.id).order_by(Furnace.id)).all())
    ids = list(dict.fromkeys(int(fid) for fid in furnace_ids))
    if not ids:
        return []
    existing = set(db.scalars(select(Furnace.id).where(Furnace.id.in_(ids))).all())
    missing = [fid for fid in ids if fid not in existing]
    if missing:
        raise DomainError(f"工作点不存在：{','.join(str(fid) for fid in missing)}")
    return ids


def _status_payload(month: str, furnace_id: int, row: MonthlyExportLock | None) -> dict:
    return {
        "month": month,
        "furnaceId": furnace_id,
        "locked": bool(row and row.locked),
        "lockedBy": row.locked_by if row else None,
        "lockedAt": row.locked_at.isoformat() if row and row.locked_at else None,
        "unlockedBy": row.unlocked_by if row else None,
        "unlockedAt": row.unlocked_at.isoformat() if row and row.unlocked_at else None,
    }


def month_lock_status(
    db: Session,
    month: str,
    furnace_ids: list[int] | None = None,
) -> dict:
    m = normalize_month(month)
    ids = _resolve_furnace_ids(db, furnace_ids)
    rows = (
        db.scalars(
            select(MonthlyExportLock).where(
                MonthlyExportLock.month == m,
                MonthlyExportLock.furnace_id.in_(ids),
            )
        ).all()
        if ids
        else []
    )
    by_furnace = {row.furnace_id: row for row in rows}
    items = [_status_payload(m, fid, by_furnace.get(fid)) for fid in ids]
    return {
        "month": m,
        "lockedCount": sum(1 for item in items if item["locked"]),
        "totalCount": len(items),
        "items": items,
    }


def year_lock_statuses(
    db: Session,
    year: str,
    furnace_ids: list[int] | None = None,
) -> dict:
    y = (year or "").strip()
    if not _YEAR_RE.match(y):
        raise DomainError("年份格式应为 YYYY")
    months = [f"{y}-{month:02d}" for month in range(1, 13)]
    ids = _resolve_furnace_ids(db, furnace_ids)
    locked_rows = (
        db.execute(
            select(MonthlyExportLock.month, MonthlyExportLock.furnace_id).where(
                MonthlyExportLock.month.in_(months),
                MonthlyExportLock.furnace_id.in_(ids),
                MonthlyExportLock.locked.is_(True),
            )
        ).all()
        if ids
        else []
    )
    locked_count = Counter(row.month for row in locked_rows)
    return {
        "year": y,
        "items": [
            {
                "month": month,
                "lockedCount": locked_count[month],
                "totalCount": len(ids),
            }
            for month in months
        ],
    }


def set_month_locks(
    db: Session,
    *,
    month: str,
    furnace_ids: list[int],
    locked: bool,
    actor: str,
) -> dict:
    m = normalize_month(month)
    ids = _resolve_furnace_ids(db, furnace_ids)
    if not ids:
        raise DomainError("至少选择一个工作点")
    # 与月度人工旷工保存串行化，防止封账与修改交错。
    list(db.scalars(select(Furnace).where(Furnace.id.in_(ids)).order_by(Furnace.id).with_for_update()))
    now = datetime.now(timezone.utc)
    existing = {
        row.furnace_id: row
        for row in db.scalars(
            select(MonthlyExportLock).where(
                MonthlyExportLock.month == m,
                MonthlyExportLock.furnace_id.in_(ids),
            )
        ).all()
    }
    changed = 0
    for furnace_id in ids:
        row = existing.get(furnace_id)
        if not row:
            if not locked:
                continue
            row = MonthlyExportLock(month=m, furnace_id=furnace_id)
            db.add(row)
        if row.locked != locked:
            changed += 1
        row.locked = locked
        row.updated_at = now
        if locked:
            row.locked_by = actor
            row.locked_at = now
        else:
            row.unlocked_by = actor
            row.unlocked_at = now

    db.add(
        AuditLog(
            actor=actor,
            action="monthly_export_lock" if locked else "monthly_export_unlock",
            payload=json.dumps(
                {
                    "month": m,
                    "furnaceIds": ids,
                    "locked": locked,
                    "changed": changed,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {"month": m, "furnaceIds": ids, "locked": locked, "changed": changed}


def assert_month_export_unlocked(db: Session, date: str, furnace_id: int) -> None:
    month = month_of_date(date)
    row = db.scalar(
        select(MonthlyExportLock).where(
            MonthlyExportLock.month == month,
            MonthlyExportLock.furnace_id == furnace_id,
            MonthlyExportLock.locked.is_(True),
        )
    )
    if row:
        furnace = db.get(Furnace, furnace_id)
        label = f"{furnace.region} {furnace.furnace_no}" if furnace else f"工作点 {furnace_id}"
        raise ConflictError(
            f"{month} {label}月度导出已锁定，不能修改会影响导出结果的数据；请先由 HR/Admin 解锁"
        )


def months_between(date_from: str, date_to: str) -> list[str]:
    start_month = month_of_date(date_from)
    end_month = month_of_date(date_to)
    if date_from > date_to:
        raise DomainError("dateFrom must be <= dateTo")
    sy, sm = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1
            m = 1
    return out


def assert_month_range_unlocked(
    db: Session,
    date_from: str,
    date_to: str,
    furnace_ids: list[int] | None = None,
) -> None:
    months = months_between(date_from, date_to)
    stmt = select(MonthlyExportLock.month, MonthlyExportLock.furnace_id).where(
        MonthlyExportLock.month.in_(months),
        MonthlyExportLock.locked.is_(True),
    )
    if furnace_ids is not None:
        if not furnace_ids:
            return
        stmt = stmt.where(MonthlyExportLock.furnace_id.in_(furnace_ids))
    locked = db.execute(stmt).all()
    if locked:
        counts = Counter(row.month for row in locked)
        joined = "、".join(f"{month}（{counts[month]}个工作点）" for month in sorted(counts))
        raise ConflictError(f"{joined}月度导出已锁定，不能拉取或转换会影响导出结果的数据")


def locked_furnace_ids_for_range(
    db: Session,
    date_from: str,
    date_to: str,
    furnace_ids: list[int] | None = None,
) -> list[int]:
    """日期范围覆盖的各月中，给定工作点范围内已封账的工作点 id（furnace_ids=None 表示全部工作点）。"""
    months = months_between(date_from, date_to)
    stmt = select(MonthlyExportLock.furnace_id).where(
        MonthlyExportLock.month.in_(months),
        MonthlyExportLock.locked.is_(True),
    )
    if furnace_ids is not None:
        if not furnace_ids:
            return []
        stmt = stmt.where(MonthlyExportLock.furnace_id.in_(furnace_ids))
    return sorted(set(db.scalars(stmt).all()))


def _month_window(date_from: str, date_to: str) -> tuple[str, str]:
    """把日期范围扩展为覆盖的完整自然月（考勤结果同步的资格按整月判定，收窄同口径）。"""
    start = datetime.strptime(date_from[:7] + "-01", "%Y-%m-%d").date()
    end = datetime.strptime(date_to[:7] + "-01", "%Y-%m-%d").date()
    end = (end.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def inherit_locked_months_for_region(
    db: Session,
    furnace_id: int,
    region_name: str,
    actor: str = "system:inherit-lock",
) -> list[str]:
    """把目标地区已封账月份补建到指定工作点，避免新建/迁入工作点绕过月锁。"""
    locked_months = db.scalars(
        select(MonthlyExportLock.month)
        .join(Furnace, Furnace.id == MonthlyExportLock.furnace_id)
        .where(
            Furnace.region == region_name,
            MonthlyExportLock.locked.is_(True),
        )
        .distinct()
    ).all()
    now = datetime.now(timezone.utc)
    added: list[str] = []
    for month in sorted(set(locked_months)):
        row = db.scalar(
            select(MonthlyExportLock).where(
                MonthlyExportLock.month == month,
                MonthlyExportLock.furnace_id == furnace_id,
            )
        )
        if row is None:
            db.add(
                MonthlyExportLock(
                    month=month,
                    furnace_id=furnace_id,
                    locked=True,
                    locked_by=actor,
                    locked_at=now,
                )
            )
            added.append(month)
        elif not row.locked:
            row.locked = True
            row.locked_by = actor
            row.locked_at = now
            added.append(month)
    return added


def locked_furnace_employee_codes(
    db: Session,
    date_from: str,
    date_to: str,
    furnace_ids: list[int],
) -> set[str]:
    """已封账工作点在请求区间与封账月份交集内有有效/待审记录的员工工号。

    这些员工的拉取/转换必须整体排除：同一员工可能同时有已锁与未锁工作点的记录，
    只同步一半会改写已锁工作点的劳勤源数据和对账结果。
    窗口按实际封账月份逐月计算后再与请求区间求交，避免跨月手工拉取把未封账
    月份的员工一并剔除。
    """
    if not furnace_ids:
        return set()
    months = months_between(date_from, date_to)
    locked_months = set(
        db.scalars(
            select(MonthlyExportLock.month).where(
                MonthlyExportLock.furnace_id.in_(furnace_ids),
                MonthlyExportLock.month.in_(months),
                MonthlyExportLock.locked.is_(True),
            )
        ).all()
    )
    codes: set[str] = set()
    for month in locked_months:
        year, mon = int(month[:4]), int(month[5:7])
        m_from = f"{month}-01"
        m_to = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"
        w_from = max(date_from, m_from)
        w_to = min(date_to, m_to)
        if w_from > w_to:
            continue
        rows = db.scalars(
            select(AttendanceRecord.emp_id)
            .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
            .where(
                AttendanceRecord.natual_date >= w_from,
                AttendanceRecord.natual_date <= w_to,
                AttendanceRecord.status.in_(("active", "待审")),
                AttendanceRecord.furnace_id.in_(furnace_ids),
                Employee.deleted.is_(False),
            )
            .distinct()
        ).all()
        codes.update(code for code in rows if code)
    return codes


def all_active_employee_codes(db: Session) -> set[str]:
    """未删除员工的全部工号（全局拉取不收窄地区时的基础集合）。"""
    return {
        code
        for code in db.scalars(
            select(Employee.emp_id).where(Employee.deleted.is_(False))
        ).all()
        if code
    }
