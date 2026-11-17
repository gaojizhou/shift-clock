"""员工差异规则预览、白名单、手动一键与零点自动同步。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.sync_log_service import logged_start, logged_sync, add_counts, task, mark
from app.db import SessionLocal
from app.models import (
    AuditLog,
    DingtalkEmployeeRaw,
    DingtalkEmployeeSyncConfig,
    DingtalkEmployeeSyncRun,
    DingtalkEmployeeSyncWhitelist,
    Employee,
    EmployeeFavorite,
)
from app.session_store import infer_role, invalidate_sessions_for_employee
from app.services import dingtalk_employee_service as directory

logger = logging.getLogger(__name__)

FIELDS = ("region", "role", "post")
PROTECTED_ROLES = {"admin", "hr", "stat"}
VALID_SYNC_ROLES = {"worker", "chief", "leader", "repairer", *PROTECTED_ROLES}
SCOPES = {"local_empty", "different", "mapped", "syncable"}
ARRAY_MODES = {"append", "fill_empty", "replace"}
ROLE_MODES = {"fill_empty", "replace"}
DIFF_TYPES = {"empty_remote", "contains_remote", "missing_remote", "multi"}
STALE_HOURS = 36


def _effective_role(employee: Employee) -> str:
    """与登录链路一致：显式角色优先，否则只按 canonical 工号推断。"""
    return employee.role or infer_role(employee.emp_id)


class RunStartError(RuntimeError):
    """后台线程尚未接管时的启动失败；run_id 精确标识可安全收敛的运行。"""

    def __init__(self, message: str, run_id: int | None = None):
        super().__init__(message)
        self.run_id = run_id


def default_rule(field: str) -> dict:
    return {
        "field": field,
        "enabled": False,
        "scope": "different",
        "updateMode": "append" if field in ("region", "post") else "fill_empty",
        "q": "",
        "localRegions": [],
        "currentRoles": [],
        "targetRoles": [],
        "remoteTitles": [],
        "departments": [],
        "furnaces": [],
        "differenceTypes": ["empty_remote", "missing_remote"],
        "skipMulti": True,
    }


def default_config() -> dict:
    return {
        "enabled": False,
        "rules": {field: default_rule(field) for field in FIELDS},
    }


def normalize_rule(value: dict, *, expected_field: str | None = None) -> dict:
    field = str(value.get("field") or expected_field or "")
    if field not in FIELDS or (expected_field and field != expected_field):
        raise ValueError("field must be region, role or post")
    rule = default_rule(field)
    rule.update({key: value[key] for key in rule if key in value})
    if rule["scope"] not in SCOPES:
        raise ValueError("invalid sync scope")
    allowed_modes = ROLE_MODES if field == "role" else ARRAY_MODES
    if rule["updateMode"] not in allowed_modes:
        raise ValueError("invalid update mode")
    for key in (
        "localRegions", "currentRoles", "targetRoles", "remoteTitles",
        "departments", "furnaces", "differenceTypes",
    ):
        rule[key] = list(dict.fromkeys(str(item).strip() for item in rule.get(key, []) if str(item).strip()))
    if not set(rule["differenceTypes"]).issubset(DIFF_TYPES):
        raise ValueError("invalid difference type")
    if field == "role" and "multi" in rule["differenceTypes"]:
        raise ValueError("role does not support multi difference")
    rule["enabled"] = bool(rule.get("enabled"))
    rule["skipMulti"] = bool(rule.get("skipMulti", True))
    rule["q"] = str(rule.get("q") or "").strip()
    return rule


def _config_row(db: Session) -> DingtalkEmployeeSyncConfig:
    row = db.get(DingtalkEmployeeSyncConfig, 1)
    if not row:
        row = DingtalkEmployeeSyncConfig(
            id=1, enabled=False, rules_json=json.dumps(default_config()["rules"], ensure_ascii=False)
        )
        db.add(row)
        db.commit()
    return row


def get_config(db: Session) -> dict:
    row = _config_row(db)
    defaults = default_config()
    try:
        stored = json.loads(row.rules_json or "{}")
    except json.JSONDecodeError:
        stored = {}
    rules = {
        field: normalize_rule(stored.get(field, {}), expected_field=field)
        for field in FIELDS
    }
    return {
        "enabled": bool(row.enabled),
        "rules": rules,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        "updatedBy": row.updated_by,
    }


def put_config(db: Session, payload: dict, actor: str) -> dict:
    row = _config_row(db)
    rules_payload = payload.get("rules") or {}
    rules = {
        field: normalize_rule(rules_payload.get(field, {}), expected_field=field)
        for field in FIELDS
    }
    row.enabled = bool(payload.get("enabled", False))
    row.rules_json = json.dumps(rules, ensure_ascii=False)
    row.updated_by = actor
    db.add(AuditLog(
        actor=actor,
        action="employee_auto_sync_config",
        payload=json.dumps({"enabled": row.enabled, "rules": rules}, ensure_ascii=False),
    ))
    db.commit()
    return get_config(db)


def whitelist_ids(db: Session) -> set[str]:
    return set(db.scalars(select(DingtalkEmployeeSyncWhitelist.emp_id)).all())


def list_whitelist(db: Session, q: str = "") -> dict:
    stmt = (
        select(DingtalkEmployeeSyncWhitelist, Employee)
        .join(Employee, Employee.emp_id == DingtalkEmployeeSyncWhitelist.emp_id)
        .order_by(DingtalkEmployeeSyncWhitelist.created_at.desc())
    )
    keyword = q.strip()
    if keyword:
        pattern = f"%{keyword}%"
        stmt = stmt.where(or_(Employee.emp_id.ilike(pattern), Employee.name.ilike(pattern)))
    rows = db.execute(stmt).all()
    return {
        "items": [{
            "empId": employee.emp_id,
            "name": employee.name,
            "role": _effective_role(employee),
            "createdAt": entry.created_at.isoformat(),
            "createdBy": entry.created_by,
        } for entry, employee in rows]
    }


def add_whitelist(db: Session, emp_ids: list[str], actor: str) -> dict:
    ids = list(dict.fromkeys(item.strip() for item in emp_ids if item.strip()))
    existing_employees = set(db.scalars(
        select(Employee.emp_id).where(Employee.emp_id.in_(ids), Employee.deleted.is_(False))
    ).all())
    existing = whitelist_ids(db)
    for emp_id in ids:
        if emp_id in existing_employees and emp_id not in existing:
            db.add(DingtalkEmployeeSyncWhitelist(emp_id=emp_id, created_by=actor))
            db.add(AuditLog(actor=actor, action="employee_sync_whitelist_add", payload=emp_id))
    db.commit()
    return list_whitelist(db)


def remove_whitelist(db: Session, emp_id: str, actor: str) -> dict:
    row = db.get(DingtalkEmployeeSyncWhitelist, emp_id)
    if row:
        db.delete(row)
        db.add(AuditLog(actor=actor, action="employee_sync_whitelist_remove", payload=emp_id))
        db.commit()
    return list_whitelist(db)


def _favorite_map(db: Session, emp_ids: list[str]) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    if not emp_ids:
        return result
    rows = db.scalars(select(EmployeeFavorite).where(
        EmployeeFavorite.emp_id.in_(emp_ids),
        EmployeeFavorite.kind.in_(["region", "post"]),
    )).all()
    for row in rows:
        result.setdefault((row.emp_id, row.kind), []).append(row.value)
    for values in result.values():
        values[:] = sorted(set(values))
    return result


def _latest_raws(db: Session) -> tuple[dict[str, DingtalkEmployeeRaw], set[str]]:
    """返回 (active 员工最新原始记录, 工号冲突的员工集合)。

    只取 active=True（与 dingtalk_employee_service 差异列表一致，排除 NULL/未激活）；
    同一 job_number 被多个 userid 占用时无法确定归属，标记为冲突由预览页跳过。
    """
    result: dict[str, DingtalkEmployeeRaw] = {}
    rows = db.scalars(
        select(DingtalkEmployeeRaw)
        .where(DingtalkEmployeeRaw.active.is_(True))
        .order_by(DingtalkEmployeeRaw.synced_at.desc(), DingtalkEmployeeRaw.id.desc())
    ).all()
    job_userids: dict[str, set[str]] = {}
    for raw in rows:
        emp_id = directory._raw_emp_id(raw)
        if emp_id and emp_id not in result:
            result[emp_id] = raw
        if emp_id:
            job_userids.setdefault(emp_id, set()).add((raw.userid or "").strip())
    ambiguous = {job for job, userids in job_userids.items() if len(userids) > 1}
    return result, ambiguous


def _is_fresh(value: datetime | None) -> bool:
    if not value:
        return False
    now = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value >= now - timedelta(hours=STALE_HOURS)


def _matches(values: list[str], selected: list[str]) -> bool:
    return not selected or bool(set(values) & set(selected))


def _matches_rule_scope(
    employee: Employee,
    raw: DingtalkEmployeeRaw,
    rule: dict,
    targets: dict[str, str | None],
    local_regions: list[str],
    current_role: str,
) -> bool:
    """规则的关键字/地区/角色/职位/部门/工作点筛选是否命中当前状态。

    预览快照可能过期（同批前序规则会改写角色/地区收藏），因此应用写入前
    必须用员工当前状态重跑一次该筛选，而不仅依赖快照里的匹配结果。
    """
    text = " ".join([
        employee.emp_id, employee.name or "", raw.name or "", raw.title or "",
        raw.dept_path or "", raw.root_label or "",
    ]).lower()
    if rule["q"] and rule["q"].lower() not in text:
        return False
    if not _matches(local_regions, rule["localRegions"]):
        return False
    if rule["currentRoles"] and current_role not in rule["currentRoles"]:
        return False
    if rule["targetRoles"] and (targets.get("role") or "") not in rule["targetRoles"]:
        return False
    raw_title = (raw.title or "").strip()
    if rule["remoteTitles"] and raw_title not in rule["remoteTitles"]:
        return False
    dept = (raw.dept_path or "").strip()
    if rule["departments"] and not any(value in dept for value in rule["departments"]):
        return False
    if rule["furnaces"] and not any(value in dept for value in rule["furnaces"]):
        return False
    return True


def _preview_item(
    employee: Employee,
    raw: DingtalkEmployeeRaw,
    rule: dict,
    targets: dict[str, str | None],
    favorites: dict[tuple[str, str], list[str]],
    whitelist: set[str],
    allowed_emp_ids: set[str] | None,
    ambiguous_emp_ids: set[str],
) -> dict | None:
    field = rule["field"]
    current_role = _effective_role(employee)
    current: str | list[str]
    if field == "role":
        current = current_role
        # 判空看持久化字段，推断角色只用于权限/值班长保护。这样 fill_empty 能把
        # 远程映射显式落库，同时不会因 infer_role 的 worker 兜底而永远失效。
        is_empty = employee.role is None
    else:
        current = favorites.get((employee.emp_id, field), [])
        is_empty = len(current) == 0
    target = targets.get(field)
    target_role = targets.get("role")
    local_regions = favorites.get((employee.emp_id, "region"), [])
    if not _matches_rule_scope(
        employee, raw, rule, targets, local_regions, current_role
    ):
        return None

    contains = bool(target) and (
        current == target if field == "role" else target in current
    )
    types: list[str] = []
    if is_empty and target:
        types.append("empty_remote")
    if not is_empty and contains:
        types.append("contains_remote")
    if not is_empty and target and not contains:
        types.append("missing_remote")
    if field != "role" and len(current) > 1:
        types.append("multi")
    different = bool(target) and (
        (is_empty or current != target)
        if field == "role"
        else (
            current != [target]
            if rule["updateMode"] == "replace"
            else not contains
        )
    )

    scope = rule["scope"]
    protected = current_role in PROTECTED_ROLES or target_role in PROTECTED_ROLES
    chief_demotion = field == "role" and current_role == "chief" and target_role != "chief"
    stale = not _is_fresh(raw.synced_at)
    inactive = raw.active is False
    whitelisted = employee.emp_id in whitelist
    skip_reason = None
    if whitelisted:
        skip_reason = "同步白名单"
    elif protected:
        skip_reason = "当前或目标为 admin/HR/统计"
    elif chief_demotion:
        skip_reason = "规则同步不执行值班长降级，请走员工管理页手工调整"
    elif employee.emp_id in ambiguous_emp_ids:
        skip_reason = "远程存在多个同工号账号"
    elif not target:
        skip_reason = "远程未配置映射"
    elif field == "role" and target not in VALID_SYNC_ROLES:
        skip_reason = "远程角色映射非法"
    elif inactive:
        skip_reason = "远程账号未激活"
    elif stale:
        skip_reason = "远程数据超过36小时未刷新"
    elif allowed_emp_ids is not None and employee.emp_id not in allowed_emp_ids:
        skip_reason = "本次通讯录未成功刷新"
    elif rule["skipMulti"] and field != "role" and len(current) > 1:
        skip_reason = "本地存在多个地区或职位"

    if scope == "local_empty" and not (is_empty and target):
        return None
    if scope == "different" and not different:
        return None
    if scope == "mapped" and not target:
        return None
    if scope == "syncable" and (not target or skip_reason):
        return None
    if rule["differenceTypes"] and not set(types) & set(rule["differenceTypes"]):
        return None

    action = "skip" if skip_reason else "unchanged"
    will_add: list[str] = []
    will_remove: list[str] = []
    mode = rule["updateMode"]
    if not skip_reason and target:
        if mode == "fill_empty":
            if is_empty:
                action = "fill"
                will_add = [target]
            elif contains:
                action = "unchanged"
            else:
                skip_reason = "本地已有值，仅填空模式不修改"
                action = "skip"
        elif mode == "append":
            if contains:
                action = "unchanged"
            else:
                action = "append"
                will_add = [target]
        elif mode == "replace":
            if contains and (field == "role" or current == [target]):
                action = "unchanged"
            else:
                action = "replace"
                will_add = [target]
                if isinstance(current, list):
                    will_remove = [value for value in current if value != target]
                elif current and current != target:
                    will_remove = [str(current)]
    return {
        "empId": employee.emp_id,
        "name": employee.name,
        "userid": raw.userid,
        "remoteTitle": raw.title,
        "deptPath": raw.dept_path,
        "rootLabel": raw.root_label,
        "currentRole": current_role,
        "targetRole": target_role,
        "localRegions": local_regions,
        "field": field,
        "current": current,
        "remote": target,
        "different": different,
        "differenceTypes": types,
        "action": action,
        "willAdd": will_add,
        "willRemove": will_remove,
        "skipReason": skip_reason,
        "whitelisted": whitelisted,
        "remoteFresh": not stale,
        "remoteActive": not inactive,
        "remoteSyncedAt": raw.synced_at.isoformat() if raw.synced_at else None,
    }


def preview(db: Session, rule_payload: dict, *, page: int = 1, page_size: int = 100,
            allowed_emp_ids: set[str] | None = None) -> dict:
    rule = normalize_rule(rule_payload)
    employees = {
        row.emp_id: row for row in db.scalars(
            select(Employee).where(Employee.deleted.is_(False))
        ).all()
    }
    favorites = _favorite_map(db, list(employees))
    raws, ambiguous_emp_ids = _latest_raws(db)
    title_map = directory.load_title_map(db)
    root_regions = directory.load_root_region_names(db)
    whitelist = whitelist_ids(db)
    items: list[dict] = []
    option_sets = {
        "localRegions": set(), "currentRoles": set(), "targetRoles": set(),
        "remoteTitles": set(), "departments": set(), "furnaces": set(),
    }
    for emp_id, employee in employees.items():
        raw = raws.get(emp_id)
        if not raw:
            continue
        targets = directory._remote_targets(raw, title_map, root_regions)
        current_role = _effective_role(employee)
        option_sets["localRegions"].update(favorites.get((emp_id, "region"), []))
        option_sets["currentRoles"].add(current_role)
        if targets.get("role"):
            option_sets["targetRoles"].add(targets["role"])
        raw_title = (raw.title or "").strip()
        dept_path = (raw.dept_path or "").strip()
        if raw_title:
            option_sets["remoteTitles"].add(raw_title)
        if dept_path:
            option_sets["departments"].add(dept_path)
            option_sets["furnaces"].update(
                part.strip() for part in dept_path.split("/") if "工作点" in part.strip()
            )
        item = _preview_item(
            employee, raw, rule, targets, favorites, whitelist, allowed_emp_ids,
            ambiguous_emp_ids,
        )
        if item:
            items.append(item)
    skip_reasons: dict[str, int] = {}
    for item in items:
        if item["skipReason"]:
            skip_reasons[item["skipReason"]] = skip_reasons.get(item["skipReason"], 0) + 1
    counts = {
        "matched": len(items),
        "willUpdate": sum(item["action"] in ("fill", "append", "replace") for item in items),
        "unchanged": sum(item["action"] == "unchanged" for item in items),
        "skipped": sum(item["action"] == "skip" for item in items),
        "skipReasons": skip_reasons,
    }
    size = max(1, min(page_size, 100_000))
    start = (max(1, page) - 1) * size
    return {
        **counts,
        "page": max(1, page),
        "pageSize": size,
        "items": items[start:start + size],
        "options": {key: sorted(values) for key, values in option_sets.items()},
        "rule": rule,
    }


def apply_rule(
    db: Session,
    rule_payload: dict,
    actor: str,
    source: str,
    allowed_emp_ids: set[str] | None = None,
    precomputed: dict | None = None,
) -> dict:
    result = precomputed or preview(
        db, rule_payload, page=1, page_size=500_000, allowed_emp_ids=allowed_emp_ids
    )
    # 规则驱动的全量写入需与手动应用（dingtalk_employee_service 单次最多 500）对齐，
    # 超限直接失败并写入 run.message，避免零点无人值守时批量改动海量数据。
    actionable = [item for item in result["items"] if item["action"] in ("fill", "append", "replace")]
    if len(actionable) > 500:
        raise ValueError("命中条数超过单次上限 500，请收紧规则后再同步")
    rule = result["rule"]
    title_map = directory.load_title_map(db)
    root_regions = directory.load_root_region_names(db)
    raws, _ = _latest_raws(db)
    employees = {
        row.emp_id: row for row in db.scalars(
            select(Employee).where(
                Employee.emp_id.in_([item["empId"] for item in result["items"]]),
                Employee.deleted.is_(False),
            )
        ).all()
    }
    protected_ids = whitelist_ids(db)
    changed = 0
    warnings: list[dict] = []
    runtime_skipped: list[dict] = []
    for item in result["items"]:
        if item["action"] not in ("fill", "append", "replace"):
            continue
        employee = employees.get(item["empId"])
        if employee is None:
            runtime_skipped.append({
                "empId": item["empId"],
                "reason": "员工已删除或远程记录已失效",
            })
            continue
        if employee.emp_id in protected_ids:
            runtime_skipped.append({
                "empId": employee.emp_id,
                "reason": "同步白名单",
            })
            continue
        target = str(item["remote"])
        field = rule["field"]
        mode = rule["updateMode"]
        current_role = _effective_role(employee)
        if current_role in PROTECTED_ROLES or (
            field == "role" and target in PROTECTED_ROLES
        ):
            runtime_skipped.append({
                "empId": employee.emp_id,
                "reason": "当前或目标为 admin/HR/统计",
            })
            continue
        # 写入前复核规则筛选：预检快照生成后，同批前序规则可能已改写本员工的
        # 角色/地区等筛选依据，必须以当前状态重判，避免向已不匹配的员工写入。
        raw = raws.get(employee.emp_id)
        if raw is None:
            runtime_skipped.append({
                "empId": employee.emp_id,
                "reason": "员工已删除或远程记录已失效",
            })
            continue
        local_regions = sorted(set(db.scalars(select(EmployeeFavorite.value).where(
            EmployeeFavorite.emp_id == employee.emp_id,
            EmployeeFavorite.kind == "region",
        )).all()))
        if not _matches_rule_scope(
            employee,
            raw,
            rule,
            directory._remote_targets(raw, title_map, root_regions),
            local_regions,
            current_role,
        ):
            runtime_skipped.append({
                "empId": employee.emp_id,
                "reason": "应用前复核不再满足规则筛选条件（员工状态已被同批前序规则变更）",
            })
            continue
        if field in ("region", "post"):
            existing = db.scalars(select(EmployeeFavorite).where(
                EmployeeFavorite.emp_id == employee.emp_id,
                EmployeeFavorite.kind == field,
            )).all()
            old = [row.value for row in existing]
            if mode == "fill_empty" and existing:
                runtime_skipped.append({
                    "empId": employee.emp_id,
                    "reason": "本地已有值，仅填空模式不修改",
                })
                continue
            if item["action"] == "replace":
                for row in existing:
                    if row.value != target:
                        db.delete(row)
            if not any(row.value == target for row in existing):
                db.add(EmployeeFavorite(emp_id=employee.emp_id, kind=field, value=target))
        else:
            old_role = current_role
            old = old_role
            if target not in VALID_SYNC_ROLES:
                runtime_skipped.append({"empId": employee.emp_id, "reason": "远程角色映射非法"})
                continue
            if mode == "fill_empty" and employee.role is not None:
                runtime_skipped.append({
                    "empId": employee.emp_id,
                    "reason": "本地已有值，仅填空模式不修改",
                })
                continue
            if old_role == "chief" and target != "chief":
                runtime_skipped.append({
                    "empId": employee.emp_id,
                    "reason": "规则同步不执行值班长降级，请走单人二次确认",
                })
                continue
            if target == "chief" and old_role != "chief":
                if not directory._bind_chief_by_dept(
                    db, employee.emp_id, raw, actor,
                    root_regions.get((raw.root_label or "").strip()),
                ):
                    runtime_skipped.append({
                        "empId": employee.emp_id,
                        "reason": "角色同步为值班长但未匹配到工作点，已跳过",
                    })
                    continue
            side_effects: dict = {}
            if old_role == "chief" and target != "chief":
                side_effects = directory._unbind_chief(db, employee.emp_id, actor)
            employee.role = target
            invalidate_sessions_for_employee(db, employee.emp_id, employee.dd_user_id)
        employee.updated_by = actor
        db.add(AuditLog(
            actor=actor,
            action="employee_directory_field_sync",
            payload=json.dumps({
                "empId": employee.emp_id, "field": field, "old": old, "new": target,
                "updateMode": rule["updateMode"], "source": source,
                "sideEffects": side_effects if field == "role" else {},
            }, ensure_ascii=False),
        ))
        changed += 1
    db.commit()
    add_counts(employeeChanged=changed)
    merged_skip_reasons = dict(result["skipReasons"])
    for item in runtime_skipped:
        reason = item["reason"]
        merged_skip_reasons[reason] = merged_skip_reasons.get(reason, 0) + 1
    return {
        "field": rule["field"],
        "matched": result["matched"],
        "changed": changed,
        "unchanged": result["unchanged"],
        "skipped": result["skipped"] + len(runtime_skipped),
        "skipReasons": merged_skip_reasons,
        "skippedItems": runtime_skipped,
        "warnings": warnings,
    }


def _update_run(db: Session, run: DingtalkEmployeeSyncRun, **values) -> None:
    for key, value in values.items():
        setattr(run, key, value)
    db.commit()


def _mark_run_error_fresh_session(run_id: int, message: str) -> None:
    """用全新会话把仍为 running 的 run 收敛为 error。

    执行线程的兜底落库与原会话复用：数据库连接中断时原会话已不可用，
    兜底会二次抛异常逃出线程，留下永久 running 空洞；独立新会话才有机会写成功。
    """
    db = SessionLocal()
    try:
        run = db.get(DingtalkEmployeeSyncRun, run_id)
        if run is not None and run.status == "running":
            run.status = "error"
            run.phase = run.phase or "失败"
            run.message = message
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def reconcile_stale_running_runs() -> int:
    """启动时把上一进程遗留的 running 记录归一化为 error。

    容器重启/OOM kill 时线程内状态来不及落库，running 行会永久停留且无审计；
    只收敛 started_at 早于本进程启动的行，避免误杀其它 worker 正在执行的任务。
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        rows = db.scalars(
            select(DingtalkEmployeeSyncRun).where(
                DingtalkEmployeeSyncRun.status == "running",
                or_(
                    DingtalkEmployeeSyncRun.started_at.is_(None),
                    DingtalkEmployeeSyncRun.started_at < _PROCESS_STARTED_AT,
                ),
            )
        ).all()
        if not rows:
            return 0
        for run in rows:
            run.status = "error"
            run.phase = run.phase or "失败"
            run.message = "进程重启，同步中断；请重新发起同步"
            run.finished_at = now
        db.commit()
        return len(rows)
    finally:
        db.close()


