"""Selectable post whitelist（由各岗位「扫码可选」开关决定，开关说了算）。"""

from sqlalchemy import distinct, or_, select
from sqlalchemy.orm import Session

from app.models import Furnace, QuotaRule, SelectablePost

# 仅作为参考保留（前端默认：分班岗可选、不分班岗不可选）；不再用于强制过滤。
MANAGEMENT_POSTS = frozenset({"值班长", "班组长", "副班组长", "维修工"})

DEFAULT_REGION_POSTS = (
    "出料工",
    "供气工",
    "物料转运工",
    "加料操作工",
)


def _posts_from_quota_rules(db: Session, *, furnace_id: int) -> list[str]:
    """无显式名单时的默认：分班岗位（squad 非空）默认可选，不分班岗位默认不可选。"""
    rows = db.scalars(
        select(distinct(QuotaRule.post)).where(
            QuotaRule.furnace_id == furnace_id,
            QuotaRule.squad.isnot(None),
        )
    ).all()
    return sorted(p for p in rows if p)


def _roles_of(value: str | None) -> set[str]:
    return {r for r in (value or "").split(",") if r}


def get_visible_posts(db: Session, *, region: str, furnace: str) -> list[dict]:
    """该工作点「可选岗 + 可见角色」明细（不过滤角色，供后台编辑回填多选）。

    取值优先级同 get_selectable_posts：工作点级 selectable_post 优先，否则地区级。
    返回 [{"post": 岗位, "roles": [role.code, ...]}]。
    """
    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace)
    )
    furnace_no = f.furnace_no if f else furnace

    rows = db.scalars(
        select(SelectablePost)
        .where(SelectablePost.region == region, SelectablePost.furnace_no == furnace_no)
        .order_by(SelectablePost.id)
    ).all()
    if not rows:
        rows = db.scalars(
            select(SelectablePost)
            .where(SelectablePost.region == region, SelectablePost.furnace_no.is_(None))
            .order_by(SelectablePost.id)
        ).all()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r.post, [x for x in (r.visible_roles or "").split(",") if x])
    return [{"post": p, "roles": roles} for p, roles in out.items()]


def get_selectable_posts(
    db: Session, *, region: str, furnace: str, role: str | None = "worker"
) -> list[str]:
    """扫码可选岗位（按可见角色过滤）。

    role：要查看的角色 code，仅返回「role ∈ 该岗 visible_roles」的岗位。
      传 None=不过滤（管理端「看全部可选岗」用）；默认 "worker"（向后兼容旧的工人扫码语义）。
    """

    def visible(vr: str | None) -> bool:
        return role is None or role in _roles_of(vr)

    # 无显式名单时的兜底岗位按旧语义视为工人可选
    worker_scope = role is None or role == "worker"

    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace)
    )
    furnace_no = f.furnace_no if f else furnace

    if f:
        # 1) 工作点级「可选岗」名单优先（编辑当前工作点编制 / 批量应用模板时写入），开关说了算
        furnace_rows = db.scalars(
            select(SelectablePost)
            .where(SelectablePost.region == region, SelectablePost.furnace_no == furnace_no)
            .order_by(SelectablePost.id)
        ).all()
        if furnace_rows:
            return list(dict.fromkeys(r.post for r in furnace_rows if visible(r.visible_roles)))

        # 2) 否则按编制岗位默认（分班岗默认工人可选）
        from_quota = _posts_from_quota_rules(db, furnace_id=f.id)
        if from_quota:
            return from_quota if worker_scope else []

    rows = db.scalars(
        select(SelectablePost)
        .where(
            SelectablePost.region == region,
            or_(
                SelectablePost.furnace_no.is_(None),
                SelectablePost.furnace_no == furnace_no,
            ),
        )
        .order_by(SelectablePost.id)
    ).all()
    if rows:
        return list(dict.fromkeys(r.post for r in rows if visible(r.visible_roles)))

    return list(DEFAULT_REGION_POSTS) if worker_scope else []
