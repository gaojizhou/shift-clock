"""共享的员工月度旷工人工值；None 表示恢复自动计算，0 是有效覆盖值。"""
import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import ConflictError, DomainError
from app.models import AttendanceRecord, AuditLog, MonthlyAbsenceOverride, MonthlyExportLock
from app.services.monthly_export_lock_service import normalize_month


class AbsencePermissionError(DomainError):
    status_code = 403


def get_absence_override_values(db: Session, month: str, employee_ids) -> dict[str, Decimal]:
    if not employee_ids:
        return {}
    return {row.emp_id: row.days for row in db.scalars(select(MonthlyAbsenceOverride).where(
        MonthlyAbsenceOverride.month == month,
        MonthlyAbsenceOverride.emp_id.in_(employee_ids),
        MonthlyAbsenceOverride.days.is_not(None),
    ))}


def apply_absence_overrides(calculated: dict, saved: dict[str, Decimal],
                            limits: dict[str, Decimal]) -> dict:
    # 同步/补录可在保存后改变可用天数；下载时也校验，禁止旧的超限值进入薪资文件。
    from app.services.export_service import ExportInputError
    result = dict(calculated)
    for employee_id, days in saved.items():
        limit = limits[employee_id]
        if days > limit:
            raise ExportInputError(
                f"员工 {employee_id} 的手动旷工 {days} 天超过当前可用上限 {limit} 天，"
                "请在手动输入列修改或清空后重新导出"
            )
        result[employee_id] = float(days)
    return result


def _employee_limits(db: Session, month: str, employee_ids) -> dict[str, Decimal]:
    # 复用报表整月全工作点的只读对账与逐日余额口径。
    from app.services.export_service import _monthly_absence_limits, _valid_day_fractions_bulk
    from app.services.report_snapshot import ReportSnapshot
    context = ReportSnapshot.for_month(db, month, set(employee_ids))
    records = [r for r in context.records if r.status == "active" and r.record_kind == "出勤"]
    fractions = _valid_day_fractions_bulk(db, records, context)
    return _monthly_absence_limits(context, month, fractions, employee_ids)


def _employee_furnaces(db: Session, month: str, employee_ids=None, furnace_ids=None):
    query = select(AttendanceRecord.emp_id, AttendanceRecord.furnace_id).where(
        AttendanceRecord.natual_date.startswith(month),
        AttendanceRecord.status == "active",
        AttendanceRecord.record_kind.in_(["出勤", "加班"]),
    ).distinct()
    if employee_ids is not None:
        query = query.where(AttendanceRecord.emp_id.in_(employee_ids))
    if furnace_ids is not None:
        query = query.where(AttendanceRecord.furnace_id.in_(furnace_ids))
    result = {}
    for emp_id, furnace_id in db.execute(query):
        result.setdefault(emp_id, set()).add(furnace_id)
    return result


def list_absence_overrides(db: Session, month: str, visible_furnaces: list[int], allowed_furnaces: list[int]):
    month = normalize_month(month)
    visible = _employee_furnaces(db, month, furnace_ids=visible_furnaces)
    if not visible:
        return {"items": {}}
    all_furnaces = _employee_furnaces(db, month, employee_ids=set(visible))
    locked = set(db.scalars(select(MonthlyExportLock.furnace_id).where(
        MonthlyExportLock.month == month, MonthlyExportLock.locked.is_(True),
    )))
    saved = {row.emp_id: row for row in db.scalars(select(MonthlyAbsenceOverride).where(
        MonthlyAbsenceOverride.month == month, MonthlyAbsenceOverride.emp_id.in_(visible),
    ))}
    limits = _employee_limits(db, month, visible)
    items = {}
    for emp_id, furnace_ids in all_furnaces.items():
        row = saved.get(emp_id)
        reason = ""
        if not furnace_ids.issubset(set(allowed_furnaces)):
            reason = "员工当月涉及其他地区，请管理员修改共享旷工值"
        elif furnace_ids & locked:
            reason = "员工当月涉及已封账工作点，不能修改"
        items[emp_id] = {"days": float(row.days) if row and row.days is not None else None,
                         "revision": row.revision if row else 0, "maxDays": float(limits[emp_id]),
                         "editable": not reason, "reason": reason,
                         "updatedBy": row.updated_by if row else None}
    return {"items": items}


def save_absence_override(db: Session, *, month: str, emp_id: str, days: Decimal | None,
                          revision: int, actor: str, allowed_furnaces: list[int]):
    month = normalize_month(month)
    if days is not None:
        if not days.is_finite() or days < 0 or days > 31 or days != days.quantize(Decimal("0.01")):
            raise DomainError("旷工天数须为非负数，最多两位小数且不超过当月可用上限")
    furnace_ids = _employee_furnaces(db, month, employee_ids=[emp_id]).get(emp_id, set())
    if not furnace_ids:
        raise DomainError("该员工当月没有可导出记录")
    if not furnace_ids.issubset(set(allowed_furnaces)):
        raise AbsencePermissionError("无权修改该员工涉及其他地区的共享旷工值")
    # 与封账共用工作点记录锁，避免保存与封账请求交错。
    from app.models import Furnace
    list(db.scalars(select(Furnace).where(Furnace.id.in_(furnace_ids)).order_by(Furnace.id).with_for_update()))
    if db.scalar(select(MonthlyExportLock.id).where(
        MonthlyExportLock.month == month, MonthlyExportLock.furnace_id.in_(furnace_ids),
        MonthlyExportLock.locked.is_(True),
    )):
        raise ConflictError("员工当月涉及已封账工作点，不能修改")
    limit = _employee_limits(db, month, [emp_id])[emp_id]
    if days is not None and days > limit:
        raise DomainError(f"旷工天数最多可填 {limit} 天（统计至昨天，扣除全工作点有效出勤和请假），请调整或清空")
    key = {"month": month, "emp_id": emp_id}
    existing = db.get(MonthlyAbsenceOverride, (month, emp_id))
    old_days = float(existing.days) if existing and existing.days is not None else None
    now = datetime.now(timezone.utc)
    try:
        if revision == 0:
            if existing:
                raise ConflictError("该旷工值已被他人修改，请刷新后重试")
            db.add(MonthlyAbsenceOverride(**key, days=days, revision=1, updated_by=actor, updated_at=now))
            db.flush()
        else:
            result = db.execute(update(MonthlyAbsenceOverride).where(
                MonthlyAbsenceOverride.month == month, MonthlyAbsenceOverride.emp_id == emp_id,
                MonthlyAbsenceOverride.revision == revision,
            ).values(days=days, revision=revision + 1, updated_by=actor, updated_at=now))
            if result.rowcount != 1:
                raise ConflictError("该旷工值已被他人修改，请刷新后重试")
        db.add(AuditLog(actor=actor, action="monthly_absence_override", payload=json.dumps({
            "month": month, "empId": emp_id, "before": old_days,
            "after": float(days) if days is not None else None, "revision": revision + 1,
        }, ensure_ascii=False)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("该旷工值已被他人修改，请刷新后重试")
    return {"days": float(days) if days is not None else None, "revision": revision + 1,
            "editable": True, "reason": "", "updatedBy": actor, "maxDays": float(limit)}
