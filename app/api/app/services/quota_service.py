from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.domain.post_names import quota_post, scan_posts_for_quota
from app.domain.quota import allowed_employ_types, capacity_full, check_formal_allowed
from app.models import AttendanceRecord, Furnace, QuotaRule
from app.scan_active import STANDARD_SHIFT_HOURS, counts_toward_capacity


def _capped_hours():
    """单条工时对标准班时封顶（>8h 的加班部分不占额外编制位）。SQLite/PG 通用。"""
    wh = func.coalesce(AttendanceRecord.work_hours, STANDARD_SHIFT_HOURS)
    return case((wh > STANDARD_SHIFT_HOURS, STANDARD_SHIFT_HOURS), else_=wh)


def resolve_quota_post(
    db: Session,
    *,
    furnace_id: int,
    region: str,
    post: str,
    squad: str | None = None,
) -> str:
    """Resolve a UI/scan post to the actual quota_rule.post for this furnace.

    The live quota table is authoritative. Legacy regional aliases are only a fallback
    for old templates/data that still use names like 示例厂区B「物料工」.
    """
    candidates = [post]
    alias = quota_post(region, post)
    if alias != post:
        candidates.append(alias)
    for cand in candidates:
        stmt = select(QuotaRule.id).where(
            QuotaRule.furnace_id == furnace_id,
            QuotaRule.post == cand,
        )
        if squad is not None:
            stmt = stmt.where((QuotaRule.squad == squad) | (QuotaRule.squad.is_(None)))
        if db.scalar(stmt):
            return cand
    return post


def find_quota(
    db: Session,
    *,
    region: str,
    furnace: str,
    post: str,
    squad: str | None = None,
    natual_date: str | None = None,
    incoming_hours: float | None = None,
) -> dict | None:
    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace)
    )
    if not f:
        return None

    canon = resolve_quota_post(
        db,
        furnace_id=f.id,
        region=region,
        post=post,
        squad=squad,
    )
    q_stmt = select(QuotaRule).where(QuotaRule.furnace_id == f.id, QuotaRule.post == canon)
    if squad:
        q_stmt = q_stmt.where(QuotaRule.squad == squad)
    else:
        q_stmt = q_stmt.where(QuotaRule.squad.is_(None))
    q = db.scalar(q_stmt)
    if not q:
        return None

    post_names = scan_posts_for_quota(region, canon)
    # 算薪占用按工时汇总后换算编制位：整班=8h=1位，半班=4h=0.5位，两个半班合一位
    # （旧数据 work_hours 为空按整班 8h）。编制(capacity)仍以「人/位」计，故 ÷ 标准班时。
    # 口径含正式加班(counts_toward_capacity)；单条工时对 8h 封顶，避免 12h 占 1.5 位挤占他人。
    formal_stmt = select(
        func.coalesce(func.sum(_capped_hours()), 0.0)
    ).where(
        AttendanceRecord.furnace_id == f.id,
        AttendanceRecord.post.in_(post_names),
        AttendanceRecord.squad == squad if squad else AttendanceRecord.squad.is_(None),
        AttendanceRecord.employ_type == "正式",
        counts_toward_capacity(),
    )
    if natual_date:
        formal_stmt = formal_stmt.where(AttendanceRecord.natual_date == natual_date)
    formal_used_hours = float(db.scalar(formal_stmt) or 0.0)
    formal_used = formal_used_hours / STANDARD_SHIFT_HOURS  # 工时 → 编制位（0.5/1.0…）
    incoming_fraction = (
        incoming_hours / STANDARD_SHIFT_HOURS if incoming_hours is not None else None
    )
    squadless = q.squad is None
    formal_allowed = check_formal_allowed(q.capacity, formal_used, "正式", incoming_fraction)
    return {
        "capacity": q.capacity,
        "squad": q.squad,
        "squadless": squadless,
        # 兼容旧字段名：formalUsed 现为按工时汇总后换算的编制位数（可能含小数）
        "formalUsed": formal_used,
        "formalUsedFraction": formal_used,
        "formalAllowed": formal_allowed,
        "capacityFull": capacity_full(q.capacity, formal_used),
        "allowedEmployTypes": allowed_employ_types(q.capacity, formal_used),
    }
