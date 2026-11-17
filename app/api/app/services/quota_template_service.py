"""C4: 编制按设备类型模板 + 批量应用到工作点。

模板按设备类型存单条/岗位（三班同编制故只存单班值）。应用时：
- squadless 岗位(值班长/维修工)→ 生成 1 条 QuotaRule(squad=None)
- 分班岗位 → 展开 1/2/3 班三条同 capacity 的 QuotaRule
覆盖式：先删目标工作点旧 QuotaRule 再按模板重写。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Furnace,
    FurnaceType,
    QuotaRule,
    QuotaTemplate,
    QuotaTemplateScope,
    Region,
    SelectablePost,
)

SQUADS = ("1班", "2班", "3班")


def _roles_to_str(roles) -> str:
    """角色列表 → 逗号拼接去重保序的 role.code 串。"""
    return ",".join(dict.fromkeys(str(r).strip() for r in (roles or []) if str(r).strip()))


def _roles_from_str(value: str | None) -> list[str]:
    return [r for r in (value or "").split(",") if r]


def _default_visible_roles(squadless: bool) -> str:
    """旧 selectable 缺省：分班岗→worker、不分班岗→chief。"""
    return "chief" if squadless else "worker"


def _ftype_by_name(db: Session, name: str) -> FurnaceType | None:
    return db.scalar(
        select(FurnaceType).where(FurnaceType.name == name, FurnaceType.deleted.is_(False))
    )


def _template_rows(
    db: Session, *, furnace_type_code: str, region: str | None
) -> list[QuotaTemplate]:
    """地区模板优先；地区尚无模板时读取历史全局模板。

    0079 会复制现有全局模板，但读侧兜底仍覆盖新增地区、迁移前滚动发布以及
    部分迁移数据，避免地区页面再次出现空模板/批量应用 404。
    """
    rows = list(
        db.scalars(
            select(QuotaTemplate)
            .where(
                QuotaTemplate.furnace_type_code == furnace_type_code,
                QuotaTemplate.region == region,
            )
            .order_by(QuotaTemplate.post)
        ).all()
    )
    if rows or region is None:
        return rows
    initialized = db.scalar(
        select(QuotaTemplateScope.id).where(
            QuotaTemplateScope.region == region,
            QuotaTemplateScope.furnace_type_code == furnace_type_code,
        )
    )
    if initialized is not None:
        return []
    return list(
        db.scalars(
            select(QuotaTemplate)
            .where(
                QuotaTemplate.furnace_type_code == furnace_type_code,
                QuotaTemplate.region.is_(None),
            )
            .order_by(QuotaTemplate.post)
        ).all()
    )


def list_templates(
    db: Session, *, furnace_type: str, region: str | None = None
) -> list[dict]:
    """furnace_type 入参为字典名称（API 契约不变）；存储仅有 furnace_type_code。"""
    ft = _ftype_by_name(db, furnace_type)
    if not ft:
        return []
    rows = _template_rows(db, furnace_type_code=ft.code, region=region)
    return [
        {
            "id": t.id,
            "region": t.region,
            "inherited": bool(region is not None and t.region is None),
            "furnaceType": ft.name,
            "furnaceTypeCode": ft.code,
            "post": t.post,
            "capacity": t.capacity,
            "capacity2": t.capacity2 if t.capacity2 is not None else t.capacity,
            "capacity3": t.capacity3 if t.capacity3 is not None else t.capacity,
            "squadless": t.squadless,
            "visibleRoles": _roles_from_str(t.visible_roles),
        }
        for t in rows
    ]


def upsert_templates(
    db: Session,
    *,
    furnace_type: str,
    items: list[dict],
    region: str | None = None,
    actor: str | None = None,
) -> list[dict]:
    """覆盖式写入某地区、某设备类型的模板，不触碰其它地区。"""
    ft = _ftype_by_name(db, furnace_type)
    if not ft:
        raise ValueError("furnace type not in dict")
    existing = db.scalars(
        select(QuotaTemplate).where(
            QuotaTemplate.furnace_type_code == ft.code,
            QuotaTemplate.region == region,
        )
    ).all()
    for row in existing:
        db.delete(row)
    if region is not None and db.scalar(
        select(QuotaTemplateScope.id).where(
            QuotaTemplateScope.region == region,
            QuotaTemplateScope.furnace_type_code == ft.code,
        )
    ) is None:
        region_code = db.scalar(
            select(Region.code).where(Region.name == region, Region.deleted.is_(False))
        )
        db.add(
            QuotaTemplateScope(
                region=region,
                region_code=region_code,
                furnace_type_code=ft.code,
            )
        )
    db.flush()
    for it in items:
        squadless = bool(it.get("squadless", False))
        cap1 = int(it["capacity"])
        cap2 = int(it["capacity2"]) if it.get("capacity2") is not None else cap1
        cap3 = int(it["capacity3"]) if it.get("capacity3") is not None else cap1
        db.add(
            QuotaTemplate(
                region=region,
                furnace_type_code=ft.code,
                post=it["post"],
                squad=None,
                capacity=cap1,
                capacity2=cap2,
                capacity3=cap3,
                squadless=squadless,
                # 可见角色：前端传 visibleRoles 列表；缺省按分班→worker、不分班→chief
                visible_roles=(
                    _roles_to_str(it["visibleRoles"])
                    if it.get("visibleRoles") is not None
                    else _default_visible_roles(squadless)
                ),
            )
        )
    db.add(
        AuditLog(
            actor=actor or "hr",
            action="quota_template_upsert",
            payload=json.dumps(
                {"furnaceType": furnace_type, "region": region, "count": len(items)},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return list_templates(db, furnace_type=furnace_type, region=region)


def overwrite_furnace_quota(
    db: Session,
    *,
    furnace_id: int,
    rules: list[dict],
    visible_posts: list[dict] | None = None,
    actor: str | None = None,
) -> list[dict]:
    """覆盖式重写单个工作点的编制（供「编辑当前工作点编制」弹框用）。

    rules 为展开后的明细：{squad, post, capacity}；squad=None 表示不分班岗位，
    分班岗位由前端按 1/2/3 班（联动或各自）展开后传入，允许三班不同编制。
    visible_posts 为该工作点「可选岗 + 可见角色」名单（工作点级覆盖）：
      [{"post": 岗位, "roles": [role.code, ...]}]；roles 为空的岗位不写入（无人可扫码选）。
    """
    f = db.get(Furnace, furnace_id)
    if f is None:
        raise ValueError("furnace not found")
    old = db.scalars(select(QuotaRule).where(QuotaRule.furnace_id == furnace_id)).all()
    for row in old:
        db.delete(row)
    old_sel = db.scalars(
        select(SelectablePost).where(
            SelectablePost.region == f.region, SelectablePost.furnace_no == f.furnace_no
        )
    ).all()
    for row in old_sel:
        db.delete(row)
    db.flush()
    for r in rules:
        post = (r.get("post") or "").strip()
        if not post:
            continue
        squad = r.get("squad") or None
        db.add(
            QuotaRule(
                furnace_id=furnace_id, squad=squad, post=post, capacity=int(r["capacity"])
            )
        )
    seen_posts: set[str] = set()
    for vp in visible_posts or []:
        post = (vp.get("post") or "").strip()
        roles = _roles_to_str(vp.get("roles"))
        if not post or not roles or post in seen_posts:
            continue
        seen_posts.add(post)
        db.add(
            SelectablePost(
                region=f.region, furnace_no=f.furnace_no, post=post, visible_roles=roles
            )
        )
    db.add(
        AuditLog(
            actor=actor or "hr",
            action="furnace_quota_overwrite",
            payload=json.dumps(
                {"furnaceId": furnace_id, "count": len(rules)}, ensure_ascii=False
            ),
        )
    )
    db.commit()
    saved = db.scalars(select(QuotaRule).where(QuotaRule.furnace_id == furnace_id)).all()
    return [
        {"squad": r.squad, "post": r.post, "capacity": r.capacity, "squadless": r.squad is None}
        for r in saved
    ]


def apply_to_furnaces(
    db: Session,
    *,
    furnace_type: str,
    furnace_nos: list[str] | None = None,
    region: str | None = None,
    actor: str | None = None,
) -> dict:
    """按设备类型模板覆盖式应用到目标工作点（不传 furnace_nos 则应用到该设备类型全部工作点）。"""
    if not region:
        raise ValueError("region is required")
    ft = _ftype_by_name(db, furnace_type)
    if not ft:
        raise ValueError("template not found")
    templates = _template_rows(db, furnace_type_code=ft.code, region=region)
    if not templates:
        raise ValueError("template not found")

    stmt = select(Furnace).where(Furnace.furnace_type_code == ft.code)
    stmt = stmt.where(Furnace.region == region)
    if furnace_nos:
        stmt = stmt.where(Furnace.furnace_no.in_(furnace_nos))
    furnaces = db.scalars(stmt).all()
    allowed_nos = {f.furnace_no for f in furnaces}
    requested_nos = set(furnace_nos or [])
    skipped = sorted(requested_nos - allowed_nos)

    applied: list[str] = []
    for f in furnaces:
        # 覆盖式：先删该工作点旧 QuotaRule + 该工作点级 SelectablePost
        old = db.scalars(select(QuotaRule).where(QuotaRule.furnace_id == f.id)).all()
        for row in old:
            db.delete(row)
        old_sel = db.scalars(
            select(SelectablePost).where(
                SelectablePost.region == f.region, SelectablePost.furnace_no == f.furnace_no
            )
        ).all()
        for row in old_sel:
            db.delete(row)
        db.flush()
        for t in templates:
            if t.squadless:
                db.add(
                    QuotaRule(
                        furnace_id=f.id, squad=None, post=t.post, capacity=t.capacity
                    )
                )
            else:
                caps = {
                    "1班": t.capacity,
                    "2班": t.capacity2 if t.capacity2 is not None else t.capacity,
                    "3班": t.capacity3 if t.capacity3 is not None else t.capacity,
                }
                for squad in SQUADS:
                    db.add(
                        QuotaRule(
                            furnace_id=f.id,
                            squad=squad,
                            post=t.post,
                            capacity=caps[squad],
                        )
                    )
            if t.visible_roles:
                db.add(
                    SelectablePost(
                        region=f.region,
                        furnace_no=f.furnace_no,
                        post=t.post,
                        visible_roles=t.visible_roles,
                    )
                )
        applied.append(f.furnace_no)

    db.add(
        AuditLog(
            actor=actor or "hr",
            action="quota_template_apply",
            payload=json.dumps(
                {"furnaceType": furnace_type, "region": region, "furnaces": applied},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {
        "furnaceType": furnace_type,
        "applied": applied,
        "skipped": skipped,
    }