@logged_sync("employee_difference", employee_run=True)
def _execute_run(run_id: int, rules: list[dict]) -> None:
    try:
        db = SessionLocal()
        pulling_directory = False
        try:
            run = db.get(DingtalkEmployeeSyncRun, run_id)
            _update_run(db, run, phase="拉取最新通讯录", message="正在拉取最新通讯录")
            pulling_directory = True
            pulled = directory.run_directory_sync_locked(db)
            pulling_directory = False
            allowed = set(pulled["refreshedEmpIds"])
            enabled_rules = [normalize_rule(rule) for rule in rules]
            enabled_rules = [rule for rule in enabled_rules if rule["enabled"]]

            # 所有规则先统一预检上限，确保不会出现前一条已提交、后一条才发现超限。
            _update_run(db, run, phase="预检同步规则", message="正在校验全部同步规则")
            precomputed: list[dict] = []
            for normalized in enabled_rules:
                check = preview(
                    db,
                    normalized,
                    page=1,
                    page_size=500_000,
                    allowed_emp_ids=allowed,
                )
                if check["willUpdate"] > 500:
                    raise ValueError(
                        f"{normalized['field']} 命中条数超过单次上限 500，请收紧规则后再同步"
                    )
                precomputed.append(check)

            results = []
            for normalized, check in zip(enabled_rules, precomputed, strict=True):
                _update_run(db, run, phase=f"同步{normalized['field']}", message=f"正在同步 {normalized['field']}")
                try:
                    results.append(
                        apply_rule(
                            db,
                            normalized,
                            run.actor,
                            run.trigger,
                            allowed,
                            precomputed=check,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    failed = {"field": normalized["field"], "error": str(exc)}
                    summary = {"pull": pulled, "fields": results, "failedFields": [failed]}
                    completed = "、".join(item["field"] for item in results) or "无"
                    _update_run(
                        db,
                        run,
                        status="error",
                        phase=f"同步{normalized['field']}失败",
                        result_json=json.dumps(summary, ensure_ascii=False),
                        message=f"已完成字段：{completed}；{normalized['field']} 失败：{exc}",
                        finished_at=datetime.now(timezone.utc),
                    )
                    db.add(AuditLog(
                        actor=run.actor,
                        action="employee_difference_sync_run",
                        payload=json.dumps({
                            "runId": run.id,
                            "trigger": run.trigger,
                            "status": "partial" if results else "error",
                            "fields": results,
                            "failedFields": [failed],
                        }, ensure_ascii=False),
                    ))
                    db.commit()
                    return
            summary = {"pull": pulled, "fields": results}
            _update_run(
                db, run, status="done", phase="完成",
                result_json=json.dumps(summary, ensure_ascii=False),
                message="同步完成", finished_at=datetime.now(timezone.utc),
            )
            db.add(AuditLog(
                actor=run.actor, action="employee_difference_sync_run",
                payload=json.dumps({"runId": run.id, "trigger": run.trigger, "fields": results}, ensure_ascii=False),
            ))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            # 共用 roster 状态只描述通讯录拉取。拉取成功后的规则应用失败，
            # 只写本次 run，保留通讯录成功明细与失败部门信息。
            try:
                db.rollback()
                if pulling_directory:
                    directory.mark_directory_sync_error(db, exc)
                run = db.get(DingtalkEmployeeSyncRun, run_id)
                _update_run(
                    db, run, status="error", phase="失败", message=str(exc),
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:  # noqa: BLE001 - 原会话可能已随连接中断失效，换新会话兜底
                logger.exception("员工同步失败状态落库失败，改用新会话兜底")
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _mark_run_error_fresh_session(run_id, str(exc))
                except Exception:  # noqa: BLE001
                    logger.exception("新会话兜底写员工同步失败状态仍失败")
        finally:
            # 最终防线：任何路径离开时 run 仍为 running（兜底自身再次失败、解释器退出等），
            # 用新会话强制收敛，不让状态页与审计出现永久空洞。
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                _mark_run_error_fresh_session(run_id, "同步异常中止，未留下明确状态；请重新发起同步")
            except Exception:  # noqa: BLE001
                logger.exception("收敛员工同步 running 状态失败")
    finally:
        # 外层 finally 保证即使 SessionLocal()/db.get() 抛异常也释放锁，避免永久泄漏。
        directory.release_directory_sync()


@logged_start("employee_difference")
def start_run(db: Session, rules: list[dict], actor: str, trigger: str) -> dict:
    normalized = [normalize_rule(rule) for rule in rules]
    if not directory.try_acquire_directory_sync():
        raise RuntimeError("通讯录拉取或员工同步任务正在执行")
    handed_off = False
    run: DingtalkEmployeeSyncRun | None = None
    try:
        run = DingtalkEmployeeSyncRun(
            trigger=trigger, actor=actor, status="running", phase="等待执行",
            rule_json=json.dumps(normalized, ensure_ascii=False),
        )
        db.add(run)
        db.commit()
        threading.Thread(target=_execute_run, args=(run.id, normalized), daemon=True).start()
        handed_off = True
    except Exception as exc:
        # 获取锁之后、后台线程成功启动之前任一失败都要释放锁，避免永久泄漏。
        if not handed_off:
            directory.release_directory_sync()
        raise RunStartError(str(exc), run.id if run is not None else None) from exc
    # 线程启动后锁已移交给 _execute_run 的 finally；直接返回已提交对象，避免一次
    # 非必要的状态查询失败后把真正在跑的任务误记成“启动失败”。
    assert run is not None
    return _run_payload(run)


def _run_payload(run: DingtalkEmployeeSyncRun) -> dict:
    try:
        result = json.loads(run.result_json) if run.result_json else None
    except json.JSONDecodeError:
        result = None
    return {
        "id": run.id, "trigger": run.trigger, "actor": run.actor,
        "status": run.status, "phase": run.phase, "message": run.message,
        "result": result,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
    }


def run_status(db: Session, run_id: int | None = None) -> dict:
    run = db.get(DingtalkEmployeeSyncRun, run_id) if run_id else db.scalar(
        select(DingtalkEmployeeSyncRun).order_by(DingtalkEmployeeSyncRun.id.desc())
    )
    if not run:
        return {"status": "idle"}
    return _run_payload(run)


def _record_automatic_failure(
    db: Session, exc: Exception, rules: list[dict] | None = None
) -> None:
    """尽力把零点任务在 start_run 之外的失败也落到状态页和审计日志。"""
    reason = f"{type(exc).__name__}: {exc}"
    try:
        db.rollback()
        # 只有 RunStartError 携带的明确 run_id 才能收敛；绝不反查“最近 running”，
        # 避免把已经移交后台线程、正在改数据的任务误标成启动失败。
        run = (
            db.get(DingtalkEmployeeSyncRun, exc.run_id)
            if isinstance(exc, RunStartError) and exc.run_id is not None
            else None
        )
        if run is None:
            run = DingtalkEmployeeSyncRun(
                trigger="auto",
                actor="scheduler",
                status="error",
                phase="启动失败",
                rule_json=json.dumps(rules or [], ensure_ascii=False),
            )
            db.add(run)
        run.status = "error"
        run.phase = "启动失败"
        run.message = reason
        run.finished_at = datetime.now(timezone.utc)
        db.flush()
        db.add(AuditLog(
            actor="scheduler",
            action="employee_difference_sync_run",
            payload=json.dumps(
                {
                    "runId": run.id,
                    "trigger": "auto",
                    "status": "error",
                    "ok": False,
                    "reason": str(exc),
                    "errorType": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
        ))
        db.commit()
    except Exception:  # noqa: BLE001 - 数据库本身不可用时不能让记录失败掩盖原异常
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def automatic_sync_job() -> dict:
    db: Session | None = None
    rules: list[dict] = []
    try:
        db = SessionLocal()
        config = get_config(db)
        if not config["enabled"]:
            with task(db, "employee_difference", "scheduler", "auto"):
                mark("skipped", "自动同步开关已关闭")
            return {"started": False, "reason": "disabled"}
        rules = [config["rules"][field] for field in FIELDS if config["rules"][field]["enabled"]]
        if not rules:
            with task(db, "employee_difference", "scheduler", "auto"):
                mark("skipped", "没有启用的同步规则")
            return {"started": False, "reason": "no enabled rules"}
        return start_run(db, rules, actor="scheduler", trigger="auto")
    except Exception as exc:  # noqa: BLE001 - 调度入口必须留下可见失败状态
        if db is not None:
            _record_automatic_failure(db, exc, rules)
            with task(db, "employee_difference", "scheduler", "auto"):
                mark("failed", str(exc))
        return {
            "started": False,
            "reason": str(exc),
            "errorType": type(exc).__name__,
        }
    finally:
        if db is not None:
            db.close()
