"""扫码上岗自动预选：班组 / 用工类型 / 工种。

- 班组：只保留不分班优先；分班班组不再按当前时间就近预选。
- 用工类型：默认「正式」。
- 工种：常用职位(员工管理维护) → 上次打卡职位；且该工种在本工作点可选、班次匹配、正式编制未满时才预选，
  否则留空让工人自己选。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    Employee,
    EmployeeFavorite,
    Furnace,
    QuotaRule,
)
from app.services.quota_service import find_quota, resolve_quota_post
from app.services.selectable_posts import get_selectable_posts
from app.session_store import infer_role


def _is_squadless_post(db: Session, furnace_id: int, region: str, post: str) -> bool:
    """该工种在本工作点是否为不分班岗（其编制规则全部 squad 为空），与前端 squadlessPosts 口径一致。"""
    qp = resolve_quota_post(db, furnace_id=furnace_id, region=region, post=post)
    rules = db.scalars(
        select(QuotaRule).where(QuotaRule.furnace_id == furnace_id, QuotaRule.post == qp)
    ).all()
    return bool(rules) and all(r.squad is None for r in rules)


def _emp_role(db: Session, emp_id: str) -> str:
    e = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    return (e.role if e and e.role else None) or infer_role((e.dd_user_id if e else None) or emp_id)


def _suggest_post(
    db: Session, *, emp_id: str, region: str, furnace_no: str, furnace: Furnace,
    squad: str | None, squadless: bool, selectable: set[str], natual_date: str | None,
) -> str:
    candidates: list[str] = []
    # 常用职位（员工管理维护，可多个，按维护顺序）
    for ef in db.scalars(
        select(EmployeeFavorite)
        .where(EmployeeFavorite.emp_id == emp_id, EmployeeFavorite.kind == "post")
        .order_by(EmployeeFavorite.id)
    ):
        if ef.value:
            candidates.append(ef.value)
    # 上次打卡职位
    last = db.scalar(
        select(AttendanceRecord)
        .where(AttendanceRecord.emp_id == emp_id, AttendanceRecord.record_kind == "出勤")
        .order_by(AttendanceRecord.natual_date.desc(), AttendanceRecord.scan_time.desc())
    )
    if last and last.post:
        candidates.append(last.post)

    seen: set[str] = set()
    for p in candidates:
        if p in seen or p not in selectable:
            seen.add(p)
            continue
        seen.add(p)
        qp = resolve_quota_post(
            db,
            furnace_id=furnace.id,
            region=region,
            post=p,
            squad=None if squadless else squad,
        )
        # 班次匹配：该岗在所选班次（或不分班）须有编制规则
        rule = db.scalar(
            select(QuotaRule.id).where(
                QuotaRule.furnace_id == furnace.id,
                QuotaRule.post == qp,
                QuotaRule.squad.is_(None) if squadless else QuotaRule.squad == squad,
            )
        )
        if not rule:
            continue
        # 正式编制未满（默认按正式整班 8h 试位）——须按当天日期统计占用，否则跨全期累计会误判满编
        q = find_quota(
            db, region=region, furnace=furnace_no, post=p,
            squad=None if squadless else squad, incoming_hours=8.0, natual_date=natual_date,
        )
        if q and not q.get("formalAllowed", True):
            continue
        return p
    return ""


def suggest_scan_defaults(
    db: Session, *, region: str, furnace_no: str, emp_id: str, date: str | None = None
) -> dict:
    from app.services.anomaly_service import get_demo_context, resolve_demo_date

    natual_date = resolve_demo_date(date) if date else get_demo_context()["today"]
    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace_no)
    )
    if not f:
        return {"squad": None, "squadless": False, "employType": "正式", "post": ""}
    # 按该工人「角色」能选的工种判断：只有当他能选到不分班岗(值班长/维修工，受 visible_roles 限制)时
    # 才预选不分班；普通分班班组不再按时间就近预选。
    role = _emp_role(db, emp_id)
    selectable = set(get_selectable_posts(db, region=region, furnace=furnace_no, role=role))
    can_squadless = any(_is_squadless_post(db, f.id, region, p) for p in selectable)
    if can_squadless:
        squad, squadless = None, True
    else:
        squad, squadless = None, False
    post = _suggest_post(
        db, emp_id=emp_id, region=region, furnace_no=furnace_no,
        furnace=f, squad=squad, squadless=squadless, selectable=selectable,
        natual_date=natual_date,
    )
    return {"squad": squad, "squadless": squadless, "employType": "正式", "post": post}
