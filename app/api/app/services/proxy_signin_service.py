"""代签 (chief proxy sign-in) monthly quota.

Effective limit per worker/month = global default (runtime-editable in app_setting,
falling back to config) + sum of temporary grants for that worker+month.
Usage = count of 代签 MakeupRequest (status 通过) in that month.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.config import settings
from app.models import AppSetting, AttendanceRecord, AuditLog, Employee, ProxySigninGrant

DEFAULT_LIMIT_KEY = "proxy_signin_monthly_limit"


def lock_emp_month(db, emp_id: str, month: str) -> None:
    """Serialize concurrent 代签 quota ops for the same worker+month.

    Uses a Postgres transaction-scoped advisory lock keyed on (emp_id, month) so the
    count→insert→commit window can't interleave (avoids exceeding the monthly limit
    under concurrency). The lock auto-releases at commit/rollback. No-op on non-Postgres
    backends (e.g. SQLite in tests), which are single-threaded anyway.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    digest = hashlib.blake2b(f"{emp_id}|{month}".encode(), digest_size=8).digest()
    key = int.from_bytes(digest, "big", signed=True)  # 64-bit signed -> bigint
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def month_of(date: str) -> str:
    """YYYY-MM-DD -> YYYY-MM (attendance month, per T-2 reconciliation)."""
    return (date or "")[:7]


def get_default_limit(db) -> int:
    row = db.get(AppSetting, DEFAULT_LIMIT_KEY)
    if row is not None:
        try:
            return int(row.value)
        except (TypeError, ValueError):
            pass
    return settings.fdc_proxy_signin_monthly_limit


def set_default_limit(db, value: int, actor: str) -> int:
    if value < 0:
        raise HTTPException(400, "limit must be >= 0")
    old = get_default_limit(db)
    row = db.get(AppSetting, DEFAULT_LIMIT_KEY)
    if row is None:
        db.add(AppSetting(key=DEFAULT_LIMIT_KEY, value=str(value)))
    else:
        row.value = str(value)
    db.add(
        AuditLog(
            actor=actor,
            action="proxy_limit_change",
            payload=_json({"from": old, "to": value}),
        )
    )
    db.commit()
    return value


def count_proxy_signins(db, emp_id: str, month: str) -> int:
    # 仅计「仍有效」的代签出勤：并表后代签即一条 source=代签 的出勤记录，
    # 被 exclude 置「已取消」即不再计入（scan_is_active 过滤 active+出勤），额度随之释放。
    from app.scan_active import scan_is_active

    return (
        db.scalar(
            select(func.count())
            .select_from(AttendanceRecord)
            .where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.source == "代签",
                AttendanceRecord.natual_date.like(f"{month}-%"),
                scan_is_active(),
            )
        )
        or 0
    )


def get_extra(db, emp_id: str, month: str) -> int:
    return (
        db.scalar(
            select(func.coalesce(func.sum(ProxySigninGrant.extra), 0)).where(
                ProxySigninGrant.emp_id == emp_id,
                ProxySigninGrant.month == month,
            )
        )
        or 0
    )


def quota_for(db, emp_id: str, date: str) -> dict:
    month = month_of(date)
    default = get_default_limit(db)
    extra = get_extra(db, emp_id, month)
    used = count_proxy_signins(db, emp_id, month)
    limit = default + extra
    return {
        "empId": emp_id,
        "month": month,
        "used": used,
        "default": default,
        "extra": extra,
        "limit": limit,
        "remaining": max(limit - used, 0),
    }


def assert_can_proxy(db, emp_id: str, date: str) -> dict:
    q = quota_for(db, emp_id, date)
    if q["used"] >= q["limit"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PROXY_LIMIT_EXCEEDED",
                "message": f"本月该员工代签已达上限（{q['limit']}次）",
                **q,
            },
        )
    return q


def add_grant(db, emp_id: str, month: str, extra: int, reason: str | None, actor: str) -> dict:
    if extra <= 0:
        raise HTTPException(400, "extra must be > 0")
    # 校验员工存在，避免给不存在工号授额度产生无主脏数据
    if not db.scalar(select(Employee).where(Employee.emp_id == emp_id, Employee.deleted.is_(False))):
        raise HTTPException(404, f"员工不存在：{emp_id}")
    lock_emp_month(db, emp_id, month)  # consistent with concurrent approve on same worker+month
    db.add(
        ProxySigninGrant(
            emp_id=emp_id,
            month=month,
            extra=extra,
            reason=reason,
            actor=actor,
            ts=datetime.now(timezone.utc),
        )
    )
    db.add(
        AuditLog(
            actor=actor,
            action="proxy_grant",
            payload=_json({"empId": emp_id, "month": month, "extra": extra, "reason": reason or ""}),
        )
    )
    db.commit()
    return quota_for(db, emp_id, f"{month}-01")


def usage(db, month: str) -> dict:
    """Per-worker usage for a month: anyone with 代签 or a grant in that month."""
    default = get_default_limit(db)
    names = {e.emp_id: e.name for e in db.scalars(select(Employee)).all()}

    # 与 count_proxy_signins 一致：仅计「仍有效」的代签出勤，撤回后该用量随之释放，
    # 避免用量列表 used 与 quota_for 口径不一致、误导加量决策。
    from app.scan_active import scan_is_active

    used_rows = db.execute(
        select(AttendanceRecord.emp_id, func.count())
        .where(
            AttendanceRecord.source == "代签",
            AttendanceRecord.natual_date.like(f"{month}-%"),
            scan_is_active(),
        )
        .group_by(AttendanceRecord.emp_id)
    ).all()
    used_map = {emp: cnt for emp, cnt in used_rows}

    grant_rows = db.execute(
        select(ProxySigninGrant.emp_id, func.coalesce(func.sum(ProxySigninGrant.extra), 0))
        .where(ProxySigninGrant.month == month)
        .group_by(ProxySigninGrant.emp_id)
    ).all()
    extra_map = {emp: int(s) for emp, s in grant_rows}

    items = []
    for emp_id in sorted(set(used_map) | set(extra_map)):
        used = int(used_map.get(emp_id, 0))
        extra = extra_map.get(emp_id, 0)
        limit = default + extra
        items.append(
            {
                "empId": emp_id,
                "name": names.get(emp_id),
                "used": used,
                "default": default,
                "extra": extra,
                "limit": limit,
                "remaining": max(limit - used, 0),
                "atLimit": used >= limit,
            }
        )
    items.sort(key=lambda x: x["used"], reverse=True)
    return {"month": month, "default": default, "items": items}


def _json(obj: dict) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
