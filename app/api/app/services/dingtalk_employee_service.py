"""钉钉通讯录 → employee 花名册同步。

两段式（仿 dingtalk_attendance_service）：
1) 后台同步原始数据到新表：部门树 dingtalk_department_raw（listsub BFS）+ 员工 dingtalk_employee_raw（user/list）。
2) transform：原始员工 → employee 花名册（按 emp_id upsert，保留 HR 维护的 role）。

范围由 ROSTER_ROOT_DEPTS（命名字典，集中可改）控制；同步前校验根部门是否失效并明确报错。
部门树/成员接口受 90002 平台级 QPS 限流 → 节流 + 退避重试；耗时长 → 后台线程跑，进度写 dingtalk_sync_state 供轮询。
凭据复用 settings.dingtalk_app_key/secret（部署时由用户提供的两个 key 设入）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.services.sync_log_service import logged_start, logged_sync, add_counts, mark
from app.db import SessionLocal
from app.models import (
    AuditLog,
    ChiefWorker,
    DingtalkDepartmentRaw,
    DingtalkEmployeeRaw,
    DingtalkSyncState,
    Employee,
    EmployeeFavorite,
    Furnace,
    FurnaceChief,
    Region,
    RosterRootDept,
    RosterTitleMap,
)
from app.session_store import infer_role, invalidate_sessions_for_employee

logger = logging.getLogger(__name__)

# ── 同步根部门默认值（仅当 roster_root_dept 表为空时回退） ──
# 开源版不内置任何真实钉钉部门 ID；由 HR 在「通讯录同步」页配置。
DEFAULT_ROSTER_ROOT_CONFIGS: tuple[dict, ...] = ()
DEFAULT_ROSTER_ROOT_DEPTS: dict[str, int] = {
    item["label"]: item["deptId"] for item in DEFAULT_ROSTER_ROOT_CONFIGS
}


def load_roots(db: Session) -> dict[str, int]:
    """读取同步范围（label→dept_id），按 sort,id 保序；表为空回退默认值。"""
    rows = db.scalars(
        select(RosterRootDept).order_by(RosterRootDept.sort, RosterRootDept.id)
    ).all()
    if not rows:
        return dict(DEFAULT_ROSTER_ROOT_DEPTS)
    return {r.label: r.dept_id for r in rows}


def list_roots(db: Session) -> dict:
    """API：列出同步范围（含 sort，供编辑页展示）。"""
    rows = db.scalars(
        select(RosterRootDept).order_by(RosterRootDept.sort, RosterRootDept.id)
    ).all()
    if not rows:
        region_names = {
            region.code: region.name
            for region in db.scalars(select(Region).where(Region.deleted.is_(False))).all()
        }
        return {
            "items": [
                {
                    **item,
                    "regionName": region_names.get(item["regionCode"]),
                    "sort": i,
                }
                for i, item in enumerate(DEFAULT_ROSTER_ROOT_CONFIGS)
            ],
            "isDefault": True,
        }
    region_names = {
        region.code: region.name
        for region in db.scalars(select(Region).where(Region.deleted.is_(False))).all()
    }
    return {
        "items": [
            {
                "label": r.label,
                "deptId": r.dept_id,
                "regionCode": r.region_code,
                "regionName": region_names.get(r.region_code),
                "sort": r.sort,
            }
            for r in rows
        ],
        "isDefault": False,
    }


def load_root_region_names(db: Session) -> dict[str, str]:
    """返回根部门描述 → 基础地区名称，供花名册常用地区与值班长绑定使用。"""
    rows = db.scalars(
        select(RosterRootDept).order_by(RosterRootDept.sort, RosterRootDept.id)
    ).all()
    configs = (
        [(row.label, row.region_code) for row in rows]
        if rows
        else [(item["label"], item["regionCode"]) for item in DEFAULT_ROSTER_ROOT_CONFIGS]
    )
    codes = {code for _, code in configs if code}
    names = {
        region.code: region.name
        for region in db.scalars(
            select(Region).where(Region.code.in_(codes), Region.deleted.is_(False))
        ).all()
    } if codes else {}
    return {label: names[code] for label, code in configs if code in names}


def save_roots(db: Session, items: list[dict]) -> dict:
    """覆盖式保存同步范围；每个根部门必须选择一个有效的基础地区。"""
    cleaned: list[tuple[str, int, str]] = []
    seen_labels: set[str] = set()
    seen_dept_ids: set[int] = set()
    valid_region_codes = set(
        db.scalars(select(Region.code).where(Region.deleted.is_(False))).all()
    )
    for it in items or []:
        label = str(it.get("label") or "").strip()
        region_code = str(it.get("regionCode") or "").strip()
        try:
            dept_id = int(it.get("deptId"))
        except (TypeError, ValueError):
            raise ValueError(f"部门 ID 非法：{it.get('deptId')!r}（描述「{label}」）")
        if not label:
            raise ValueError("描述不能为空")
        if dept_id <= 0:
            raise ValueError(f"部门 ID 必须为正整数（描述「{label}」）")
        if label in seen_labels:
            raise ValueError(f"描述重复：「{label}」")
        if dept_id in seen_dept_ids:
            raise ValueError(f"钉钉部门 ID 重复：{dept_id}")
        if not region_code:
            raise ValueError(f"请选择地区（描述「{label}」）")
        if region_code not in valid_region_codes:
            raise ValueError(f"地区不存在或已停用：{region_code}（描述「{label}」）")
        seen_labels.add(label)
        seen_dept_ids.add(dept_id)
        cleaned.append((label, dept_id, region_code))
    if not cleaned:
        raise ValueError("同步范围至少需要一个根部门")
    db.query(RosterRootDept).delete()
    db.flush()
    for i, (label, dept_id, region_code) in enumerate(cleaned):
        db.add(
            RosterRootDept(
                label=label, dept_id=dept_id, region_code=region_code, sort=i
            )
        )
    db.commit()
    return list_roots(db)


# ── 职位映射：钉钉职位 → 角色 / 常用职位 ───────────────────────────────────
def load_title_map(db: Session) -> dict[str, dict]:
    """{title: {"role": role|None, "post": post|None}}（转花名册用）。"""
    rows = db.scalars(select(RosterTitleMap)).all()
    return {r.title: {"role": r.role, "post": r.post} for r in rows}


def list_title_map(db: Session) -> dict:
    """API：列出职位映射（按 sort,id）。"""
    rows = db.scalars(
        select(RosterTitleMap).order_by(RosterTitleMap.sort, RosterTitleMap.id)
    ).all()
    return {
        "items": [
            {"title": r.title, "role": r.role or "", "post": r.post or "", "sort": r.sort}
            for r in rows
        ]
    }


def save_title_map(db: Session, items: list[dict]) -> dict:
    """覆盖式保存职位映射（title 非空唯一；role/post 可空）。"""
    cleaned: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for it in items or []:
        title = str(it.get("title") or "").strip()
        role = (str(it.get("role") or "").strip()) or None
        post = (str(it.get("post") or "").strip()) or None
        if not title:
            raise ValueError("钉钉职位不能为空")
        if role and role not in SYNC_ROLE_VALUES:
            raise ValueError(f"非法角色映射：「{role}」")
        if title in seen:
            raise ValueError(f"职位重复：「{title}」")
        seen.add(title)
        cleaned.append((title, role, post))
    db.query(RosterTitleMap).delete()
    db.flush()
    for i, (title, role, post) in enumerate(cleaned):
        db.add(RosterTitleMap(title=title, role=role, post=post, sort=i))
    db.commit()
    return list_title_map(db)


def list_unmapped_titles(db: Session) -> dict:
    """已同步原始员工里、尚未出现在映射表中的去重职位（含人数），供页面一键导入。"""
    mapped = {r.title for r in db.scalars(select(RosterTitleMap)).all()}
    rows = db.execute(
        select(DingtalkEmployeeRaw.title, func.count())
        .where(DingtalkEmployeeRaw.title.is_not(None))
        .group_by(DingtalkEmployeeRaw.title)
    ).all()
    items = [
        {"title": t, "count": n}
        for t, n in rows
        if t and t.strip() and t not in mapped
    ]
    items.sort(key=lambda x: x["count"], reverse=True)
    return {"items": items}


# ── 值班长按部门自动绑定工作点 ─────────────────────────────────────────────────
def _region_from_root_label(label: str | None) -> str | None:
    """root_label（如「示例厂区A生产部」）→ furnace.region（「示例厂区A」）。"""
    if not label:
        return None
    if "示例厂区A" in label:
        return "示例厂区A"
    if "示例厂区B" in label:
        return "示例厂区B"
    return None


def _bind_chief_by_dept(
    db: Session,
    emp_id: str,
    raw: DingtalkEmployeeRaw,
    actor: str,
    mapped_region: str | None = None,
) -> bool:
    """按员工钉钉部门把其绑定为工作点-值班长：地区优先取根部门映射，工作点编号取路径末级。
    成功绑定返回 True；无法定位工作点返回 False。"""
    region = mapped_region or _region_from_root_label(raw.root_label)
    leaf = (raw.dept_path or "").rstrip("/").split("/")[-1].strip()
    if not region or not leaf:
        return False
    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == leaf)
    )
    if not f:
        return False
    exists = db.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == f.id, FurnaceChief.emp_id == emp_id
        )
    )
    if exists:
        if getattr(exists, "deleted", False):
            exists.deleted = False
            exists.updated_by = actor
    else:
        db.add(FurnaceChief(furnace_id=f.id, emp_id=emp_id, created_by=actor, updated_by=actor))
    if not f.chief_user_id:
        f.chief_user_id = emp_id
    return True

_OAPI = "https://oapi.dingtalk.com"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理
THROTTLE = 0.08
RATE_RETRY = 8
RATE_BACKOFF = 1.5
SYNC_KIND = "roster"

_token_cache: dict = {"token": None, "exp": 0.0}
_lock = threading.Lock()  # 防止并发同步


# ── HTTP 基础 ───────────────────────────────────────────────────────────
def _http(url: str, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now + 60:
        return _token_cache["token"]
    if not (settings.dingtalk_app_key and settings.dingtalk_app_secret):
        raise RuntimeError("未配置钉钉应用凭据（DINGTALK_APP_KEY / DINGTALK_APP_SECRET）")
    data = _http(
        f"{_OAPI}/gettoken?appkey={settings.dingtalk_app_key}&appsecret={settings.dingtalk_app_secret}"
    )
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken 失败：{data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def _oapi(path: str, token: str, payload: dict) -> dict:
    """调用 oapi 通讯录接口，节流 + 90002 退避重试。"""
    url = f"{_OAPI}{path}?access_token={token}"
    for _ in range(RATE_RETRY):
        time.sleep(THROTTLE)
        r = _http(url, json.dumps(payload).encode("utf-8"))
        ec = r.get("errcode", 0)
        if ec == 0:
            return r
        if ec == 88 and "90002" in (r.get("errmsg", "") or ""):
            time.sleep(RATE_BACKOFF)
            continue
        raise RuntimeError(f"{path}: errcode={ec} {r.get('errmsg')}")
    raise RuntimeError(f"{path}: 多次重试仍被限流(90002)")


# ── 进度状态 ─────────────────────────────────────────────────────────────
def _state(db: Session) -> DingtalkSyncState:
    st = db.scalar(select(DingtalkSyncState).where(DingtalkSyncState.kind == SYNC_KIND))
    if not st:
        st = DingtalkSyncState(kind=SYNC_KIND, status="idle")
        db.add(st)
        db.commit()
    return st


def _update(db: Session, st: DingtalkSyncState, **fields) -> None:
    for k, v in fields.items():
        setattr(st, k, v)
    st.updated_at = datetime.now(timezone.utc)
    db.commit()


def get_state(db: Session) -> dict:
    st = _state(db)
    return {
        "kind": st.kind,
        "status": st.status,
        "phase": st.phase,
        "currentRoot": st.current_root,
        "deptTotal": st.dept_total,
        "deptDone": st.dept_done,
        "memberTotalDepts": st.member_total_depts,
        "memberDoneDepts": st.member_done_depts,
        "memberCount": st.member_count,
        "message": st.message,
        "startedAt": st.started_at.isoformat() if st.started_at else None,
        "finishedAt": st.finished_at.isoformat() if st.finished_at else None,
        "updatedAt": st.updated_at.isoformat() if st.updated_at else None,
        "roots": load_roots(db),
    }


# ── 根部门校验（失效明确报错） ─────────────────────────────────────────────
def validate_roots(token: str, roots: dict[str, int]) -> dict[int, str]:
    """返回 {root_dept_id: 部门名}；任一失效抛 RuntimeError（含标签+ID）。"""
    names: dict[int, str] = {}
    for label, did in roots.items():
        r = _http(
            f"{_OAPI}/topapi/v2/department/get?access_token={token}",
            json.dumps({"dept_id": did}).encode("utf-8"),
        )
        if r.get("errcode") != 0:
            raise RuntimeError(
                f"根部门「{label}({did})」已失效或无权限（errcode={r.get('errcode')} "
                f"{r.get('errmsg')}）——请在「通讯录同步」页更新同步范围"
            )
        names[did] = (r.get("result") or {}).get("name") or label
    return names


# ── 部门树同步 ───────────────────────────────────────────────────────────
def sync_departments(
    db: Session, token: str, root_names: dict[int, str], st: DingtalkSyncState,
    roots: dict[str, int],
):
    """BFS 每个根的最新子树，并返回本次同步实际发现的部门。

    dingtalk_department_raw 仅作为单人导入时的部门路径缓存；人员同步必须使用本次
    listsub 遍历得到的部门列表，不能再次从缓存表取数。每个根遍历完成后同时清理
    钉钉已删除的旧部门，避免失效 dept_id 在后续同步中反复触发 60003。
    """
    _update(db, st, phase="部门树", dept_total=0, dept_done=0, message="开始同步部门树")
    path_of: dict[int, str] = {}
    current_depts: dict[int, list[int]] = {}
    done = 0
    for label, root in roots.items():
        root_name = root_names.get(root, label)
        path_of[root] = root_name
        root_depts = [root]
        # 根部门本身也入库
        add_counts(departmentFetched=1)
        _upsert_dept(db, root, None, root_name, root_name, None, root)
        queue = [root]
        while queue:
            did = queue.pop(0)
            kids = _oapi("/topapi/v2/department/listsub", token, {"dept_id": did}).get("result", [])
            add_counts(departmentFetched=len(kids))
            for k in kids:
                kid = k["dept_id"]
                name = k.get("name") or ""
                path_of[kid] = f"{path_of.get(did, '')}/{name}"
                fc = None
                m = re.search(r'"faceCount":"(\d+)"', k.get("ext") or "")
                if m:
                    fc = int(m.group(1))
                _upsert_dept(db, kid, k.get("parent_id"), name, path_of[kid], fc, root)
                root_depts.append(kid)
                queue.append(kid)
                done += 1
                if done % 10 == 0:
                    _update(db, st, current_root=label, dept_done=done,
                            message=f"部门树同步：{label} 已 {done} 个部门")
        # 只保留钉钉本次返回的部门。人员同步虽不再依赖缓存，缓存本身也应保持最新，
        # 供单人导入解析部门路径时使用。
        db.query(DingtalkDepartmentRaw).filter(
            DingtalkDepartmentRaw.root_dept_id == root,
            DingtalkDepartmentRaw.dept_id.notin_(root_depts),
        ).delete(synchronize_session=False)
        db.commit()
        add_counts(departmentUpserted=len(root_depts))
        current_depts[root] = root_depts
    _update(db, st, dept_total=done, dept_done=done, message=f"部门树同步完成，共 {done} 个子部门")
    return path_of, current_depts


def _upsert_dept(db, dept_id, parent_id, name, layer, face_count, root_dept_id):
    stmt = pg_insert(DingtalkDepartmentRaw).values(
        dept_id=dept_id, parent_id=parent_id, name=name, layer=layer,
        face_count=face_count, root_dept_id=root_dept_id,
    ).on_conflict_do_update(
        index_elements=["dept_id"],
        set_={"parent_id": parent_id, "name": name, "layer": layer,
              "face_count": face_count, "root_dept_id": root_dept_id,
              "synced_at": datetime.now(timezone.utc)},
    )
    db.execute(stmt)


# ── 成员同步 ─────────────────────────────────────────────────────────────
def sync_members(
    db: Session, token: str, path_of: dict[int, str], st: DingtalkSyncState,
    roots: dict[str, int], current_depts: dict[int, list[int]],
    refreshed_userids: set[str] | None = None,
):
    """遍历本次部门树结果 → user/list 分页 → upsert 原始员工（按 userid 去重）。

    单个部门拉取失败时回滚该部门尚未提交的数据、记录原因并继续，避免一个已删除部门
    或局部钉钉接口异常导致整棵通讯录同步失效。失败明细返回给任务入口生成结束摘要。
    """
    seen: set[str] = set()
    member_count = 0
    done_depts = 0
    failed_depts: list[dict] = []
    root_label = {v: k for k, v in roots.items()}
    total = sum(len(current_depts.get(root, [])) for root in roots.values())
    _update(db, st, phase="人员", member_total_depts=total, member_done_depts=0, member_count=0,
            message=f"开始同步人员，共 {total} 个部门")
    for root in roots.values():
        for did in current_depts.get(root, []):
            failed_this_dept = False
            dept_seen: set[str] = set()
            try:
                cursor = 0
                while True:
                    res = _oapi("/topapi/v2/user/list", token, {
                        "dept_id": did, "cursor": cursor, "size": 100,
                        "order_field": "modify_desc", "contain_access_limit": False,
                    }).get("result", {})
                    add_counts(memberFetched=len(res.get("list", [])))
                    for u in res.get("list", []):
                        uid = u.get("userid")
                        if not uid or uid in seen or uid in dept_seen:
                            continue
                        dept_seen.add(uid)
                        _upsert_emp(db, u, did, path_of.get(did), root_label.get(root))
                    if not res.get("has_more"):
                        break
                    cursor = res.get("next_cursor", 0)
                db.commit()
                add_counts(memberUpserted=len(dept_seen))
                seen.update(dept_seen)
                member_count += len(dept_seen)
            except Exception as exc:  # noqa: BLE001 - 单部门失败必须继续同步其余部门
                db.rollback()
                failed_this_dept = True
                add_counts(failedDepartments=1)
                mark("partial", "部分部门拉取失败，请检查通讯录同步进度中的失败部门")
                failed_depts.append({
                    "deptId": did,
                    "deptPath": path_of.get(did) or str(did),
                    "reason": str(exc),
                })
            done_depts += 1
            if done_depts % 5 == 0 or failed_this_dept:
                _update(db, st, current_root=root_label.get(root), member_done_depts=done_depts,
                        member_count=member_count,
                        message=(
                            f"人员同步：已 {done_depts}/{total} 部门，累计 {member_count} 人"
                            f"，跳过 {len(failed_depts)} 个失败部门"
                        ))
    _update(db, st, member_done_depts=done_depts, member_count=member_count,
            message=f"人员同步完成，共 {member_count} 人，跳过 {len(failed_depts)} 个失败部门")
    if refreshed_userids is not None:
        refreshed_userids.update(seen)
    return failed_depts


def _member_failure_summary(failed_depts: list[dict], limit: int = 10) -> str:
    """将部门失败明细压缩到同步状态 message，避免页面提示无限增长。"""
    if not failed_depts:
        return ""
    details = "；".join(
        f"{item['deptPath']}({item['deptId']}): {item['reason']}"
        for item in failed_depts[:limit]
    )
    omitted = len(failed_depts) - limit
    if omitted > 0:
        details += f"；另有 {omitted} 个部门"
    return f"；跳过 {len(failed_depts)} 个失败部门：{details}"


def _upsert_emp(db, u: dict, dept_id, dept_path, root_label):
    stmt = pg_insert(DingtalkEmployeeRaw).values(
        userid=u.get("userid"), name=u.get("name"), job_number=u.get("job_number"),
        title=u.get("title"), mobile=u.get("mobile"), unionid=u.get("unionid"),
        hired_date=u.get("hired_date"), active=u.get("active"),
        dept_id=dept_id, dept_path=dept_path, root_label=root_label,
    ).on_conflict_do_update(
        index_elements=["userid"],
        set_={
            "name": u.get("name"), "job_number": u.get("job_number"), "title": u.get("title"),
            "mobile": u.get("mobile"), "unionid": u.get("unionid"),
            "hired_date": u.get("hired_date"), "active": u.get("active"),
            "dept_id": dept_id, "dept_path": dept_path, "root_label": root_label,
            "synced_at": datetime.now(timezone.utc),
        },
    )
    db.execute(stmt)


def _sync_employment_statuses(
    db: Session, refreshed_userids: set[str], actor: str = "钉钉通讯录同步"
) -> dict:
    """将本次真实刷新的通讯录 active 同步到已有 employee。

    只接受明确的 True/False；NULL 表示远程未提供状态，必须保留本地值。
    同工号多个远程账号状态冲突时不作猜测。离职只更新人事状态，
    不改 deleted，也不解绑工作点、收藏和历史业务数据。
    """
    if not refreshed_userids:
        return {
            "matched": 0,
            "changed": 0,
            "active": 0,
            "departed": 0,
            "unknown": 0,
            "conflicts": 0,
        }
    raws = db.scalars(
        select(DingtalkEmployeeRaw).where(
            DingtalkEmployeeRaw.userid.in_(refreshed_userids)
        )
    ).all()
    raw_emp_ids = {_raw_emp_id(raw) for raw in raws if _raw_emp_id(raw)}
    raw_userids = {(raw.userid or "").strip() for raw in raws if (raw.userid or "").strip()}
    employees = db.scalars(
        select(Employee).where(
            or_(Employee.emp_id.in_(raw_emp_ids), Employee.dd_user_id.in_(raw_userids))
        )
    ).all()
    by_emp_id = {employee.emp_id: employee for employee in employees}
    by_dd_user_id: dict[str, Employee] = {}
    duplicate_dd_user_ids: set[str] = set()
    for employee in employees:
        dd_user_id = (employee.dd_user_id or "").strip()
        if not dd_user_id:
            continue
        if dd_user_id in by_dd_user_id:
            duplicate_dd_user_ids.add(dd_user_id)
        else:
            by_dd_user_id[dd_user_id] = employee
    for dd_user_id in duplicate_dd_user_ids:
        by_dd_user_id.pop(dd_user_id, None)

    statuses: dict[int, tuple[Employee, set[str], list[str]]] = {}
    unknown = 0
    for raw in raws:
        if raw.active is None:
            unknown += 1
            continue
        userid = (raw.userid or "").strip()
        employee = by_emp_id.get(_raw_emp_id(raw)) or by_dd_user_id.get(userid)
        if employee is None:
            continue
        status = "active" if raw.active else "departed"
        current = statuses.setdefault(employee.id, (employee, set(), []))
        current[1].add(status)
        current[2].append(userid)

    changed = active = departed = conflicts = 0
    for employee, remote_statuses, userids in statuses.values():
        if len(remote_statuses) != 1:
            conflicts += 1
            continue
        status = next(iter(remote_statuses))
        if employee.employment_status == status:
            continue
        old_status = employee.employment_status
        employee.employment_status = status
        employee.updated_by = actor
        db.add(
            AuditLog(
                actor=actor,
                action="employee_employment_status_sync",
                payload=json.dumps(
                    {
                        "empId": employee.emp_id,
                        "old": old_status,
                        "new": status,
                        "userids": sorted(set(userids)),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        changed += 1
        active += int(status == "active")
        departed += int(status == "departed")
    db.commit()
    return {
        "matched": len(statuses),
        "changed": changed,
        "active": active,
        "departed": departed,
        "unknown": unknown,
        "conflicts": conflicts,
    }


# ── 后台任务入口 ─────────────────────────────────────────────────────────
def run_directory_sync_locked(db: Session) -> dict:
    """在调用方已持有通讯录锁时同步，并返回本次真实刷新的员工范围。"""
    st = _state(db)
    _update(db, st, status="running", phase="校验根", current_root=None,
            dept_total=0, dept_done=0, member_total_depts=0, member_done_depts=0,
            member_count=0, message="校验根部门…", started_at=datetime.now(timezone.utc),
            finished_at=None)
    roots = load_roots(db)
    token = _token()
    root_names = validate_roots(token, roots)
    path_of, current_depts = sync_departments(db, token, root_names, st, roots)
    refreshed_userids: set[str] = set()
    failed_depts = sync_members(
        db, token, path_of, st, roots, current_depts,
        refreshed_userids=refreshed_userids,
    )
    employment_statuses = _sync_employment_statuses(db, refreshed_userids)
    refreshed_emp_ids = sorted({
        _raw_emp_id(raw)
        for raw in db.scalars(
            select(DingtalkEmployeeRaw).where(
                DingtalkEmployeeRaw.userid.in_(refreshed_userids)
            )
        ).all()
        if _raw_emp_id(raw)
    })
    _update(db, st, status="done", phase="完成", finished_at=datetime.now(timezone.utc),
            message=(
                f"同步完成：{st.dept_done} 个子部门，{st.member_count} 人"
                f"{_member_failure_summary(failed_depts)}"
            ))
    return {
        "refreshedEmpIds": refreshed_emp_ids,
        "refreshedUserIds": sorted(refreshed_userids),
        "failedDepts": failed_depts,
        "employmentStatuses": employment_statuses,
    }


def mark_directory_sync_error(db: Session, error: Exception | str) -> None:
    """复位共用拉取状态，避免自动/一键拉取失败后永久停留在 running。"""
    st = _state(db)
    _update(
        db, st, status="error", phase="失败",
        finished_at=datetime.now(timezone.utc), message=str(error),
    )


def try_acquire_directory_sync() -> bool:
    return _lock.acquire(blocking=False)


def release_directory_sync() -> None:
    # 跨线程移交是有意设计；重复释放必须留下调用栈，不能静默掩盖互斥失效。
    try:
        _lock.release()
    except RuntimeError:
        logger.warning("尝试释放未持有的通讯录同步锁", stack_info=True)


@contextmanager
def directory_sync_lock():
    """同一线程内获取并保证释放的上下文管理器；跨线程（start_run 获取、后台线程释放）
    仍用 try_acquire_directory_sync / release_directory_sync 原语。"""
    if not _lock.acquire(blocking=False):
        raise RuntimeError("通讯录拉取或员工同步任务正在执行")
    try:
        yield
    finally:
        release_directory_sync()


@logged_sync("directory")
def _run(actor="admin"):
    db = SessionLocal()
    try:
        run_directory_sync_locked(db)
    except Exception as e:  # noqa: BLE001
        mark("failed", str(e))
        try:
            st = _state(db)
            _update(db, st, status="error", finished_at=datetime.now(timezone.utc),
                    message=f"同步失败：{e}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()
        release_directory_sync()


@logged_start("directory")
def start_sync(db: Session, actor: str = "admin") -> dict:
    """非阻塞启动后台同步；已在跑则返回 alreadyRunning。"""
    if not _lock.acquire(blocking=False):
        return {"started": False, "alreadyRunning": True}
    st = None
    try:
        # 在线程启动前同步落库 running，前端第一次轮询不可能读到上一轮遗留的 done。
        st = _state(db)
        _update(
            db,
            st,
            status="running",
            phase="等待执行",
            current_root=None,
            dept_total=0,
            dept_done=0,
            member_total_depts=0,
            member_done_depts=0,
            member_count=0,
            message="通讯录同步任务已启动，等待后台执行",
            started_at=datetime.now(timezone.utc),
            finished_at=None,
        )
        threading.Thread(target=_run, args=(actor,), daemon=True).start()
    except Exception as exc:
        try:
            if st is not None:
                _update(
                    db,
                    st,
                    status="error",
                    phase="启动失败",
                    message=f"通讯录同步启动失败：{exc}",
                    finished_at=datetime.now(timezone.utc),
                )
        finally:
            release_directory_sync()
        raise
    return {"started": True, "alreadyRunning": False}


# ── 转换：原始员工 → employee 花名册 ──────────────────────────────────────
def _apply_single_favorite(
    db: Session, emp_id: str, kind: str, value: str, overwrite: bool
) -> bool:
    """写单值员工收藏；默认只填空，overwrite=True 时替换同 kind 的其他值。"""
    existing = db.scalars(
        select(EmployeeFavorite).where(
            EmployeeFavorite.emp_id == emp_id, EmployeeFavorite.kind == kind
        )
    ).all()
    if not overwrite:
        if existing:
            return False
        db.add(EmployeeFavorite(emp_id=emp_id, kind=kind, value=value))
        return True
    for fav in existing:
        if fav.value != value:
            db.delete(fav)
    if not any(fav.value == value for fav in existing):
        db.add(EmployeeFavorite(emp_id=emp_id, kind=kind, value=value))
    return True


def _apply_post_favorite(db: Session, emp_id: str, post: str, overwrite: bool) -> bool:
    return _apply_single_favorite(db, emp_id, "post", post, overwrite)


def _apply_region_favorite(db: Session, emp_id: str, region: str, overwrite: bool) -> bool:
    return _apply_single_favorite(db, emp_id, "region", region, overwrite)


def _transform_one(
    db: Session,
    r: DingtalkEmployeeRaw,
    title_map: dict[str, dict],
    root_regions: dict[str, str],
    *,
    overwrite: bool, actor: str, reactivate: bool = False, enable_push: bool = False,
) -> dict | None:
    """单个原始员工 → employee 花名册（写角色、常用职位/地区及值班长绑定）。

    返回该员工的处理结果；emp_id 无法确定时返回 None。
    reactivate=True 时把软删除的员工重新激活（供单人自助导入：登录被判为不匹配往往是被软删）。
    enable_push=True 时（仅用户自助导入）把 push_enabled 置 True，默认开启钉钉通知；
    手动批量同步/HR导入传 False，不动该值（新建默认 False，已有保持原值）。
    """
    emp_id = (r.job_number or r.userid or "").strip()
    if not emp_id:
        return None
    e = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    if e:
        # 已存在：更姓名/钉钉号，默认保留 deleted（reactivate 时重新激活）
        e.name = r.name or e.name
        e.dd_user_id = r.userid or e.dd_user_id
        e.updated_by = actor
        action = "updated"
        if reactivate and getattr(e, "deleted", False):
            e.deleted = False
        if enable_push:  # 自助导入：本人确认身份后默认开启通知
            e.push_enabled = True
    else:
        e = Employee(
            emp_id=emp_id, name=r.name or emp_id, dd_user_id=r.userid or emp_id,
            role=None, sync_status="钉钉同步", created_by=actor, updated_by=actor,
            push_enabled=bool(enable_push),
            employment_status="active" if r.active is not False else "departed",
        )
        db.add(e)
        action = "inserted"

    status_changed = False
    if r.active is not None:
        remote_status = "active" if r.active else "departed"
        if e.employment_status != remote_status:
            old_status = e.employment_status
            e.employment_status = remote_status
            e.updated_by = actor
            status_changed = True
            db.add(AuditLog(
                actor=actor,
                action="employee_employment_status_sync",
                payload=json.dumps({
                    "empId": emp_id,
                    "old": old_status,
                    "new": remote_status,
                    "userid": r.userid,
                }, ensure_ascii=False),
            ))

    role_set = post_set = region_set = chief_bound = chief_unmatched = False
    mapped_region = root_regions.get((r.root_label or "").strip())
    if mapped_region and _apply_region_favorite(db, emp_id, mapped_region, overwrite):
        region_set = True
    m = title_map.get((r.title or "").strip()) if r.title else None
    if m:
        mapped_role = m.get("role")
        mapped_post = m.get("post")
        if mapped_role and (overwrite or not e.role):
            if e.role != mapped_role:
                e.role = mapped_role
                role_set = True
        if mapped_post:
            if _apply_post_favorite(db, emp_id, mapped_post, overwrite):
                post_set = True
        # 特殊：映射为值班长且最终角色为值班长 → 按部门自动绑定工作点-值班长
        if mapped_role == "chief" and e.role == "chief":
            if _bind_chief_by_dept(db, emp_id, r, actor, mapped_region):
                chief_bound = True
            else:
                chief_unmatched = True
    return {
        "empId": emp_id, "name": e.name, "role": e.role, "title": r.title,
        "action": action, "roleSet": role_set, "postSet": post_set,
        "regionSet": region_set,
        "employmentStatus": e.employment_status, "employmentStatusChanged": status_changed,
        "chiefBound": chief_bound, "chiefUnmatched": chief_unmatched,
    }


def transform(db: Session, overwrite: bool = False) -> dict:
    """原始员工 → employee 花名册，并写角色、常用职位/地区及值班长绑定。

    overwrite=False（默认）：role 仅在为空时填入，常用职位/地区仅在无收藏时填入。
    overwrite=True：role / 常用职位 / 常用地区以本次映射为准覆盖。
    role 映射为 chief 且最终角色为值班长时，按部门自动绑定工作点-值班长。
    """
    title_map = load_title_map(db)
    root_regions = load_root_region_names(db)
    raws = db.scalars(select(DingtalkEmployeeRaw)).all()
    actor = "钉钉同步"
    inserted = updated = role_set = post_set = region_set = chief_bound = 0
    chief_unmatched: list[str] = []
    for r in raws:
        res = _transform_one(
            db, r, title_map, root_regions, overwrite=overwrite, actor=actor
        )
        if not res:
            continue
        if res["action"] == "inserted":
            inserted += 1
        else:
            updated += 1
        role_set += int(res["roleSet"])
        post_set += int(res["postSet"])
        region_set += int(res["regionSet"])
        chief_bound += int(res["chiefBound"])
        if res["chiefUnmatched"]:
            chief_unmatched.append(f"{res['name']}({res['empId']})")
    db.commit()
    total = db.scalar(select(func.count()).select_from(DingtalkEmployeeRaw)) or 0
    return {
        "inserted": inserted, "updated": updated, "rawTotal": total,
        "roleSet": role_set, "postSet": post_set,
        "regionSet": region_set,
        "chiefBound": chief_bound, "chiefUnmatched": chief_unmatched,
        "overwrite": overwrite,
    }


# ── 员工差异：按字段预览与同步 ─────────────────────────────────────────────
DIFF_FIELDS = ("region", "role", "post")
PROTECTED_SYNC_ROLES = {"admin", "hr", "stat"}
SYNC_ROLE_VALUES = {"worker", "chief", "leader", "repairer", *PROTECTED_SYNC_ROLES}


def _raw_emp_id(raw: DingtalkEmployeeRaw) -> str:
    return (raw.job_number or raw.userid or "").strip()


def _favorite_values(db: Session, emp_id: str, kind: str) -> list[str]:
    return sorted(set(db.scalars(
        select(EmployeeFavorite.value).where(
            EmployeeFavorite.emp_id == emp_id,
            EmployeeFavorite.kind == kind,
        )
    ).all()))


def _remote_targets(
    raw: DingtalkEmployeeRaw,
    title_map: dict[str, dict],
    root_regions: dict[str, str],
) -> dict[str, str | None]:
    mapping = title_map.get((raw.title or "").strip()) if raw.title else None
    return {
        "region": root_regions.get((raw.root_label or "").strip()),
        "role": (mapping or {}).get("role") or None,
        "post": (mapping or {}).get("post") or None,
    }


def _difference_item(
    db: Session,
    employee: Employee,
    raw: DingtalkEmployeeRaw,
    title_map: dict[str, dict],
    root_regions: dict[str, str],
    favorites: dict[tuple[str, str], list[str]] | None = None,
    update_mode: str = "append",
) -> dict:
    targets = _remote_targets(raw, title_map, root_regions)
    current_role = employee.role or infer_role(employee.dd_user_id or employee.emp_id)
    persisted_role = (employee.role or "").strip()
    current = {
        "region": (
            favorites.get((employee.emp_id, "region"), [])
            if favorites is not None else _favorite_values(db, employee.emp_id, "region")
        ),
        "role": current_role,
        "post": (
            favorites.get((employee.emp_id, "post"), [])
            if favorites is not None else _favorite_values(db, employee.emp_id, "post")
        ),
    }
    fields: dict[str, dict] = {}
    for field in DIFF_FIELDS:
        target = targets[field]
        is_managed_region = field == "region" and current_role in ("hr", "stat")
        reason = None if target else (
            "根部门未配置地区映射" if field == "region" else "钉钉职位未配置该字段映射"
        )
        different = bool(target) and (
            (not persisted_role or persisted_role != target)
            if field == "role"
            else (
                current[field] != [target]
                if is_managed_region or update_mode == "replace"
                else target not in current[field]
            )
        )
        protected = bool(target) and (
            is_managed_region
            or (
                field == "role"
                and (current_role in PROTECTED_SYNC_ROLES or target in PROTECTED_SYNC_ROLES)
            )
        )
        fields[field] = {
            "current": current[field],
            "remote": target,
            "different": different,
            "syncable": bool(target) and different,
            "reason": reason,
            "protected": protected,
            # 普通收藏只追加，不会删除；HR/统计地区是单值数据范围，替换前必须明示删除项。
            "willRemove": (
                [value for value in current[field] if value != target]
                if field in ("region", "post")
                and (is_managed_region or update_mode == "replace")
                else []
            ),
        }
    return {
        "empId": employee.emp_id,
        "name": employee.name,
        "userid": raw.userid,
        "remoteName": raw.name,
        "remoteTitle": raw.title,
        "rootLabel": raw.root_label,
        "deptPath": raw.dept_path,
        "remoteSyncedAt": raw.synced_at.isoformat() if raw.synced_at else None,
        "fields": fields,
        "hasDifference": any(v["different"] for v in fields.values()),
    }


def list_employee_differences(
    db: Session,
    *,
    q: str | None = None,
    field: str | None = None,
    different_only: bool = True,
    page: int = 1,
    page_size: int = 100,
    update_mode: str = "append",
) -> dict:
    if field and field not in DIFF_FIELDS:
        raise ValueError("field must be region, role or post")
    if update_mode not in ("append", "fill_empty", "replace"):
        raise ValueError("updateMode must be append, fill_empty or replace")
    title_map = load_title_map(db)
    root_regions = load_root_region_names(db)
    employees = {
        employee.emp_id: employee
        for employee in db.scalars(
            select(Employee).where(Employee.deleted.is_(False))
        ).all()
    }
    favorites: dict[tuple[str, str], list[str]] = {}
    for favorite in db.scalars(
        select(EmployeeFavorite).where(
            EmployeeFavorite.emp_id.in_(employees),
            EmployeeFavorite.kind.in_(["region", "post"]),
        )
    ).all():
        favorites.setdefault((favorite.emp_id, favorite.kind), []).append(favorite.value)
    for values in favorites.values():
        values[:] = sorted(set(values))
    keyword = (q or "").strip().lower()
    items: list[dict] = []
    latest = db.scalar(select(func.max(DingtalkEmployeeRaw.synced_at)))
    seen_emp_ids: set[str] = set()
    raws = db.scalars(
        select(DingtalkEmployeeRaw)
        .where(DingtalkEmployeeRaw.active.is_(True))
        .order_by(
            DingtalkEmployeeRaw.job_number,
            DingtalkEmployeeRaw.synced_at.desc(),
            DingtalkEmployeeRaw.userid,
        )
    ).all()
    for raw in raws:
        raw_emp_id = _raw_emp_id(raw)
        if raw_emp_id in seen_emp_ids:
            continue
        seen_emp_ids.add(raw_emp_id)
        employee = employees.get(raw_emp_id)
        if not employee:
            continue
        if keyword and keyword not in " ".join([
            employee.emp_id, employee.name or "", raw.name or "", raw.title or "",
            raw.dept_path or "",
        ]).lower():
            continue
        item = _difference_item(
            db,
            employee,
            raw,
            title_map,
            root_regions,
            favorites=favorites,
            update_mode=update_mode,
        )
        if field and not item["fields"][field]["different"]:
            continue
        if different_only and not item["hasDifference"]:
            continue
        items.append(item)
    total = len(items)
    start = (max(page, 1) - 1) * max(1, min(page_size, 500))
    size = max(1, min(page_size, 500))
    return {
        "total": total,
        "page": max(page, 1),
        "pageSize": size,
        "remoteSyncedAt": latest.isoformat() if latest else None,
        "items": items[start:start + size],
    }


def _unbind_chief(db: Session, emp_id: str, actor: str) -> dict:
    """解绑值班长：删除其工作点和其名下名单，保留其作为工人的他人名单关系。

    与 api.py 工作点配置（db.delete）及员工删除路径保持一致；避免软删行被
    api.py 工作点管理页 / confirm_service / scan_detail 等未过滤 deleted 的
    读路径继续当作在任值班长，也避免工作点保存时被重新激活。
    """
    rows = db.scalars(
        select(FurnaceChief).where(
            FurnaceChief.emp_id == emp_id, FurnaceChief.deleted.is_(False)
        )
    ).all()
    furnace_ids = [row.furnace_id for row in rows]
    worker_rows = db.scalars(
        select(ChiefWorker).where(ChiefWorker.chief_user_id == emp_id)
    ).all()
    worker_links = [
        {"chiefUserId": row.chief_user_id, "empId": row.emp_id}
        for row in worker_rows
    ]
    for row in rows:
        furnace = db.get(Furnace, row.furnace_id)
        if furnace and furnace.chief_user_id == emp_id:
            replacement = db.scalar(
                select(FurnaceChief.emp_id).where(
                    FurnaceChief.furnace_id == row.furnace_id,
                    FurnaceChief.emp_id != emp_id,
                    FurnaceChief.deleted.is_(False),
                ).order_by(FurnaceChief.id)
            )
            furnace.chief_user_id = replacement
        if furnace:
            furnace.updated_by = actor
        db.delete(row)
    for cw in worker_rows:
        db.delete(cw)
    return {"furnaceIds": furnace_ids, "chiefWorkers": worker_links}


def _apply_employee_differences_unlocked(
    db: Session,
    *,
    emp_ids: list[str],
    field: str,
    actor: str,
    confirm_protected: bool = False,
    update_mode: str | None = None,
) -> dict:
    if field not in DIFF_FIELDS:
        raise ValueError("field must be region, role or post")
    update_mode = update_mode or ("fill_empty" if field == "role" else "append")
    if update_mode not in ("append", "fill_empty", "replace"):
        raise ValueError("updateMode must be append, fill_empty or replace")
    if field == "role" and update_mode == "append":
        raise ValueError("role updateMode must be fill_empty or replace")
    ids = list(dict.fromkeys(x.strip() for x in emp_ids if x.strip()))
    if not ids:
        raise ValueError("请至少选择一名员工")
    if len(ids) > 500:
        raise ValueError("单次最多同步 500 名员工")
    # 白名单是所有差异同步入口的硬保护，不能只依赖前端隐藏按钮。
    from app.services.employee_difference_sync_service import (
        _is_fresh,
        _latest_raws,
        whitelist_ids,
    )

    protected_ids = whitelist_ids(db)
    batch = len(ids) > 1
    title_map = load_title_map(db)
    root_regions = load_root_region_names(db)
    raws, ambiguous_emp_ids = _latest_raws(db)
    employees = {
        employee.emp_id: employee
        for employee in db.scalars(
            select(Employee).where(
                Employee.emp_id.in_(ids), Employee.deleted.is_(False)
            )
        ).all()
    }
    changed = unchanged = 0
    skipped: list[dict] = []
    warnings: list[dict] = []
    for emp_id in ids:
        if emp_id in protected_ids:
            skipped.append({"empId": emp_id, "reason": "员工在同步白名单中"})
            continue
        employee = employees.get(emp_id)
        raw = raws.get(emp_id)
        if not employee or not raw:
            skipped.append({"empId": emp_id, "reason": "本地员工或在职远程员工不存在"})
            continue
        if emp_id in ambiguous_emp_ids:
            skipped.append({"empId": emp_id, "reason": "远程存在多个同工号账号"})
            continue
        if not _is_fresh(raw.synced_at):
            skipped.append({"empId": emp_id, "reason": "远程数据超过36小时未刷新"})
            continue
        item = _difference_item(
            db, employee, raw, title_map, root_regions, update_mode=update_mode
        )
        diff = item["fields"][field]
        if not diff["remote"]:
            skipped.append({"empId": emp_id, "reason": diff["reason"] or "未配置映射"})
            continue
        if field == "role" and diff["remote"] not in SYNC_ROLE_VALUES:
            # 读取侧仍需防御历史脏映射；不能只依赖当前 title-map 写接口的白名单。
            skipped.append({"empId": emp_id, "reason": "远程角色映射非法"})
            continue
        if not diff["different"]:
            unchanged += 1
            continue
        old_role_for_guard = str(diff["current"]) if field == "role" else ""
        chief_demotion = (
            field == "role" and old_role_for_guard == "chief" and diff["remote"] != "chief"
        )
        if (diff["protected"] or chief_demotion) and (batch or not confirm_protected):
            skipped.append({
                "empId": emp_id,
                "reason": (
                    "值班长降级仅允许单人二次确认"
                    if chief_demotion
                    else "管理角色或地区范围变动仅允许单人二次确认"
                ),
            })
            continue
        old_value = diff["current"]
        target = str(diff["remote"])
        did_change = False
        side_effects: dict = {}
        removed_values: list[str] = []
        if field in ("region", "post"):
            # 地区/职位原本允许多选。update_mode 决定是否删除其它值：
            # fill_empty 仅本地为空时写入；append 只追加；replace 删除其它值只保留目标。
            existing = db.scalars(select(EmployeeFavorite).where(
                EmployeeFavorite.emp_id == emp_id, EmployeeFavorite.kind == field
            )).all()
            if field == "region" and diff["protected"]:
                # HR/统计地区是单值权限范围，恒 overwrite（单人二次确认已在前面拦截）。
                removed_values = [row.value for row in existing if row.value != target]
                _apply_single_favorite(db, emp_id, field, target, overwrite=True)
                did_change = old_value != [target]
            elif update_mode == "replace":
                for row in existing:
                    if row.value != target:
                        removed_values.append(row.value)
                        db.delete(row)
                if not any(row.value == target for row in existing):
                    db.add(EmployeeFavorite(emp_id=emp_id, kind=field, value=target))
                did_change = sorted({row.value for row in existing}) != [target]
            elif update_mode == "fill_empty":
                if not existing:
                    db.add(EmployeeFavorite(emp_id=emp_id, kind=field, value=target))
                    did_change = True
            else:
                if not any(row.value == target for row in existing):
                    db.add(EmployeeFavorite(emp_id=emp_id, kind=field, value=target))
                    did_change = True
        else:
            old_role = str(old_value)
            persisted_role = (employee.role or "").strip()
            if update_mode == "fill_empty" and persisted_role:
                skipped.append({
                    "empId": emp_id,
                    "reason": "本地已有值，仅填空模式不修改",
                })
                continue
            if target in ("hr", "stat"):
                # 对齐 api.py 不变量：HR/统计只能配置一个所在地区；多地区先规范再同步角色。
                region_values = {
                    rv.value
                    for rv in db.scalars(
                        select(EmployeeFavorite).where(
                            EmployeeFavorite.emp_id == emp_id,
                            EmployeeFavorite.kind == "region",
                        )
                    ).all()
                }
                if len(region_values) > 1:
                    skipped.append({
                        "empId": emp_id,
                        "reason": "目标为 HR/统计但本地存在多个地区收藏，请先规范为单一地区",
                    })
                    continue
            if target == "chief" and old_role != "chief":
                if not _bind_chief_by_dept(
                    db, emp_id, raw, actor, root_regions.get((raw.root_label or "").strip())
                ):
                    # 先保证值班长绑定成功再写角色，避免制造「角色是值班长但无任何工作点」的半成品。
                    skipped.append({"empId": emp_id, "reason": "角色同步为值班长但未匹配到工作点，已跳过"})
                    continue
            employee.role = target
            # fill_empty 的“空”以持久化列为准；infer_role 只用于值班长/管理角色保护。
            # 否则 NULL 会被推断成 worker 后误判为已有值，预览与实际写入相反。
            did_change = persisted_role != target
            invalidate_sessions_for_employee(db, employee.emp_id, employee.dd_user_id)
            if old_role == "chief" and target != "chief":
                side_effects = _unbind_chief(db, emp_id, actor)
        if not did_change:
            unchanged += 1
            continue
        employee.updated_by = actor
        db.add(AuditLog(
            actor=actor,
            action="employee_directory_field_sync",
            payload=json.dumps({
                "empId": emp_id,
                "field": field,
                "old": old_value,
                "new": target,
                "mode": "batch" if batch else "single",
                "updateMode": update_mode,
                "removed": removed_values,
                "sideEffects": side_effects,
            }, ensure_ascii=False),
        ))
        changed += 1
    db.commit()
    add_counts(employeeChanged=changed)
    return {
        "requested": len(ids),
        "changed": changed,
        "unchanged": unchanged,
        "skipped": len(skipped),
        "skippedItems": skipped,
        "warnings": warnings,
        "field": field,
    }


@logged_sync("employee_difference")
def apply_employee_differences(
    db: Session,
    *,
    emp_ids: list[str],
    field: str,
    actor: str,
    confirm_protected: bool = False,
    update_mode: str | None = None,
    rule_payload: dict | None = None,
) -> dict:
    """旧员工管理入口也复用通讯录/规则同步互斥锁。

    这样该入口不会和 apply_rule 同时插入 EmployeeFavorite 或覆盖 role，避免唯一
    约束冲突导致规则同步整批回滚，也避免两个入口互相丢失更新。
    """
    with directory_sync_lock():
        ids = list(dict.fromkeys(value.strip() for value in emp_ids if value.strip()))
        rule_skipped: list[dict] = []
        if rule_payload is not None:
            from app.services.employee_difference_sync_service import normalize_rule, preview

            normalized = normalize_rule(rule_payload)
            effective_mode = update_mode or ("fill_empty" if field == "role" else "append")
            if normalized["field"] != field or normalized["updateMode"] != effective_mode:
                raise ValueError("提交规则与同步字段或更新方式不一致")
            reviewed = preview(db, normalized, page=1, page_size=500_000)
            reviewed_by_id = {item["empId"]: item for item in reviewed["items"]}
            eligible_ids: list[str] = []
            for emp_id in ids:
                item = reviewed_by_id.get(emp_id)
                if item is not None and item["action"] != "skip":
                    eligible_ids.append(emp_id)
                    continue
                rule_skipped.append({
                    "empId": emp_id,
                    "reason": (
                        item.get("skipReason")
                        if item is not None
                        else "员工已不再符合当前同步规则"
                    ) or "员工已不再符合当前同步规则",
                })
            ids = eligible_ids

        if ids:
            result = _apply_employee_differences_unlocked(
                db,
                emp_ids=ids,
                field=field,
                actor=actor,
                confirm_protected=confirm_protected,
                update_mode=update_mode,
            )
        else:
            result = {
                "requested": 0,
                "changed": 0,
                "unchanged": 0,
                "skipped": 0,
                "skippedItems": [],
                "warnings": [],
                "field": field,
            }
        if rule_skipped:
            result["requested"] = len(ids) + len(rule_skipped)
            result["skipped"] += len(rule_skipped)
            result["skippedItems"] = [*rule_skipped, *result["skippedItems"]]
        return result


# ── 单人导入：用钉钉直接查这一个 userid 并落地花名册 ────────────────────────
def _walk_dept_up(
    token: str, dept_id: int, root_ids: set[int], max_depth: int = 15,
) -> tuple[str | None, int | None]:
    """从 dept_id 沿 parent_id 向上走，命中某个根部门即返回 (完整路径, 根部门ID)。
    未命中任何配置的根部门返回 (None, None)。路径形如「根名/.../末级工作点编号」。"""
    names: list[str] = []
    cur = dept_id
    for _ in range(max_depth):
        res = _oapi("/topapi/v2/department/get", token, {"dept_id": cur}).get("result", {})
        names.append(res.get("name") or "")
        if cur in root_ids:
            return "/".join(reversed(names)), cur
        parent = res.get("parent_id")
        if not parent or parent == cur:
            break
        cur = parent
    return None, None


def _resolve_dept_context(
    db: Session, token: str | None, dept_id_list: list[int], roots: dict[str, int],
) -> tuple[int | None, str | None, str | None]:
    """为单个用户定位「归属根 + 部门路径」，供值班长按部门绑定工作点。

    优先用已同步的 dingtalk_department_raw（无需额外请求）；
    缓存未命中则用 department/get 实时向上回溯找根部门（即便未跑过整体同步也能用）。
    返回 (dept_id, dept_path, root_label)；无法定位时尽量返回首个部门、路径/归属为空。
    """
    root_ids = set(roots.values())
    id_to_label = {v: k for k, v in roots.items()}
    dept_id_list = [int(d) for d in (dept_id_list or [])]
    # 1) 缓存命中（已跑过通讯录同步）
    for did in dept_id_list:
        row = db.scalar(select(DingtalkDepartmentRaw).where(DingtalkDepartmentRaw.dept_id == did))
        if row and row.root_dept_id in root_ids:
            return did, row.layer, id_to_label.get(row.root_dept_id)
    # 2) 实时回溯（未同步或部门不在缓存里）
    if dept_id_list and token is None:
        token = _token()
    for did in dept_id_list:
        assert token is not None
        path, root_id = _walk_dept_up(token, did, root_ids)
        if root_id is not None:
            return did, path, id_to_label.get(root_id)
    return (dept_id_list[0] if dept_id_list else None), None, None


def import_single_user(
    db: Session, userid: str, *, overwrite: bool = False, actor: str = "HR导入",
    enable_push: bool = False, user_detail: dict | None = None,
) -> dict:
    """用钉钉直接查询单个 userid 的用户信息，落地原始员工表并转入 employee 花名册。

    与「批量同步 + 转换」同源：复用 _upsert_emp / _transform_one，因此角色映射、
    常用岗位、根部门常用地区映射及值班长按部门绑定工作点的规则完全一致。
    登录流程可传入已取得的 user_detail，避免重复调用钉钉 user/get。
    返回该用户的导入结果（含 empId/role/是否绑定工作点等）。
    """
    userid = (userid or "").strip()
    if not userid:
        raise ValueError("缺少钉钉 userid")
    token: str | None = None
    if user_detail is None:
        token = _token()
        r = _http(
            f"{_OAPI}/topapi/v2/user/get?access_token={token}",
            json.dumps({"userid": userid}).encode("utf-8"),
        )
        if r.get("errcode") != 0:
            raise RuntimeError(
                f"钉钉查询用户失败（userid={userid}）：errcode={r.get('errcode')} {r.get('errmsg')}"
            )
        u = r.get("result") or {}
    else:
        # 登录接口已经调用过 user/get；首次导入复用同一份响应，避免再次请求钉钉。
        u = dict(user_detail)
        returned_uid = str(u.get("userid") or "").strip()
        if returned_uid and returned_uid != userid:
            raise RuntimeError("钉钉登录身份与待导入用户不一致")
        if not u:
            raise RuntimeError("钉钉未返回可导入的用户详情")
    real_uid = u.get("userid") or userid
    roots = load_roots(db)
    dept_id, dept_path, root_label = _resolve_dept_context(
        db, token, u.get("dept_id_list") or [], roots
    )
    _upsert_emp(
        db,
        {
            "userid": real_uid, "name": u.get("name"), "job_number": u.get("job_number"),
            "title": u.get("title"), "mobile": u.get("mobile"), "unionid": u.get("unionid"),
            "hired_date": u.get("hired_date"), "active": u.get("active"),
        },
        dept_id, dept_path, root_label,
    )
    db.flush()
    raw = db.scalar(select(DingtalkEmployeeRaw).where(DingtalkEmployeeRaw.userid == real_uid))
    res = _transform_one(
        db,
        raw,
        load_title_map(db),
        load_root_region_names(db),
        overwrite=overwrite,
        actor=actor,
        reactivate=True,
        enable_push=enable_push,
    )
    if not res:
        db.rollback()
        raise ValueError("该用户缺少工号且无 userid，无法生成员工档案")
    db.commit()
    return {
        **res, "userid": real_uid, "deptPath": dept_path, "rootLabel": root_label,
        "title": u.get("title"),
    }


def list_raw(db: Session, q: str | None = None, limit: int = 500) -> dict:
    stmt = select(DingtalkEmployeeRaw)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            (DingtalkEmployeeRaw.userid.like(like))
            | (DingtalkEmployeeRaw.name.like(like))
            | (DingtalkEmployeeRaw.job_number.like(like))
        )
    rows = db.scalars(stmt.order_by(DingtalkEmployeeRaw.job_number).limit(max(1, min(limit, 100000)))).all()
    total = db.scalar(select(func.count()).select_from(DingtalkEmployeeRaw)) or 0
    return {
        "total": total, "count": len(rows),
        "items": [
            {
                "userid": x.userid, "name": x.name, "jobNumber": x.job_number,
                "title": x.title, "mobile": x.mobile, "active": x.active,
                "deptPath": x.dept_path, "rootLabel": x.root_label,
            }
            for x in rows
        ],
    }
