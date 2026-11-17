"""Operation audit log: human actions (代签 / 否认 / 排除 / 日结 / 异常处置) for oversight.

Surfaces AuditLog rows to stat/hr with Chinese labels, resolved operator role and
target employee name, so the admin 操作日志 tab is readable without raw payloads.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.models import AuditLog, Employee
from app.session_store import infer_role

SH = ZoneInfo("Asia/Shanghai")


def _sh_date(ts: datetime | None) -> str | None:
    """落库时间戳 → 上海本地日期 YYYY-MM-DD。naive 视为 UTC，aware 按其时区换算，
    避免按上海日期筛选时把当天 00:00-08:00（其 UTC 日期是前一天）的操作误排除。"""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(SH).date().isoformat()

# action code -> human label shown as a tag in the 操作日志 list
ACTION_LABELS: dict[str, str] = {
    "makeup_proxy_signin": "代签到",
    "proxy_limit_change": "改代签上限",
    "proxy_grant": "代签加量",
    "makeup_review": "补录审批",
    "confirm_exclude": "取消打卡",
    "confirm_submit": "日结确认",
    "anomaly_resolve": "异常处置",
    "scan": "扫码打卡",
}

REQUEST_ACTION_LABELS: dict[str, str] = {
    "request_ok": "请求正常",
    "request_error": "请求报错",
    "request_unauthenticated": "未登录",
    "request_forbidden": "权限不足",
    "request_rejected": "其他请求异常",
}

ROLE_LABELS: dict[str, str] = {
    "worker": "操作工",
    "chief": "值班长",
    "hr": "人事专员",
    "stat": "统计员",
    "admin": "系统管理员",
    "repairer": "维修工",
    "leader": "班组长",
}

# system/non-human actions never shown in the operation log
HIDDEN_ACTIONS = {"db:seed"}


def _parse_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {"value": data}
    except (ValueError, TypeError):
        return {"value": raw}


def _detail(action: str, p: dict) -> str:
    """Short human-readable summary of what happened."""
    furnace = p.get("furnace")
    date = p.get("date")
    parts: list[str] = []
    if action.startswith("request_"):
        req = p.get("request") or {}
        status = p.get("status")
        parts.append(
            " ".join(
                str(x)
                for x in (req.get("method"), req.get("path"), status)
                if x not in (None, "")
            )
        )
        if p.get("error"):
            parts.append(f"错误：{p['error']}")
    elif action == "makeup_proxy_signin":
        parts.append(f"代签到 {furnace or ''} {p.get('post') or ''}".strip())
    elif action == "makeup_review":
        sub = {"approve": "通过", "reject": "拒绝"}.get(str(p.get("action")), p.get("action") or "")
        parts.append(f"补录{sub}")
    elif action == "confirm_exclude":
        parts.append("取消/排除打卡")
        if p.get("reason"):
            parts.append(f"原因：{p['reason']}")
    elif action == "confirm_submit":
        parts.append(f"日结确认 {furnace or ''}".strip())
    elif action == "proxy_limit_change":
        parts.append(f"代签默认上限 {p.get('from')} → {p.get('to')}")
    elif action == "proxy_grant":
        parts.append(f"{p.get('month') or ''} 代签加量 +{p.get('extra')}".strip())
        if p.get("reason"):
            parts.append(f"原因：{p['reason']}")
    elif action == "anomaly_resolve":
        parts.append(f"异常处置：{p.get('action') or ''}".strip())
    elif action == "scan":
        parts.append("扫码打卡")
    if date and action not in ("confirm_submit",):
        parts.append(f"日期 {date}")
    return " · ".join([x for x in parts if x])


def list_audit_logs(
    db,
    action: str | None = None,
    limit: int = 200,
    only_labeled: bool = False,
    actor: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    start: str | None = None,
    end: str | None = None,
    exclude_actor_roles: set[str] | None = None,
    include_request_logs: bool = False,
    include_request_detail: bool = False,
) -> dict:
    """exclude_actor_roles：按操作人角色排除（如统计视图不看 hr/admin 的操作）。
    角色取花名册 Employee.role，缺失时按工号前缀推断（与 actorRole 展示同一来源）。"""
    # name/role lookup for operators & target employees
    _emps = db.scalars(select(Employee)).all()
    names = {e.emp_id: e.name for e in _emps}
    # 操作人角色以花名册 Employee.role 为准；值班长/HR 等工号不带角色前缀，按前缀推断会误标成「工人」
    roles = {e.emp_id: e.role for e in _emps if e.role}

    # 关键字筛选（大小写不敏感子串）：操作人/对象/详情；时间按 ts 的上海日期范围
    actor_kw = (actor or "").strip().lower()
    target_kw = (target or "").strip().lower()
    detail_kw = (detail or "").strip().lower()
    start = (start or "").strip() or None
    end = (end or "").strip() or None

    # ---- 能下沉的过滤交给 SQL，减少读放大：隐藏动作、action 白名单(only_labeled)、
    #      指定 action、操作人(工号子串 OR 姓名命中的工号集合)。时间(需按上海时区换算)、
    #      目标/详情子串留在 Python，但只对已被 SQL 缩小的结果集做。----
    request_actions = set(REQUEST_ACTION_LABELS)
    hidden_actions = set(HIDDEN_ACTIONS)
    if not include_request_logs:
        hidden_actions |= request_actions
    stmt = select(AuditLog).where(AuditLog.action.notin_(list(hidden_actions)))
    if only_labeled:
        stmt = stmt.where(AuditLog.action.in_(list(ACTION_LABELS)))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if actor_kw:
        matched_ids = [eid for eid, nm in names.items() if nm and actor_kw in nm.lower()]
        cond = AuditLog.actor.ilike(f"%{actor_kw}%")
        if matched_ids:
            cond = or_(cond, AuditLog.actor.in_(matched_ids))
        stmt = stmt.where(cond)
    stmt = stmt.order_by(AuditLog.ts.desc(), AuditLog.id.desc())
    rows = db.scalars(stmt).all()

    items = []
    for r in rows:
        # only_labeled 下还要求操作人有中文名对照（在册员工）——此条 SQL 不便表达，留在此处
        if only_labeled and not names.get(r.actor):
            continue
        # 时间范围：按上海本地日期比对（ts 落库为 UTC 时刻，按 +08 换算后取日期）
        if start or end:
            d = _sh_date(r.ts)
            if d is None or (start and d < start) or (end and d > end):
                continue
        p = _parse_payload(r.payload)
        is_request_log = r.action in request_actions
        emp_id = p.get("empId")
        emp_name = names.get(emp_id) if emp_id else None
        actor_name = names.get(r.actor)
        actor_role = roles.get(r.actor) or infer_role(r.actor or "")
        # 角色排除（统计仅监督一线操作：统计/值班长/班组长/工人，hr/admin 不可见）
        if exclude_actor_roles and actor_role in exclude_actor_roles:
            continue
        furnace = p.get("furnace")
        detail_text = _detail(r.action, p)
        # 对象：目标员工工号/姓名 或 工作点编号 子串
        if target_kw and target_kw not in " ".join(
            x for x in (emp_id, emp_name, furnace) if x
        ).lower():
            continue
        # 详情：详情文本子串
        if detail_kw and detail_kw not in detail_text.lower():
            continue
        items.append(
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "actor": r.actor,
                "actorName": actor_name,
                "actorRole": actor_role,
                "actorRoleLabel": ROLE_LABELS.get(actor_role, actor_role),
                "action": r.action,
                "actionLabel": ACTION_LABELS.get(
                    r.action, REQUEST_ACTION_LABELS.get(r.action, r.action)
                ),
                "empId": emp_id,
                "empName": emp_name,
                "furnace": furnace,
                "date": p.get("date"),
                "detail": detail_text,
            }
        )
        if is_request_log:
            items[-1]["requestStatus"] = p.get("status")
            items[-1]["requestCategory"] = p.get("category")
        if is_request_log and include_request_detail:
            items[-1]["requestDetail"] = p
        if len(items) >= limit:
            break

    # action facets（动作类型下拉）：独立用 DISTINCT 查询，保持稳定——不随 action/操作人/
    # 时间等当前筛选收窄；only_labeled 时仅保留已配置中文标签的动作。
    facet_hidden = set(HIDDEN_ACTIONS)
    if not include_request_logs:
        facet_hidden |= request_actions
    facet_actions = db.scalars(
        select(AuditLog.action).where(AuditLog.action.notin_(list(facet_hidden))).distinct()
    ).all()
    facets = sorted(
        {a for a in facet_actions if not only_labeled or a in ACTION_LABELS},
        key=lambda a: (
            list(ACTION_LABELS).index(a)
            if a in ACTION_LABELS
            else 50 + list(REQUEST_ACTION_LABELS).index(a)
            if a in REQUEST_ACTION_LABELS
            else 99
        ),
    )
    return {
        "items": items,
        "actions": [
            {"value": a, "label": ACTION_LABELS.get(a, REQUEST_ACTION_LABELS.get(a, a))}
            for a in facets
        ],
    }
