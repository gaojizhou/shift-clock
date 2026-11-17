"""Session helpers for Mock/DingTalk auth.

Primary storage is the database so deployments/restarts do not invalidate logins.
The in-memory map is a process-local identity cache and a test fallback; it is
not authoritative and is never expected to keep sessions alive across processes.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings

_sessions: dict[str, dict] = {}


def session_ttl_seconds() -> int:
    return max(60, int(settings.fdc_session_ttl_seconds or 0))


def session_cookie_max_age() -> int:
    return session_ttl_seconds()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(now: datetime | None = None) -> datetime:
    return (now or _now()) + timedelta(seconds=session_ttl_seconds())


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    exp = _normalize_dt(expires_at)
    return exp is None or exp <= (now or _now())


def _renewal_due(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """Renew only in the final quarter of the TTL to avoid one write per request."""
    current = now or _now()
    exp = _normalize_dt(expires_at)
    if exp is None or exp <= current:
        return False
    return exp - current <= timedelta(seconds=max(15, session_ttl_seconds() // 4))


def _as_dict(userid: str, role: str, is_super: bool = False) -> dict:
    return {"userid": userid, "role": role, "super": is_super}


def cleanup_expired_sessions(db: Session, now: datetime | None = None) -> int:
    from app.models import AppSession

    current = now or _now()
    result = db.execute(delete(AppSession).where(AppSession.expires_at <= current))
    return int(result.rowcount or 0)


def set_session(
    session_id: str,
    userid: str,
    role: str,
    is_super: bool = False,
    db: Session | None = None,
) -> None:
    now = _now()
    expires_at = _expires_at(now)
    _sessions[session_id] = {**_as_dict(userid, role, is_super), "expires_at": expires_at}
    if db is None:
        return
    from app.models import AppSession

    row = db.get(AppSession, session_id)
    if row is None:
        db.add(
            AppSession(
                session_id=session_id,
                userid=userid,
                role=role,
                is_super=is_super,
                expires_at=expires_at,
            )
        )
    else:
        row.userid = userid
        row.role = role
        row.is_super = is_super
        row.updated_at = now
        row.expires_at = expires_at
    cleanup_expired_sessions(db, now=now)
    db.flush()


def get_session(session_id: str | None, db: Session | None = None, *, touch: bool = True) -> dict | None:
    if not session_id:
        return None
    if db is not None:
        from app.models import AppSession

        row = db.get(AppSession, session_id)
        if row is None:
            _sessions.pop(session_id, None)
            return None
        now = _now()
        if _is_expired(row.expires_at, now=now):
            if touch:
                _sessions.pop(session_id, None)
                db.delete(row)
                db.flush()
            return None
        if not touch:
            # 请求审计启用 Redis 队列后，中间件不再为识别身份额外查询数据库。
            # 鉴权依赖的只读查询顺手填充进程缓存，后续中间件即可零 DB 读取身份。
            _sessions[session_id] = {
                **_as_dict(row.userid, row.role, row.is_super),
                "expires_at": row.expires_at,
            }
            return _as_dict(row.userid, row.role, row.is_super)
        row.updated_at = now
        row.expires_at = _expires_at(now)
        _sessions[session_id] = {
            **_as_dict(row.userid, row.role, row.is_super),
            "expires_at": row.expires_at,
        }
        db.flush()
        return _as_dict(row.userid, row.role, row.is_super)
    sess = _sessions.get(session_id)
    if sess is None:
        return None
    if _is_expired(sess.get("expires_at")):
        _sessions.pop(session_id, None)
        return None
    if touch:
        sess["expires_at"] = _expires_at()
    return _as_dict(sess["userid"], sess["role"], bool(sess.get("super")))


def get_session_with_renewal(session_id: str | None, db: Session) -> tuple[dict | None, bool]:
    """Validate a DB session and renew it only when its remaining TTL is low.

    The caller must commit when ``renewed`` is true. Keeping the threshold logic
    here prevents authentication from depending on the asynchronous audit worker.
    """
    sess = get_session(session_id, db=db, touch=False)
    if sess is None or not session_id:
        return sess, False

    from app.models import AppSession

    row = db.get(AppSession, session_id)
    now = _now()
    if row is None or not _renewal_due(row.expires_at, now=now):
        return sess, False
    row.updated_at = now
    row.expires_at = _expires_at(now)
    _sessions[session_id] = {
        **_as_dict(row.userid, row.role, row.is_super),
        "expires_at": row.expires_at,
    }
    db.flush()
    return sess, True


def create_session(userid: str, role: str, is_super: bool = False, db: Session | None = None) -> str:
    session_id = str(uuid.uuid4())
    set_session(session_id, userid, role, is_super=is_super, db=db)
    return session_id


def create_super_session(db: Session | None = None) -> str:
    """超级管理员会话：身份为 admin，且带 super 标记（可切换成任意人后仍能切回）。"""
    return create_session("admin", "admin", is_super=True, db=db)


def mark_super(session_id: str | None, db: Session | None = None) -> dict | None:
    """把现有 admin 会话提升为 super，用于保留身份切换能力。"""
    sess = get_session(session_id, db=db)
    if not sess:
        return None
    set_session(session_id, sess["userid"], sess["role"], is_super=True, db=db)
    return _as_dict(sess["userid"], sess["role"], True)


def impersonate_session(
    session_id: str | None,
    userid: str,
    role: str,
    db: Session | None = None,
    is_super: bool | None = None,
) -> dict | None:
    """原地改当前会话的身份（userid/role），保留 super 标记。
    返回更新后的会话；会话不存在或非 super 返回 None。
    """
    sess = get_session(session_id, db=db)
    if not sess or not sess.get("super"):
        return None
    next_super = sess.get("super") if is_super is None else is_super
    set_session(session_id, userid, role, is_super=bool(next_super), db=db)
    sess = _as_dict(userid, role, bool(next_super))
    return sess


def delete_session(session_id: str | None, db: Session | None = None) -> None:
    if not session_id:
        return
    _sessions.pop(session_id, None)
    if db is None:
        return
    from app.models import AppSession

    row = db.get(AppSession, session_id)
    if row is not None:
        db.delete(row)
        db.flush()


def invalidate_sessions_for_employee(db: Session, *userids: str) -> int:
    """删除某员工的所有会话（DB + 内存），使角色/权限变更即时生效。

    userids 传该员工的 emp_id 与 dd_user_id（会话可能以任一作为 userid 建立）。
    """
    from app.models import AppSession

    uid_set = {u for u in userids if u}
    if not uid_set:
        return 0
    for sid in [sid for sid, sess in _sessions.items() if sess.get("userid") in uid_set]:
        _sessions.pop(sid, None)
    result = db.execute(delete(AppSession).where(AppSession.userid.in_(uid_set)))
    db.flush()
    return int(result.rowcount or 0)


def infer_role(userid: str, role: str | None = None) -> str:
    if role:
        return role
    uid = userid.upper()
    if userid.startswith("L"):
        return "chief"
    if uid.startswith("STAT") or userid == "S0001":
        return "stat"
    if uid.startswith("HR") or userid == "H0001":
        return "hr"
    return "worker"


# 调试推送默认目标：留空表示不内置任何真实钉钉 userId，必须显式指定目标。
DEMO_PUSH_DD = ""


def resolve_identity(db, raw: str, explicit: str | None = None) -> tuple[str, str, str | None]:
    """把登录 userId 归一到员工身份并定角色：
    按钉钉 userId / 工号在花名册匹配员工；角色优先显式 > 花名册 Employee.role > 前缀推断。
    返回 (canonical_userid, role, name)。
    """
    from sqlalchemy import select

    from app.models import Employee

    uid = str(raw)
    emp = db.scalar(
        select(Employee).where((Employee.dd_user_id == uid) | (Employee.emp_id == uid))
    )
    canonical = emp.emp_id if emp else uid
    if explicit:
        role = explicit
    elif emp and emp.role:
        role = emp.role
    else:
        role = infer_role(canonical)
    return canonical, role, (emp.name if emp else None)


def get_from_headers(headers: dict) -> dict | None:
    user = headers.get("x-test-user") or headers.get("X-Test-User")
    role = headers.get("x-test-role") or headers.get("X-Test-Role")
    if user:
        return {"userid": user, "role": infer_role(str(user), role)}
    return None
