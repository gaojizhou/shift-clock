from decimal import Decimal
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import get_db
from app.errors import ConflictError, DomainError
from app.domain.shift_slots import SHIFT_SLOTS
from app.models import (
    AttendanceRecord,
    AuditLog,
    ChiefConfirm,
    ChiefWorker,
    DingtalkEmployeeRaw,
    Employee,
    EmployeeFavorite,
    Furnace,
    FurnaceChief,
    FurnaceType,
    MonthlyExportLock,
    NotificationRecord,
    Position,
    PostCatalog,
    QuotaRule,
    QuotaTemplate,
    QuotaTemplateScope,
    Region,
    Role,
    SelectablePost,
    ShiftWindow,
    MakeupRequest,
    ScanRecord,
    Squad,
    SquadSchedule,
)
from app.services.anomaly_evidence import get_anomaly_evidence
from app.services.anomaly_service import get_anomalies, set_confirm
from app.services.attendance_service import get_attendance
from app.services.audit_service import list_audit_logs
from app.services import proxy_signin_service as proxy_q
from app.services.furnace_lookup import get_furnace
from app.services.confirm_service import (
    MAX_DAILY_WORK_HOURS,
    assert_daily_work_hours_limit,
    daily_work_hours_used,
    get_confirm_sections,
    reassign_scan_post,
    set_work_hours,
    toggle_confirm_exclusion,
)
from app.services.connection_pool_service import get_connection_pool_status
from app.services.export_service import build_export_meta, build_export_xlsx, export_health_checks
from app.services.report_jobs import report_endpoint, report_jobs, prepare_report_audit, ReportLimitExceeded
from app.services.laoqin_stats import compute_laoqin_stats
from app.services.monthly_export_lock_service import (
    all_active_employee_codes,
    assert_month_export_unlocked,
    assert_month_range_unlocked,
    inherit_locked_months_for_region,
    locked_furnace_employee_codes,
    locked_furnace_ids_for_range,
    month_lock_status,
    set_month_locks,
    year_lock_statuses,
)
from app.services.quota_service import find_quota, resolve_quota_post
from app.services.request_audit_queue import request_audit_buffer
from app.services.scan_suggest_service import suggest_scan_defaults
from app.services.recon_service import get_recon_for, run_recon_for_date
from app.services.selectable_posts import get_selectable_posts
from app.scan_active import scan_is_active
from app.session_store import (
    DEMO_PUSH_DD,
    create_session,
    create_super_session,
    delete_session,
    get_from_headers,
    get_session_with_renewal,
    impersonate_session,
    infer_role,
    invalidate_sessions_for_employee,
    mark_super,
    resolve_identity,
    session_cookie_max_age,
)

router = APIRouter(prefix="/api/v1")

ROLE_HR = ("hr", "admin")
ROLE_STAT_HR = ("stat", "hr", "admin")
ROLE_FURNACES_READ = ("hr", "stat", "worker", "admin", "chief", "repairer", "leader")
# 异常工作台：HR/统计/管理员看全部；值班长可看但仅限本人绑定工作点
ROLE_ANOMALY = ("stat", "hr", "admin", "chief")
VALID_ROLES = ("worker", "chief", "hr", "stat", "admin", "repairer", "leader")
# 授予这些角色等同于超级权限，仅超管可操作（防止 HR 垂直越权提权他人）
PRIVILEGED_ROLES = ("admin",)
# HR 可维护普通业务角色，但不得改动或授予管理角色。
HR_PROTECTED_ROLES = ("admin", "hr", "stat")
# 管理角色：查看「可选岗」时不按角色过滤，看到全部可选岗（配置/对照用）。
SCAN_MANAGEMENT_ROLES = ("hr", "admin")


def effective_scan_role(user: dict, role: str | None) -> str | None:
    """selectable-posts 的有效过滤角色：显式 role 优先；管理角色→None(看全部)；其余按会话角色。"""
    if role:
        return role if role in VALID_ROLES else None
    if user.get("role") in SCAN_MANAGEMENT_ROLES:
        return None
    return user.get("role") or "worker"


def current_user(
    request: Request,
    db: Session = Depends(get_db),
    x_test_user: str | None = Header(None),
    x_test_role: str | None = Header(None),
):
    # 演示身份头（X-Test-User/Role）仅测试模式可用。生产必须忽略：否则任何人发
    # `X-Test-User: admin` 即可冒充任意角色（高危鉴权绕过/提权）。与 /auth/login、
    # /h5/test/auth 一致由 fdc_test_mode 门控；生产仅认钉钉免登写入的 fdc_session。
    if settings.fdc_test_mode:
        hdr = get_from_headers({"x-test-user": x_test_user, "x-test-role": x_test_role})
        if hdr:
            request.state.audit_identity = {**hdr, "loggedIn": True}
            request.state.audit_session_id = None
            return hdr
    cookie = request.cookies.get("fdc_session")
    if cookie:
        # 鉴权路径自身负责低频滑动续期：只在剩余 TTL 不足 1/4 时写一次，
        # 避免登录态寿命依赖 Redis/审计 worker，也避免每个请求都更新会话行。
        sess, renewed = get_session_with_renewal(cookie, db)
        if sess:
            if renewed:
                db.commit()
            request.state.audit_identity = {**sess, "loggedIn": True}
            request.state.audit_session_id = cookie
            return sess
    request.state.audit_identity = {"userid": "anonymous", "role": "", "loggedIn": False}
    request.state.audit_session_id = None
    return {"userid": "", "role": ""}


def require_user(user=Depends(current_user)):
    if not user.get("userid"):
        raise HTTPException(401, "login required")
    return user


def require_role(*roles: str):
    allowed = set(roles)

    def _dep(user=Depends(require_user)):
        # admin 为超级权限，放行所有受角色限制的接口
        if user.get("role") != "admin" and user.get("role") not in allowed:
            raise HTTPException(403, "forbidden")
        return user

    return _dep


def _current_region_scope(db: Session, user: dict) -> str | None:
    """返回 HR/统计的服务端地区范围；其他角色不限制。

    页面筛选只是交互便利，真正的数据范围必须由服务端从当前账号收藏重算，不能信任
    客户端传入的 region。测试模式允许未建 Employee 的虚拟身份继续用于旧单元测试；
    生产账号若未关联员工或有效地区则拒绝访问业务数据。
    """
    if user.get("role") not in ("hr", "stat"):
        return None
    uid = user.get("userid") or ""
    employee = db.scalar(
        select(Employee).where(
            or_(Employee.emp_id == uid, Employee.dd_user_id == uid),
            Employee.deleted.is_(False),
        )
    )
    if not employee:
        if settings.fdc_test_mode:
            return None
        raise HTTPException(403, "当前账号未关联员工地区")
    regions = db.scalars(
        select(EmployeeFavorite.value)
        .join(
            Region,
            and_(
                Region.name == EmployeeFavorite.value,
                Region.deleted.is_(False),
            ),
        )
        .where(
            EmployeeFavorite.emp_id == employee.emp_id,
            EmployeeFavorite.kind == "region",
        )
        .order_by(EmployeeFavorite.value)
    ).all()
    if not regions:
        raise HTTPException(403, "当前账号未配置有效地区")
    if len(regions) > 1:
        # 多地区账号不允许静默取首个，避免数据范围不可预期；由管理员规范为单地区后再使用。
        raise HTTPException(
            409,
            f"当前账号配置了多个地区（{'、'.join(regions)}），请联系管理员规范为单一地区",
        )
    return regions[0]


def _assert_employee_region_scope(
    db: Session, user: dict, employee: Employee, *, allow_unassigned: bool = True
) -> None:
    """HR/统计只能读写本地区员工；未归属员工可由本地区 HR 完成归属。"""
    scope = _current_region_scope(db, user)
    if not scope:
        return
    regions = set(_employee_favorites(db, employee.emp_id)["region"])
    if regions and scope not in regions:
        raise HTTPException(403, "无权访问其他地区员工")
    if not regions and not allow_unassigned:
        raise HTTPException(403, "员工尚未归属当前地区")


def _assert_furnace_region_scope(db: Session, user: dict, furnace_id: int) -> None:
    scope = _current_region_scope(db, user)
    furnace = db.get(Furnace, furnace_id)
    if scope and (not furnace or furnace.region != scope):
        raise HTTPException(403, "无权访问其他地区工作点")


def _assert_employee_day_region_scope(
    db: Session,
    user: dict,
    emp_id: str,
    furnace_id: int | None,
) -> None:
    """员工日地区口径：有工作点证据时按工作点，无工作点时回退员工明确地区。"""
    scope = _current_region_scope(db, user)
    if not scope:
        return
    if furnace_id is not None:
        furnace = db.get(Furnace, furnace_id)
        if not furnace or furnace.region != scope:
            raise HTTPException(403, "无权访问其他地区工作点")
        return
    employee = db.scalar(
        select(Employee).where(Employee.emp_id == emp_id, Employee.deleted.is_(False))
    )
    if employee is not None:
        regions = set(_employee_favorites(db, employee.emp_id)["region"])
        if regions and scope not in regions:
            raise HTTPException(403, "无权访问其他地区员工")


def _employee_codes_for_region(db: Session, region: str | None) -> list[str] | None:
    """服务端地区对应的员工工号；None 表示 admin 等全局账号不收窄。

    已配置地区收藏的员工直接归属；尚未配置收藏但在本地区工作点有有效/待审记录的员工
    也纳入，避免地区 HR 无法修复待归属考勤。空列表必须保留为空，不能退化成全公司。
    """
    if not region:
        return None
    favorite_codes = set(db.scalars(
        select(Employee.emp_id)
        .join(EmployeeFavorite, EmployeeFavorite.emp_id == Employee.emp_id)
        .where(
            Employee.deleted.is_(False),
            EmployeeFavorite.kind == "region",
            EmployeeFavorite.value == region,
        )
    ).all())
    furnace_codes = set(db.scalars(
        select(Employee.emp_id)
        .join(AttendanceRecord, AttendanceRecord.emp_id == Employee.emp_id)
        .join(Furnace, Furnace.id == AttendanceRecord.furnace_id)
        .where(
            Employee.deleted.is_(False),
            AttendanceRecord.status.in_(["active", "待审"]),
            Furnace.region == region,
            # 已明确归属其它地区的员工不能仅因临时跨工作点记录被纳入本地区；
            # 工作点归属兜底只服务尚无任何地区收藏的待归属员工。
            ~select(EmployeeFavorite.id).where(
                EmployeeFavorite.emp_id == Employee.emp_id,
                EmployeeFavorite.kind == "region",
            ).exists(),
        )
        .distinct()
    ).all())
    return sorted(favorite_codes | furnace_codes)


def _scope_export_body(db: Session, user: dict, body) -> list[str]:
    """把导出请求收窄到账号地区；客户端省略或篡改工作点参数都不会扩大范围。

    仅当 furnaceIds 与 furnaces 都未传时才回填整地区；只传工作点编号时把工作点编号解析成
    本地区对应的 furnaceIds，避免「只选 1 个工作点却按整地区导出」。
    """
    scope = _current_region_scope(db, user)
    if not scope:
        return []
    allowed = db.scalars(select(Furnace).where(Furnace.region == scope)).all()
    by_no = {row.furnace_no: row for row in allowed}
    allowed_ids = {row.id for row in allowed}
    allowed_nos = set(by_no)

    requested_ids = body.furnaceIds
    requested_nos = body.furnaces

    if not requested_ids and not requested_nos:
        body.furnaceIds = sorted(allowed_ids)
        body.furnaces = sorted(allowed_nos)
        return []

    if requested_ids:
        rejected_ids = sorted(set(requested_ids) - allowed_ids)
        rejected_rows = {
            row.id: row
            for row in db.scalars(
                select(Furnace).where(Furnace.id.in_(rejected_ids))
            ).all()
        }
        body.furnaceIds = [value for value in requested_ids if value in allowed_ids]
        selected = {row.id: row.furnace_no for row in allowed}
        body.furnaces = sorted({selected[value] for value in body.furnaceIds})
        return [
            (
                f"{rejected_rows[value].region} {rejected_rows[value].furnace_no}"
                if value in rejected_rows
                else f"工作点ID {value}"
            )
            for value in rejected_ids
        ]

    # 只传工作点编号：收窄到本地区，并把工作点编号解析成 furnaceIds。
    rejected_nos = sorted(set(requested_nos) - allowed_nos)
    body.furnaces = [value for value in requested_nos if value in allowed_nos]
    body.furnaceIds = [by_no[value].id for value in body.furnaces]
    return rejected_nos


def _export_scope_warning(rejected: list[str]) -> dict:
    return {
        "type": "export_scope_narrowed",
        "count": len(rejected),
        "furnaces": rejected,
        "message": f"已剔除无权访问的工作点：{'、'.join(rejected)}；请刷新选择后再导出",
    }


def require_super(user=Depends(require_user)):
    """身份切换权限：super 标记（密钥后门）或 admin 角色（钉钉管理员）。
    只认 super 标记不够——模拟成 worker/chief 后 role 会变，须保留 super 才能切回；
    admin 角色发起切换时会在 impersonate 时提升为 super 会话。"""
    if not (user.get("super") or user.get("role") == "admin"):
        raise HTTPException(403, "super admin only")
    return user


class LoginBody(BaseModel):
    userid: str
    role: str | None = None


@router.post("/auth/login")
def auth_login(body: LoginBody, db: Session = Depends(get_db)):
    # 演示/测试身份切换：仅测试模式开放。生产唯一登录入口是钉钉免登（/dingtalk/login）。
    if not settings.fdc_test_mode:
        raise HTTPException(404, "not found")
    # 归一到花名册身份；角色优先显式 > 花名册 > 前缀推断
    canonical, role, _name = resolve_identity(db, body.userid, body.role)
    session_id = create_session(canonical, role, db=db)
    db.commit()
    resp = JSONResponse({"userid": canonical, "role": role})
    resp.set_cookie(
        "fdc_session",
        session_id,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=session_cookie_max_age(),
    )
    return resp


@router.post("/auth/logout")
def auth_logout(request: Request, db: Session = Depends(get_db)):
    delete_session(request.cookies.get("fdc_session"), db=db)
    db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("fdc_session", path="/")
    return resp


class AdminLoginBody(BaseModel):
    code: str


@router.post("/auth/admin-login")
def auth_admin_login(body: AdminLoginBody, db: Session = Depends(get_db)):
    """超级管理员入口：两步验证 TOTP 动态码换取 super 会话。
    服务端未配置密钥 = 入口关闭（403）；动态码不对 = 401。"""
    from app.totp import verify_totp

    secret = settings.fdc_admin_totp_secret
    if not secret:
        raise HTTPException(403, "admin entry disabled")
    if not verify_totp(secret, body.code):
        raise HTTPException(401, "invalid code")
    session_id = create_super_session(db=db)
    db.add(AuditLog(actor="admin", action="admin-login", payload="super session created"))
    db.commit()
    resp = JSONResponse({"userid": "admin", "role": "admin", "super": True})
    resp.set_cookie(
        "fdc_session",
        session_id,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=session_cookie_max_age(),
    )
    return resp


@router.get("/admin/switch/employees")
def admin_switch_employees(
    q: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_super),
):
    """身份切换页：列出未删除员工供超管模拟。"""
    stmt = select(Employee).where(Employee.deleted == False)  # noqa: E712
    kw = (q or "").strip()
    if kw:
        like = f"%{kw}%"
        conds = [Employee.name.like(like), Employee.emp_id.like(like), Employee.role.like(like)]
        # 支持按中文角色名搜（人事→hr、值班长→chief 等），不只英文 role/工号/姓名
        # 支持新中文名与常见简称搜索。
        ROLE_CN = {
            "人事专员": "hr", "人事": "hr",
            "值班长": "chief",
            "操作工": "worker", "工人": "worker",
            "统计员": "stat", "统计": "stat",
            "系统管理员": "admin", "管理员": "admin",
            "维修工": "repairer", "维修": "repairer",
            "班组长": "leader",
        }
        for cn, en in ROLE_CN.items():
            if cn in kw:
                conds.append(Employee.role == en)
        stmt = stmt.where(or_(*conds))
    rows = db.scalars(stmt.order_by(Employee.emp_id).limit(500)).all()
    return {
        "items": [
            {
                "empId": e.emp_id,
                "name": e.name,
                "role": e.role or infer_role(e.dd_user_id or e.emp_id),
                "ddUserId": e.dd_user_id,
            }
            for e in rows
        ]
    }


class ImpersonateBody(BaseModel):
    empId: str


@router.post("/admin/switch/impersonate")
def admin_switch_impersonate(
    body: ImpersonateBody,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_super),
):
    """以某员工身份进入（等价该员工钉钉免登的会话）。保留 super 标记可切回。"""
    emp = db.scalar(
        select(Employee).where(Employee.emp_id == body.empId, Employee.deleted == False)  # noqa: E712
    )
    if not emp:
        raise HTTPException(404, "employee not found")
    role = emp.role or infer_role(emp.dd_user_id or emp.emp_id)
    sid = request.cookies.get("fdc_session")
    # 钉钉 admin（非密钥）发起切换：提升为 super 会话，保证模拟后仍能切回
    if sid and user.get("role") == "admin" and not user.get("super"):
        mark_super(sid, db=db)
    sess = impersonate_session(sid, emp.emp_id, role, db=db) if sid else None
    if not sess:
        raise HTTPException(403, "super admin only")
    db.add(AuditLog(actor="admin", action="impersonate", payload=f"{emp.emp_id}({role})"))
    db.commit()
    return {"userid": emp.emp_id, "name": emp.name, "role": role}


@router.post("/admin/switch/exit")
def admin_switch_exit(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_super),
):
    """切回超级管理员身份。"""
    sid = request.cookies.get("fdc_session")
    sess = impersonate_session(sid, "admin", "admin", db=db) if sid else None
    if not sess:
        raise HTTPException(403, "super admin only")
    db.add(AuditLog(actor="admin", action="exit-impersonate", payload="back to super"))
    db.commit()
    return {"userid": "admin", "role": "admin", "super": True}


@router.get("/dingtalk/config")
def dingtalk_config():
    """前端调试页用：corpId（免登需要）+ agentId + appKey + 是否已配置服务端凭据。
    appKey 是换 authCode 用的应用 ClientID（非密钥）；它必须与「打开 H5 的那个应用」一致，
    否则换码报 40078『不存在的临时授权码』。"""
    from app.services.dingtalk_service import is_configured

    return {
        "corpId": settings.dingtalk_corp_id,
        "agentId": settings.dingtalk_agent_id,
        "appKey": settings.dingtalk_app_key,
        "configured": is_configured(),
    }


@router.get("/dingtalk/diag")
async def dingtalk_diag():
    """诊断：服务端用哪个应用换码 + 该应用 gettoken 是否成功（验证凭据有效性）。
    appKey 与打开 H5 的应用不一致 → 换 authCode 必然 40078（与凭据是否有效无关）。"""
    from app.services.dingtalk_service import _get_access_token, is_configured

    out: dict = {
        "appKey": settings.dingtalk_app_key,
        "corpId": settings.dingtalk_corp_id,
        "agentId": settings.dingtalk_agent_id,
        "configured": is_configured(),
        "tokenOk": False,
    }
    if is_configured():
        try:
            tok = await _get_access_token()
            out["tokenOk"] = bool(tok)
            out["tokenPrefix"] = (tok or "")[:6]
        except Exception as e:  # noqa: BLE001
            out["tokenError"] = str(e)
    return out


class DingtalkLoginBody(BaseModel):
    authCode: str


@router.post("/dingtalk/login")
async def dingtalk_login(body: DingtalkLoginBody, db: Session = Depends(get_db)):
    """用钉钉免登 authCode 换取用户信息；拿到 userid 时顺便建立会话。

    会话身份归一到花名册（含登录别名）：角色取 Employee.role（值班长/工人），
    便于真实值班长用钉钉登录直接识别为值班长。
    """
    from app.services.dingtalk_service import login_by_code
    from app.services.dingtalk_employee_service import import_single_user

    try:
        info = await login_by_code(body.authCode)
    except Exception as e:  # 网络 / 凭据 / 钉钉返回异常
        raise HTTPException(502, f"dingtalk error: {e}")

    raw = str(info.get("userid") or "")
    if raw:
        canonical, _initial_role, _name = resolve_identity(db, raw)
        emp = db.scalar(select(Employee).where(Employee.emp_id == canonical, Employee.deleted.is_(False)))
        imported = False
        chief_bound = False
        if not emp:
            # 前端原先会再取一枚 authCode 调 /import-self。这里直接复用 login_by_code
            # 已取得的 user/get 结果完成首次导入；通常只剩 getuserinfo + user/get，
            # 部门缓存未命中时仍可能额外调用部门路径接口。
            detail = info.get("raw") if isinstance(info.get("raw"), dict) else {}
            user_detail = detail.get("result") or {}
            try:
                result = await run_in_threadpool(
                    import_single_user,
                    db,
                    raw,
                    actor="自助导入",
                    enable_push=True,
                    user_detail=user_detail,
                )
            except (ValueError, RuntimeError) as e:
                return JSONResponse(
                    {
                        **info,
                        "matched": False,
                        "ddUserid": raw,
                        "message": f"自动导入失败：{e}",
                    }
                )
            emp = db.scalar(select(Employee).where(Employee.emp_id == result["empId"]))
            if not emp:
                raise HTTPException(500, "导入后未找到员工档案")
            canonical = emp.emp_id
            imported = True
            chief_bound = bool(result.get("chiefBound"))
        # 首次导入前，钉钉 userid 还不在花名册，不能用它的前缀推断角色。
        # 导入后重新归一到工号，与 /dingtalk/import-self 的角色口径保持一致。
        canonical, inferred, _name = resolve_identity(db, raw)
        role = emp.role or inferred
        if imported:
            db.add(AuditLog(
                actor=emp.emp_id,
                action="dingtalk-import-self",
                payload=f"userid={raw} role={role} chiefBound={chief_bound}",
            ))
        session_id = create_session(emp.emp_id, role, db=db)
        db.commit()
        resp = JSONResponse(
            {
                **info,
                "matched": True,
                "imported": imported,
                "role": role,
                "resolvedUserid": emp.emp_id,
                "name": emp.name,
                "chiefBound": chief_bound,
            }
        )
        resp.set_cookie(
            "fdc_session",
            session_id,
            path="/",
            httponly=True,
            samesite="lax",
            max_age=session_cookie_max_age(),
        )
        return resp
    return info


class DingtalkImportSelfBody(BaseModel):
    authCode: str


@router.post("/dingtalk/import-self")
async def dingtalk_import_self(body: DingtalkImportSelfBody, db: Session = Depends(get_db)):
    """登录被判为「身份不匹配」时，用户自助导入自己：用一枚新的免登 authCode 校验身份，
    再用钉钉直接查询该 userid 的信息落地花名册（角色/常用岗位映射、值班长按部门绑定工作点），
    成功后建立会话直接登录。authCode 一次性，前端点击时需重新获取。"""
    from app.services.dingtalk_service import login_by_code
    from app.services.dingtalk_employee_service import import_single_user

    try:
        info = await login_by_code(body.authCode)
    except Exception as e:  # 网络 / 凭据 / 钉钉返回异常
        raise HTTPException(502, f"dingtalk error: {e}")

    raw = str(info.get("userid") or "")
    if not raw:
        raise HTTPException(400, "未能从钉钉换取 userid，无法导入（请在钉钉内重试）")
    try:
        # 自助导入：本人确认身份，默认开启钉钉通知（enable_push=True）
        detail = info.get("raw") if isinstance(info.get("raw"), dict) else {}
        user_detail = detail.get("result") or {}
        result = await run_in_threadpool(
            import_single_user,
            db,
            raw,
            actor="自助导入",
            enable_push=True,
            user_detail=user_detail,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))

    emp = db.scalar(select(Employee).where(Employee.emp_id == result["empId"]))
    if not emp:
        raise HTTPException(500, "导入后未找到员工档案")
    canonical, inferred, _name = resolve_identity(db, raw)
    role = emp.role or inferred
    db.add(AuditLog(actor=emp.emp_id, action="dingtalk-import-self",
                    payload=f"userid={raw} role={role} chiefBound={result.get('chiefBound')}"))
    db.commit()
    session_id = create_session(emp.emp_id, role, db=db)
    db.commit()
    resp = JSONResponse({
        **info, "matched": True, "imported": True, "role": role,
        "resolvedUserid": emp.emp_id, "name": emp.name,
        "chiefBound": result.get("chiefBound", False),
    })
    resp.set_cookie(
        "fdc_session",
        session_id,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=session_cookie_max_age(),
    )
    return resp


class DingtalkPushBody(BaseModel):
    ddUserId: str | None = None  # 直接指定钉钉 userid（优先级最高）
    chief: str | None = None  # 按工号查 dd_user_id
    text: str | None = None


@router.post("/dingtalk/push-test")
async def dingtalk_push_test(
    body: DingtalkPushBody, db: Session = Depends(get_db), user=Depends(require_user)
):
    """调试用：推送钉钉工作通知。目标优先级 ddUserId > chief(工号) > 当前登录人。

    返回逐步日志 + asyncsend 受理结果 + getsendresult 实际投递结果
    （asyncsend errcode=0 只代表受理；目标不在应用可见范围会出现在 invalid/forbidden 列表里）。
    """
    import asyncio

    from app.services.dingtalk_service import (
        get_send_result,
        interpret_send_result,
        send_work_message,
    )

    logs: list[str] = []
    if body.ddUserId and body.ddUserId.strip():
        dd = body.ddUserId.strip()
        logs.append(f"目标：直接指定钉钉ID {dd}")
    elif body.chief:
        emp = db.scalar(select(Employee).where(Employee.emp_id == body.chief))
        dd = emp.dd_user_id if emp else None
        if not dd:
            raise HTTPException(404, f"未找到工号 {body.chief} 的钉钉 userid")
        logs.append(f"目标：工号 {body.chief} → 钉钉ID {dd}")
    else:
        me = db.scalar(select(Employee).where(Employee.emp_id == user["userid"]))
        dd = (me.dd_user_id if me else None) or DEMO_PUSH_DD
        if not dd:
            raise HTTPException(400, "当前账号未绑定钉钉 userid，请通过 ddUserId/chief 指定推送目标")
        logs.append(f"目标：当前登录人 {user['userid']} → 钉钉ID {dd}")

    text = body.text or "【三班倒打卡】测试推送：您有待确认的当日出勤名单，请进入值班长日确认查看。"
    logs.append(f"内容：{text[:60]}")
    try:
        result = await send_work_message(dd, text)
        if result.get("skipped"):
            # 用户未开启钉钉通知 → 未发送，明确提示（不可误报成功）
            logs.append(f"⚠️ 已跳过：{result.get('reason') or '目标用户未开启钉钉通知'}")
            return {
                "ok": False, "skipped": True,
                "error": result.get("reason") or "目标用户未开启钉钉通知，未发送",
                "ddUserId": dd, "logs": logs,
            }
        logs.append(f"asyncsend_v2 受理成功：task_id={result.get('task_id')}")
    except Exception as e:
        logs.append(f"asyncsend_v2 失败：{e}")
        raise HTTPException(502, f"push failed: {e} ｜ 过程：{'；'.join(logs)}")

    send_result = None
    delivery = {"ok": True, "error": "", "forbidden": [], "invalidUsers": []}
    task_id = result.get("task_id")
    if task_id:
        # 异步投递：稍等后查实际结果，暴露 invalid/forbidden 名单并判定是否真正送达
        await asyncio.sleep(1.5)
        try:
            send_result = await get_send_result(task_id)
            delivery = interpret_send_result(send_result)
            if delivery["error"]:
                logs.append(f"⚠️ 投递失败：{delivery['error']}")
            else:
                logs.append("getsendresult：无失败名单，投递正常")
        except Exception as e:
            logs.append(f"getsendresult 查询失败（不影响发送）：{e}")

    # 受理成功但实际未送达（不在可见范围/被禁止）→ 作为错误返回，前台按错误展示
    if not delivery["ok"]:
        return {
            "ok": False,
            "to": dd,
            "error": delivery["error"] or "部分用户未送达",
            "forbidden": delivery["forbidden"],
            "invalidUsers": delivery["invalidUsers"],
            "logs": logs,
            "result": result,
            "sendResult": send_result,
        }
    return {"ok": True, "to": dd, "logs": logs, "result": result, "sendResult": send_result}


class ScanBody(BaseModel):
    # 加长度上限：超长入参在 Pydantic 层即 422，避免直达 DB 触发 varchar 截断 500
    userid: str | None = Field(None, max_length=32)
    empId: str | None = Field(None, max_length=32)
    region: str = Field(max_length=32)
    furnace: str = Field(max_length=32)
    squad: str | None = Field(None, max_length=16)
    post: str = Field(max_length=64)
    employType: str = Field(max_length=16)
    natualDate: str | None = Field(None, max_length=10)
    workHours: float | None = None  # 整班=8 / 半班=4（正式/实习/待岗均可）


@router.get("/health")
async def health(request: Request):
    # testMode 供 H5 决定是否渲染「演示/测试」身份切换面板
    scheduler = getattr(
        request.app.state,
        "scheduler_status",
        {"started": False, "jobIds": [], "error": "调度器尚未初始化"},
    )
    audit_health = await request_audit_buffer.public_diagnostics()
    return {
        "status": (
            "ok"
            if scheduler.get("started") and audit_health.get("status") == "healthy"
            else "degraded"
        ),
        "testMode": settings.fdc_test_mode,
        "scheduler": scheduler,
        # 未登录 H5 也会访问 /health，只暴露粗粒度状态；内部错误、流量计数、
        # Stream 名和积压明细放在管理员接口。
        "requestAuditQueue": audit_health,
    }


@router.get("/admin/connection-pool")
async def connection_pool_status(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """管理员实时查看当前 API 进程连接池与 PostgreSQL 会话压力。"""
    del user
    # psycopg2/SQLAlchemy 是同步 I/O；连接池告急时不得在 uvicorn 事件循环上等待借连接。
    result = await run_in_threadpool(get_connection_pool_status, db)
    audit_queue = await request_audit_buffer.diagnostics()
    result["requestAuditQueue"] = audit_queue
    result["reports"] = report_jobs.diagnostics()
    stream_health = audit_queue.get("streamHealth") or {}
    if audit_queue.get("enabled") and (
        not audit_queue.get("available") or stream_health.get("workerHealthy") is False
    ):
        if result["status"] == "healthy":
            result["status"] = "warning"
    return result


class DingtalkUserFetchLoadTestBody(BaseModel):
    userids: list[str] = Field(min_length=1, max_length=20)
    requestCount: int = Field(default=10, ge=1, le=100)
    concurrency: int = Field(default=2, ge=1, le=50)
    confirmHighLoad: bool = False


def _write_dingtalk_load_test_audit(
    db: Session,
    *,
    actor: str,
    phase: str,
    payload: dict,
) -> None:
    db.add(
        AuditLog(
            actor=actor,
            action=f"dingtalk-load-test-{phase}",
            payload=json.dumps(payload, ensure_ascii=False),
        )
    )
    db.commit()


@router.post("/admin/dingtalk-user-fetch-load-test")
async def dingtalk_user_fetch_load_test(
    body: DingtalkUserFetchLoadTestBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """管理员受控压测钉钉 user/get；带并发确认、冷却和完整审计。"""
    from app.services.dingtalk_load_test_service import (
        DingtalkLoadTestBusyError,
        DingtalkLoadTestCooldownError,
        SAFE_CONCURRENCY,
        run_user_fetch_load_test,
    )
    from app.services.dingtalk_service import is_configured

    if not is_configured():
        raise HTTPException(503, "钉钉应用凭据未配置，无法执行信息获取压测")
    userids = list(dict.fromkeys(value.strip() for value in body.userids if value.strip()))
    if not userids:
        raise HTTPException(400, "至少需要一个有效的钉钉 userid")
    if body.concurrency > body.requestCount:
        raise HTTPException(400, "并发数不能大于请求总数")
    if body.concurrency > SAFE_CONCURRENCY and not body.confirmHighLoad:
        raise HTTPException(
            400,
            f"并发超过 {SAFE_CONCURRENCY} 必须显式确认高压测试",
        )

    actor = str(user.get("userid") or "admin")
    audit_base = {
        "useridCount": len(userids),
        "requestCount": body.requestCount,
        "concurrency": body.concurrency,
        "highLoadConfirmed": body.confirmHighLoad,
    }
    await run_in_threadpool(
        _write_dingtalk_load_test_audit,
        db,
        actor=actor,
        phase="start",
        payload={**audit_base, "status": "started"},
    )
    try:
        result = await run_user_fetch_load_test(userids, body.requestCount, body.concurrency)
    except DingtalkLoadTestBusyError as exc:
        await run_in_threadpool(
            _write_dingtalk_load_test_audit,
            db,
            actor=actor,
            phase="end",
            payload={**audit_base, "status": "rejected", "error": str(exc)},
        )
        raise HTTPException(409, str(exc))
    except DingtalkLoadTestCooldownError as exc:
        await run_in_threadpool(
            _write_dingtalk_load_test_audit,
            db,
            actor=actor,
            phase="end",
            payload={**audit_base, "status": "rejected", "error": str(exc)},
        )
        raise HTTPException(
            429,
            str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except RuntimeError as exc:
        await run_in_threadpool(
            _write_dingtalk_load_test_audit,
            db,
            actor=actor,
            phase="end",
            payload={**audit_base, "status": "failed", "error": str(exc)},
        )
        raise HTTPException(502, f"钉钉压测初始化失败：{exc}")
    await run_in_threadpool(
        _write_dingtalk_load_test_audit,
        db,
        actor=actor,
        phase="end",
        payload={
            **audit_base,
            "status": "completed",
            "successCount": result.get("successCount"),
            "errorCount": result.get("errorCount"),
            "rateLimitedCount": result.get("rateLimitedCount"),
            "durationMs": result.get("durationMs"),
        },
    )
    return result


@router.get("/demo-context")
def demo_context():
    from app.services.anomaly_service import get_demo_context

    return get_demo_context()


@router.get("/quota")
def quota(
    region: str,
    furnace: str,
    post: str,
    squad: str | None = None,
    date: str | None = None,
    workHours: float | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    natual_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    q = find_quota(
        db,
        region=region,
        furnace=furnace,
        post=post,
        squad=squad,
        natual_date=natual_date,
        incoming_hours=workHours if workHours in (4.0, 8.0) else None,
    )
    if not q:
        raise HTTPException(404, "not found")
    return q


@router.get("/staffing/shortage")
def staffing_shortage(
    furnace: str,
    date: str,
    region: str | None = "示例厂区A",
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    from app.services.anomaly_service import resolve_demo_date
    from app.services.staffing_service import compute_shortage

    return {
        "furnace": furnace,
        "date": resolve_demo_date(date),
        "shortage": compute_shortage(
            db, furnace_no=furnace, date=resolve_demo_date(date), region=region
        ),
    }


@router.get("/laoqin/stats")
def laoqin_stats(
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    return compute_laoqin_stats(db)


@router.get("/recon")
def recon(
    empId: str,
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    # 非法日期返回 400，与 /recon/run、/export 边界口径一致
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"非法日期：{date!r}，应为 YYYY-MM-DD")
    return get_recon_for(db, empId, date)


@router.get("/recon/timeline")
def recon_timeline(
    date: str,
    region: str | None = None,
    furnace: str | None = None,
    squad: str | None = None,
    status: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    """考勤对照：某日全员 扫码 vs 钉钉打卡 时间点（双轨时间轴数据源）。"""
    from app.services.recon_timeline import get_recon_timeline

    scope_region = _current_region_scope(db, user)
    return get_recon_timeline(
        db,
        date=date,
        region=None if scope_region else region,
        scope_region=scope_region,
        furnace=furnace,
        squad=squad,
        status=status,
        q=q,
    )


def _already_on_content(db: Session, f_existing) -> dict:
    """重复上岗 409 响应体：含工作点编号/设备类型/值班长名，前端据此引导「如需加班请联系值班长操作」。"""
    from app.services.scan_detail import _chief_names

    ft = (
        db.get(FurnaceType, f_existing.furnace_type_code)
        if f_existing and f_existing.furnace_type_code
        else None
    )
    return {
        "error": "已上岗",
        "duplicate": True,
        "existingRegion": f_existing.region if f_existing else None,
        "existingFurnace": f_existing.furnace_no if f_existing else None,
        "existingFurnaceType": ft.name if ft else None,
        "existingChief": _chief_names(db, f_existing.id) if f_existing else None,
    }


@router.post("/scan", status_code=201)
def scan(body: ScanBody, db: Session = Depends(get_db), user=Depends(require_user)):
    # 身份以会话为准：工人只能给自己扫码；管理角色（HR/admin/值班长）可代他人扫
    requested = body.empId or body.userid
    if user.get("role") in ("hr", "admin", "chief") and requested:
        emp_id = requested
    else:
        emp_id = user["userid"]
    if not emp_id:
        raise HTTPException(400, "empId required")
    # 员工必须在花名册（杜绝幽灵工号入库 → 污染对账/算薪）
    if not db.scalar(select(Employee).where(Employee.emp_id == emp_id, Employee.deleted.is_(False))):
        raise HTTPException(404, "employee not found")

    f = db.scalar(
        select(Furnace).where(Furnace.region == body.region, Furnace.furnace_no == body.furnace)
    )
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")

    natual_date = body.natualDate or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        assert_month_export_unlocked(db, natual_date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    squad = body.squad
    post = resolve_quota_post(
        db,
        furnace_id=f.id,
        region=body.region,
        post=body.post,
        squad=squad,
    )

    # 不分班(squadless)岗位即便前端带了 squad 也归一为 None，避免落库 `维修工/早班` 这类
    # 幽灵记录（对 quota 过滤 squad IS NULL 隐身、又被 code_resolver 回填伪装合法）。
    if squad is not None:
        squadless_rule = db.scalar(
            select(QuotaRule.id).where(
                QuotaRule.furnace_id == f.id,
                QuotaRule.post == post,
                QuotaRule.squad.is_(None),
            )
        )
        if squadless_rule:
            squad = None

    daily_scan = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == natual_date,
            scan_is_active(),
        )
    )
    if daily_scan:
        f_existing = db.get(Furnace, daily_scan.furnace_id)
        return JSONResponse(status_code=409, content=_already_on_content(db, f_existing))

    # 出勤工时口径：半班=4h（两个半班合一编制位），未传按整班 8h；仅接受 {4.0, 8.0}
    work_hours = body.workHours if body.workHours is not None else 8.0
    if work_hours not in (4.0, 8.0):
        raise HTTPException(400, "工时只能为 4 小时（半班）或 8 小时（整班）")

    q = find_quota(
        db,
        region=body.region,
        furnace=body.furnace,
        post=post,
        squad=squad,
        natual_date=natual_date,
        incoming_hours=work_hours,
    )
    formal_allowed = True
    if q:
        formal_allowed = q["formalAllowed"]
        # 硬拦越编：正式岗按出勤工时已无空位 → 直接拒绝（前端已置灰，此为后端兜底，含直连 API）
        if body.employType == "正式" and not formal_allowed:
            raise HTTPException(409, "本班组正式岗已满编，无法越编上岗")
    elif body.employType == "正式":
        raise HTTPException(404, "该岗位在本班组未配置编制，正式工无法上岗，请联系管理员配置编制或改用实习/待岗")

    record = AttendanceRecord(
        emp_id=emp_id,
        furnace_id=f.id,
        squad=squad,
        post=post,
        employ_type=body.employType,
        scan_time=datetime.now(timezone.utc),
        source="扫码",
        natual_date=natual_date,
        work_hours=work_hours,
    )
    db.add(record)
    try:
        db.flush()  # 取得 record.id 供审计溯源（旧逻辑在 flush 前取 id 落库为 "None"）
        db.add(AuditLog(actor=emp_id, action="scan", payload=str(record.id)))
        run_recon_for_date(db, natual_date, commit=False, employee_ids={emp_id})
        db.commit()
    except IntegrityError:
        # 命中 (emp_id, natual_date) 有效唯一约束：并发/跨工作点重复上岗，按已上岗处理
        db.rollback()
        daily_scan = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.emp_id == emp_id,
                AttendanceRecord.natual_date == natual_date,
                scan_is_active(),
            )
        )
        f_existing = db.get(Furnace, daily_scan.furnace_id) if daily_scan else None
        return JSONResponse(status_code=409, content=_already_on_content(db, f_existing))

    # 扫码成功后给本工作点值班长推送「上岗提醒」：谁、何时、在哪个工作点、什么岗
    try:
        from app.services.on_duty_service import notify_on_duty

        notify_on_duty(
            db,
            furnace_no=body.furnace,
            date=natual_date,
            region=body.region,
            emp_id=emp_id,
            post=post,
            squad=squad,
            scan_time=record.scan_time,
            work_hours=work_hours,
        )
    except Exception:
        # 上岗提醒失败不影响扫码主流程
        db.rollback()

    # 越编已在落库前硬拦（见上）；这里正常返回
    return {"ok": True, "id": record.id, "formalAllowed": formal_allowed}


@router.post("/makeup/approve")
def makeup_approve(
    body: dict,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    # 必填字段缺失返回 400 而非 KeyError 500
    missing = [k for k in ("empId", "date", "furnace") if not body.get(k)]
    if missing:
        raise HTTPException(400, f"缺少必填字段：{', '.join(missing)}")
    emp_id = body["empId"]
    date = body["date"]
    region = body.get("region", "示例厂区A")
    furnace_no = body["furnace"]
    approver = user["userid"]
    # 代签是审批动作：值班长只能代签本辖区工作点(按 furnace_id)，工人无权(role 已挡)；hr/admin 放行
    _assert_chief_owns_furnace(db, user, furnace_no, region)
    f = db.scalar(
        select(Furnace).where(Furnace.region == region, Furnace.furnace_no == furnace_no)
    )
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    try:
        assert_month_export_unlocked(db, date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    # 本日已锁定则不许再代签（与撤回/标半天/调班一致，锁住算薪基准）
    locked = db.scalar(
        select(ChiefConfirm).where(ChiefConfirm.furnace_id == f.id, ChiefConfirm.natual_date == date)
    )
    if locked and locked.locked:
        raise HTTPException(409, "本日已锁定确认，不能再代签")

    # 同人同日的有效出勤（自己扫码 / 此前代签 / 已审补录）。DB 有 uq_attendance_active_emp_date
    # 部分唯一索引：每个 (emp_id, natual_date) 仅允许一条 active 出勤。代签须先据此判定，否则重复
    # 代签会盲插第二条 active 记录命中索引 → IntegrityError，被误报成「跨工作点重复出勤」。
    existing_scan = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == emp_id,
            AttendanceRecord.natual_date == date,
            scan_is_active(),
        )
    )
    if existing_scan and existing_scan.furnace_id != f.id:
        # 真·跨工作点重复：其他工作点已有有效出勤，拒绝以免算薪翻倍(P0)
        fx = db.get(Furnace, existing_scan.furnace_id)
        where = f"{fx.region} {fx.furnace_no}" if fx else "其他工作点"
        raise HTTPException(409, f"该员工当日已在{where}有有效出勤，不能跨工作点重复代签")
    if existing_scan:
        raise HTTPException(409, "该员工当日已有有效出勤，如需加班请使用「加班」标签")

    # 用工类型 / 出勤工时校验（提前到落库前，供正编硬拦使用）
    proxy_type = body.get("employType") or "正式"
    if proxy_type not in ("正式", "实习", "待岗"):
        raise HTTPException(400, "invalid employType")
    proxy_hours = body.get("workHours", 8.0)
    if proxy_hours not in (4.0, 8.0):
        raise HTTPException(400, "工时只能为 4 小时（半班）或 8 小时（整班）")
    proxy_squad = body.get("squad")
    proxy_post = resolve_quota_post(
        db, furnace_id=f.id, region=region, post=body["post"], squad=proxy_squad
    )

    # 正编硬拦：按「正式」入场时，既有占用 + 本次出勤不得越过编制（与扫码口径一致；
    # 前端已置灰，此为后端兜底，含直连 API）。
    if proxy_type == "正式":
        cap = find_quota(
            db,
            region=region,
            furnace=furnace_no,
            post=proxy_post,
            squad=proxy_squad,
            natual_date=date,
            incoming_hours=proxy_hours,
        )
        if cap is None:
            raise HTTPException(404, "该岗位在本班组未配置编制，正式工无法上岗，请联系管理员配置编制或改用实习/待岗")
        if not cap["formalAllowed"]:
            raise HTTPException(409, "本班组正式岗已满编，无法越编代签")

    # serialize concurrent approves for the same worker+month so the count→insert
    # window can't interleave and exceed the limit (no-op on SQLite)
    proxy_q.lock_emp_month(db, emp_id, proxy_q.month_of(date))
    quota = proxy_q.assert_can_proxy(db, emp_id, date)  # raises 409 if at limit

    # 并表后代签即一条 active 出勤记录（source=代签）。
    db.add(
        AttendanceRecord(
            emp_id=emp_id,
            furnace_id=f.id,
            squad=proxy_squad,
            post=proxy_post,
            employ_type=proxy_type,
            scan_time=datetime.fromisoformat(f"{date}T12:00:00+08:00"),
            source="代签",
            status="active",
            natual_date=date,
            work_hours=proxy_hours,
            reviewed_by=approver,
            reviewed_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        AuditLog(
            actor=approver,
            action="makeup_proxy_signin",
            payload=json.dumps(
                {
                    "empId": emp_id,
                    "furnace": furnace_no,
                    "region": region,
                    "date": date,
                    "post": proxy_post,
                    "squad": proxy_squad,
                    "used": quota["used"] + 1,
                    "limit": quota["limit"],
                },
                ensure_ascii=False,
            ),
        )
    )
    try:
        db.flush()
        run_recon_for_date(db, date, commit=False, employee_ids={emp_id})
        db.commit()
    except IntegrityError:
        # 同人同日已有有效出勤（含其他工作点扫码/代签）：拒绝跨工作点重复出勤，防算薪翻倍(P0)
        db.rollback()
        raise HTTPException(409, "该员工当日已有有效出勤，不能跨工作点重复代签")
    recon = get_recon_for(db, emp_id, date)
    return {"ok": True, "reconStatus": recon["status"], "proxyQuota": proxy_q.quota_for(db, emp_id, date)}


class MakeupCreate(BaseModel):
    userid: str
    date: str
    furnace: str
    region: str | None = "示例厂区A"
    squad: str | None = None
    post: str
    employType: str = "正式"
    reason: str = ""
    workHours: float = 8.0  # 4=半班，8=整班（正式/实习/待岗均可）


@router.get("/attendance")
def attendance_get(
    userid: str,
    month: str,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    try:
        return get_attendance(db, userid=userid, month=month)
    except ReportLimitExceeded as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/makeup")
def makeup_create(
    body: MakeupCreate,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    # 工人只能为自己发起补录；管理角色(chief/hr/admin)可代他人发起
    if user.get("role") not in ("chief", "hr", "admin") and body.userid != user["userid"]:
        raise HTTPException(403, "只能为本人发起补录")
    f = get_furnace(db, body.furnace, body.region)
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    # HR/统计只能为本地区工作点发起补录，避免向他地区工作台注入待审记录
    _assert_furnace_region_scope(db, user, f.id)
    try:
        assert_month_export_unlocked(db, body.date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    # 补录必须工作点有值班长（否则无人可审）
    has_chief = db.scalar(
        select(FurnaceChief.id).where(
            FurnaceChief.furnace_id == f.id, FurnaceChief.deleted.is_(False)
        )
    )
    if not has_chief:
        raise HTTPException(400, f"{f.furnace_no} 未绑定值班长，无法补录，请联系 HR 先指派值班长")
    if body.workHours not in (4.0, 8.0):
        raise HTTPException(400, "工时只能为 4 小时（半班）或 8 小时（整班）")
    hours = body.workHours
    # 幂等去重：同人同日同工作点已有待审补录则直接返回它，避免重复堆积污染审核队列
    existing = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == body.userid,
            AttendanceRecord.natual_date == body.date,
            AttendanceRecord.furnace_id == f.id,
            AttendanceRecord.status == "待审",
        )
    )
    if existing:
        return {"id": existing.id, "status": "待审", "deduped": True}
    m = AttendanceRecord(
        emp_id=body.userid,
        furnace_id=f.id,
        squad=body.squad,
        post=body.post,
        employ_type=body.employType,
        natual_date=body.date,
        reason=body.reason,
        source="补录",
        status="待审",
        work_hours=hours,
    )
    db.add(m)
    db.flush()
    run_recon_for_date(db, body.date, commit=False, employee_ids={body.userid})
    db.commit()
    db.refresh(m)
    return {"id": m.id, "status": "待审"}


class MakeupReview(BaseModel):
    userid: str
    date: str
    furnace: str
    region: str | None = "示例厂区A"
    action: str


@router.post("/makeup/review")
def makeup_review(
    body: MakeupReview,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    from app.services.anomaly_service import resolve_demo_date

    # 补录审核是审批动作：值班长只能审本辖区工作点，工人无权(role 已挡)；hr/admin 放行
    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    natual_date = resolve_demo_date(body.date)
    f = get_furnace(db, body.furnace, body.region)
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    try:
        assert_month_export_unlocked(db, natual_date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    # 并表后补录即一条记录：审批直接翻其 status（通过→active 生成有效出勤；驳回→拒绝）。
    m = db.scalar(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == body.userid,
            AttendanceRecord.natual_date == natual_date,
            AttendanceRecord.furnace_id == f.id,
            AttendanceRecord.source == "补录",
            AttendanceRecord.status == "待审",
        )
    )
    approver = user["userid"]
    new_status = "active" if body.action == "approve" else "拒绝"
    if m:
        m.status = new_status
        m.reviewed_by = approver
        m.reviewed_at = datetime.now(timezone.utc)
        if body.action == "approve":
            m.scan_time = datetime.fromisoformat(f"{natual_date}T12:00:00+08:00")
    else:
        # 尚无工人发起的补录单时，记录值班长的处置决定。状态随动作（驳回=拒绝）；
        # approve 直接落一条 active 有效出勤。
        db.add(
            AttendanceRecord(
                emp_id=body.userid,
                furnace_id=f.id,
                natual_date=natual_date,
                source="补录",
                status=new_status,
                post="出料工",
                employ_type="正式",
                work_hours=8.0,
                reviewed_by=approver,
                reviewed_at=datetime.now(timezone.utc),
                scan_time=(
                    datetime.fromisoformat(f"{natual_date}T12:00:00+08:00")
                    if body.action == "approve"
                    else None
                ),
            )
        )
    db.add(
        AuditLog(
            actor=approver,
            action="makeup_review",
            payload=json.dumps(
                {
                    "empId": body.userid,
                    "furnace": body.furnace,
                    "region": body.region,
                    "date": natual_date,
                    "action": body.action,
                },
                ensure_ascii=False,
            ),
        )
    )
    try:
        db.flush()
        run_recon_for_date(db, natual_date, commit=False, employee_ids={body.userid})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该员工当日已有有效出勤，不能跨工作点重复补录")
    return {"status": "通过" if body.action == "approve" else "拒绝", "approver": approver}


class ExportBody(BaseModel):
    month: str
    furnaces: list[str] = []
    furnaceIds: list[int] | None = None  # 优先用 id 定位工作点，避免跨地区同名工作点编号污染
    format: str | None = None
    empId: str | None = None
    payroll2GroupBy: list[str] | None = None
    payroll2IncludeZeroRows: bool = False
    payroll2MergeOvertimeOnlyRows: bool = False
    payroll2MergeSameSquadOvertimeOnlyRows: bool = False


class ExportSheetBody(ExportBody):
    sheet: str


class MonthlyExportLockBody(BaseModel):
    month: str
    furnaceIds: list[int]
    locked: bool


class AbsenceOverrideBody(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    empId: str = Field(min_length=1, max_length=64)
    days: Decimal | None = Field(ge=0, le=31, max_digits=4, decimal_places=2)
    revision: int = Field(ge=0)


@router.get("/export/request-status")
def export_request_status(user=Depends(require_role(*ROLE_STAT_HR))):
    # 状态读取不占报表计算名额，即使计算已满也可查看占用者。
    return report_jobs.activity()


@router.get("/export/absence-overrides")
@report_endpoint
def export_absence_overrides_get(
    month: str, furnaceIds: str,
    db: Session = Depends(get_db), user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.absence_override_service import list_absence_overrides
    try:
        requested = [int(value) for value in furnaceIds.split(",") if value]
        if not requested:
            return {"items": {}}
        visible = _month_lock_scope_furnace_ids(db, user, requested)
        return list_absence_overrides(db, month, visible, _month_lock_scope_furnace_ids(db, user))
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError:
        raise HTTPException(400, "工作点参数格式错误")


@router.put("/export/absence-overrides")
@report_endpoint(read_only=False)
def export_absence_overrides_put(
    body: AbsenceOverrideBody,
    db: Session = Depends(get_db), user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.absence_override_service import save_absence_override
    try:
        return save_absence_override(db, month=body.month, emp_id=body.empId, days=body.days,
            revision=body.revision, actor=user["userid"],
            allowed_furnaces=_month_lock_scope_furnace_ids(db, user))
    except DomainError as e:
        db.rollback()
        raise HTTPException(e.status_code, str(e))


class ExportButtonVisibilityBody(BaseModel):
    buttons: dict[str, list[str]]


@router.get("/export/button-visibility")
def export_button_visibility_get(
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services import export_button_visibility_service as visibility

    return visibility.get_config(db)


@router.put("/admin/export/button-visibility")
def export_button_visibility_put(
    body: ExportButtonVisibilityBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services import export_button_visibility_service as visibility

    try:
        result = visibility.set_config(db, body.buttons)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.add(
        AuditLog(
            actor=user["userid"],
            action="export_button_visibility",
            payload=json.dumps(result["buttons"], ensure_ascii=False),
        )
    )
    db.commit()
    return result


def _month_lock_scope_furnace_ids(
    db: Session,
    user: dict,
    requested: list[int] | None = None,
) -> list[int]:
    """月锁可见/可操作工作点：HR/统计按地区，值班长按所辖工作点，Admin 为全部。"""
    stmt = select(Furnace.id)
    role = user.get("role")
    if role == "chief":
        stmt = stmt.where(
            Furnace.id.in_(
                select(FurnaceChief.furnace_id).where(
                    FurnaceChief.emp_id == user["userid"],
                    FurnaceChief.deleted.is_(False),
                )
            )
        )
    else:
        scope = _current_region_scope(db, user)
        if scope:
            stmt = stmt.where(Furnace.region == scope)
    allowed = list(db.scalars(stmt.order_by(Furnace.id)).all())
    if requested is None:
        return allowed
    ids = list(dict.fromkeys(requested))
    existing = set(db.scalars(select(Furnace.id).where(Furnace.id.in_(ids))).all()) if ids else set()
    missing = [fid for fid in ids if fid not in existing]
    if missing:
        raise HTTPException(404, f"工作点不存在：{','.join(str(fid) for fid in missing)}")
    denied = [fid for fid in ids if fid not in set(allowed)]
    if denied:
        raise HTTPException(403, "无权操作其他地区或非本人管辖工作点的月锁")
    return ids


def _lock_narrowed_codes(
    db: Session,
    user: dict,
    date_from: str,
    date_to: str,
    base_codes: list[str] | None,
) -> tuple[list[str] | None, list[int]]:
    """工作点级月锁收窄：从基础工号集合中剔除已封账工作点对应员工。

    整批拒绝会让同地区未封账工作点的拉取/转换一并停摆（分批封账无法推进）；
    收窄后只保护已封账工作点的数据。base_codes=None（admin 全局）以全部在职员工
    为基础；返回 (收窄后的 codes, 已封账工作点 id)，codes=None 表示无需收窄。

    冻结口径必须用**全局**已封账工作点，不能用 `_month_lock_scope_furnace_ids`
    （那是月锁可见/可操作范围）。地区 HR 的 scope 不含外区工作点，若用 scope
    当冻结范围，跨地区员工的外区已封账月份会被重新拉取/重建。
    """
    locked_ids = locked_furnace_ids_for_range(db, date_from, date_to, None)
    if not locked_ids:
        return base_codes, []
    locked_codes = locked_furnace_employee_codes(db, date_from, date_to, locked_ids)
    if not locked_codes:
        return base_codes, locked_ids
    base = all_active_employee_codes(db) if base_codes is None else set(base_codes)
    return sorted(base - locked_codes), locked_ids


@router.get("/export/month-lock")
def export_month_lock_get(
    month: str,
    furnaceId: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    try:
        furnace_ids = _month_lock_scope_furnace_ids(
            db,
            user,
            [furnaceId] if furnaceId is not None else None,
        )
        return month_lock_status(db, month, furnace_ids)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


@router.get("/export/month-locks")
def export_month_locks_get(
    year: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    try:
        return year_lock_statuses(db, year, _month_lock_scope_furnace_ids(db, user))
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


@router.put("/export/month-lock")
def export_month_lock_put(
    body: MonthlyExportLockBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    try:
        furnace_ids = _month_lock_scope_furnace_ids(db, user, body.furnaceIds)
        set_month_locks(
            db,
            month=body.month,
            furnace_ids=furnace_ids,
            locked=body.locked,
            actor=user["userid"],
        )
        return month_lock_status(
            db,
            body.month,
            _month_lock_scope_furnace_ids(db, user),
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


def _report_scope_key(db: Session, user: dict):
    # 收藏地区或值班长辖区变化后，不合并到旧范围的计算。
    if user.get("role") == "chief":
        return sorted(_chief_furnace_ids(db, user["userid"]))
    return _current_region_scope(db, user)


@router.post("/export")
@report_endpoint(scope_key=_report_scope_key)
def export_data(
    body: ExportBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.export_service import ExportInputError

    rejected = _scope_export_body(db, user, body)
    if rejected and body.format != "meta":
        raise HTTPException(400, _export_scope_warning(rejected)["message"])
    try:
        meta = build_export_meta(
            db,
            month=body.month,
            furnaces=body.furnaces,
            emp_id=body.empId,
            furnace_ids=body.furnaceIds,
            persist=body.format != "meta",
            use_absence_overrides=body.format != "meta",
            payroll2_group_by=body.payroll2GroupBy,
            payroll2_include_zero_rows=body.payroll2IncludeZeroRows,
            payroll2_merge_overtime_only_rows=body.payroll2MergeOvertimeOnlyRows,
            payroll2_merge_same_squad_overtime_only_rows=body.payroll2MergeSameSquadOvertimeOnlyRows,
        )
    except ExportInputError as e:
        raise HTTPException(400, str(e))
    if body.format == "meta":
        if rejected:
            meta.setdefault("warnings", []).append(_export_scope_warning(rejected))
        # 计算过程不写对账表；结束只读事务，释放连接。
        db.rollback()
        return meta
    prepare_report_audit(db)
    data = build_export_xlsx(meta)
    # 审计：薪资导出是对外交付动作，记录操作人/月份/工作点/行数供追溯
    db.add(
        AuditLog(
            actor=user["userid"],
            action="export",
            payload=json.dumps(
                {
                    "month": body.month,
                    "furnaces": body.furnaces,
                    "furnaceIds": body.furnaceIds,
                    "empId": body.empId,
                    "rowCount": meta["payrollSheet"].get("rowCount"),
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/export/sheet")
@report_endpoint(scope_key=_report_scope_key)
def export_sheet(
    body: ExportSheetBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.export_service import ExportInputError, build_export_sheet_xlsx

    rejected = _scope_export_body(db, user, body)
    if rejected:
        raise HTTPException(400, _export_scope_warning(rejected)["message"])
    try:
        include_monthly_hours = body.sheet == "班次有效工时"
        include_overtime_detail = body.sheet in {"加班明细", "加班汇总", "按人汇总"}
        meta = build_export_meta(
            db,
            month=body.month,
            furnaces=body.furnaces,
            emp_id=body.empId,
            furnace_ids=body.furnaceIds,
            persist=True,
            include_monthly_hours=include_monthly_hours,
            include_overtime_detail=include_overtime_detail,
            payroll2_group_by=body.payroll2GroupBy,
            payroll2_include_zero_rows=body.payroll2IncludeZeroRows,
            payroll2_merge_overtime_only_rows=body.payroll2MergeOvertimeOnlyRows,
            payroll2_merge_same_squad_overtime_only_rows=body.payroll2MergeSameSquadOvertimeOnlyRows,
        )
        prepare_report_audit(db)
        data = build_export_sheet_xlsx(meta, body.sheet)
    except ExportInputError as e:
        raise HTTPException(400, str(e))
    db.add(
        AuditLog(
            actor=user["userid"],
            action="export_sheet",
            payload=json.dumps(
                {
                    "month": body.month,
                    "furnaces": body.furnaces,
                    "furnaceIds": body.furnaceIds,
                    "sheet": body.sheet,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/export/sheet-preview")
@report_endpoint(scope_key=_report_scope_key)
def export_sheet_preview(
    body: ExportSheetBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.export_service import ExportInputError

    rejected = _scope_export_body(db, user, body)
    try:
        meta = build_export_meta(
            db,
            month=body.month,
            furnaces=body.furnaces,
            emp_id=body.empId,
            furnace_ids=body.furnaceIds,
            persist=False,
            use_absence_overrides=False,
            include_monthly_hours=False,
            include_overtime_detail=False,
            payroll2_group_by=body.payroll2GroupBy,
            payroll2_include_zero_rows=body.payroll2IncludeZeroRows,
            payroll2_merge_overtime_only_rows=body.payroll2MergeOvertimeOnlyRows,
            payroll2_merge_same_squad_overtime_only_rows=body.payroll2MergeSameSquadOvertimeOnlyRows,
        )
    except ExportInputError as e:
        raise HTTPException(400, str(e))
    db.rollback()
    if rejected:
        meta.setdefault("warnings", []).append(_export_scope_warning(rejected))
    if body.sheet == "输出给薪资":
        return {
            "sheet": body.sheet,
            "furnaceCount": meta["furnaceCount"],
            "warnings": meta.get("warnings", []),
            "payrollSheet": meta["payrollSheet"],
        }
    if body.sheet == "输出给薪资2":
        return {
            "sheet": body.sheet,
            "furnaceCount": meta["furnaceCount"],
            "warnings": meta.get("warnings", []),
            "payrollSheet": meta["payrollSheet2"],
        }
    if body.sheet in meta.get("extraSheets", {}):
        return {
            "sheet": body.sheet,
            "furnaceCount": meta["furnaceCount"],
            "warnings": meta.get("warnings", []),
            "extraSheet": meta["extraSheets"][body.sheet],
        }
    raise HTTPException(400, f"未知工作表：{body.sheet}")


def _resolve_export_restrict(db: Session, user: dict, furnace_ids: str | None) -> set[int] | None:
    """导出工作点范围：值班长恒限本人工作点；HR/统计/管理员传 furnaceIds 则限定，否则全量(None)。"""
    if user.get("role") == "chief":
        return _chief_furnace_ids(db, user["userid"])
    scope = _current_region_scope(db, user)
    if scope:
        allowed = set(
            db.scalars(select(Furnace.id).where(Furnace.region == scope)).all()
        )
        if furnace_ids:
            requested = {int(x) for x in furnace_ids.split(",") if x.strip().isdigit()}
            return requested & allowed
        return allowed
    if furnace_ids:
        ids = {int(x) for x in furnace_ids.split(",") if x.strip().isdigit()}
        return ids
    return None


@router.get("/export/monthly-hours")
@report_endpoint(scope_key=_report_scope_key)
def export_monthly_hours(
    month: str,
    format: str = "calendar",
    furnaceIds: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    """劳勤工作台月度导出：选定月份所有「班次有效」出勤工时，直接下载 xlsx。

    furnaceIds：逗号分隔的工作点 id，限定导出范围（HR/统计/管理员可用；值班长恒限本人工作点）。
    format=calendar（默认）：考勤表式日历——一人(工作点×岗位)一行，列含 地区/工作点编号/姓名/工号/岗位
        + 1~月末每日有效工时 + 全月 + 加班。
    format=flat：旧式合并汇总——按 工号+工时类型+地区+工作点编号+岗位 合并一行（总工时累加）。
    format=meta：返回 calendar 口径的 JSON 预览（columns/rows），只读不落库，不下载文件。"""
    from app.services.export_service import (
        ExportInputError,
        build_monthly_calendar,
        build_monthly_calendar_xlsx,
        build_monthly_hours_xlsx,
        build_monthly_valid_hours,
    )

    restrict = _resolve_export_restrict(db, user, furnaceIds)
    try:
        if format == "meta":
            meta = build_monthly_calendar(
                db, month=month, restrict_furnace_ids=restrict, persist=False,
                use_absence_overrides=False,
            )
        elif format == "flat":
            meta = build_monthly_valid_hours(db, month=month, restrict_furnace_ids=restrict)
            prepare_report_audit(db)
            data = build_monthly_hours_xlsx(meta)
        else:
            meta = build_monthly_calendar(db, month=month, restrict_furnace_ids=restrict)
            prepare_report_audit(db)
            data = build_monthly_calendar_xlsx(meta)
    except ExportInputError as e:
        raise HTTPException(400, str(e))
    if format == "meta":
        # 计算过程不写对账表；结束只读事务，释放连接。
        db.rollback()
        return meta
    db.add(
        AuditLog(
            actor=user["userid"],
            action="export_monthly_hours",
            payload=json.dumps(
                {"month": month, "format": format, "rowCount": len(meta["rows"])},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/export/overtime-detail")
@report_endpoint(scope_key=_report_scope_key)
def export_overtime_detail(
    month: str,
    format: str | None = None,
    furnaceIds: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    """月度「加班明细表」导出：选定月份所有有效加班逐条（出勤延时>8h + 独立加班记录）+
    「加班汇总」工作表（工作点编号×加班岗位×班次 求和）。对齐客户 Excel 加班明细分页。

    furnaceIds：逗号分隔工作点 id，限定范围（HR/统计/管理员可用；值班长恒限本人工作点）。
    format=meta：返回 JSON 预览（明细/汇总/按人汇总），只读不落库，不下载文件。"""
    from app.services.export_service import (
        ExportInputError,
        build_overtime_detail,
        build_overtime_detail_xlsx,
    )

    restrict = _resolve_export_restrict(db, user, furnaceIds)
    try:
        meta = build_overtime_detail(
            db, month=month, restrict_furnace_ids=restrict, persist=format != "meta"
        )
    except ExportInputError as e:
        raise HTTPException(400, str(e))
    if format == "meta":
        # 计算过程不写对账表；结束只读事务，释放连接。
        db.rollback()
        return meta
    prepare_report_audit(db)
    data = build_overtime_detail_xlsx(meta)
    db.add(
        AuditLog(
            actor=user["userid"],
            action="export_overtime_detail",
            payload=json.dumps({"month": month, "rowCount": len(meta["rows"])}, ensure_ascii=False),
        )
    )
    db.commit()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/export/closure-check")
def closure_check():
    return {"closable": []}


@router.get("/export/health-check")
@report_endpoint(scope_key=_report_scope_key)
def export_health_check(
    month: str,
    furnaces: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    furnace_list = [x.strip() for x in furnaces.split(",") if x.strip()]
    scope = _current_region_scope(db, user)
    out_of_scope: list[str] = []
    if scope:
        allowed = set(
            db.scalars(select(Furnace.furnace_no).where(Furnace.region == scope)).all()
        )
        out_of_scope = [value for value in furnace_list if value not in allowed]
        furnace_list = [value for value in furnace_list if value in allowed]
    status, checks = export_health_checks(
        db, month=month, furnace_nos=furnace_list, region=scope
    )
    if out_of_scope:
        checks = [
            *checks,
            *[
                {"furnace": value, "type": "out_of_scope", "message": f"{value} 不属于当前账号地区"}
                for value in out_of_scope
            ],
        ]
        status = "warn"
    if not furnace_list and not out_of_scope:
        checks = [{"type": "no_furnace", "message": "未选择可检查的工作点"}]
        status = "warn"
    return {"month": month, "checks": checks, "status": status}


class QuotaRuleUpdate(BaseModel):
    squad: str | None = None
    post: str
    capacity: int = Field(ge=0, le=9999)  # 编制非负且设上限，防超大值打穿 int 列(500)


class QuotaUpdateBody(BaseModel):
    rules: list[QuotaRuleUpdate]


class SelectablePostsBody(BaseModel):
    region: str
    furnaceNo: str | None = None
    posts: list[str]


@router.get("/furnaces/selectable-posts")
def furnaces_selectable_posts(
    region: str,
    furnace: str,
    role: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_FURNACES_READ)),
):
    scope = _current_region_scope(db, user)
    if scope and region != scope:
        raise HTTPException(403, "无权访问其他地区工作点")
    # 按可见角色过滤：扫码取当前会话角色；管理角色看全部；代签等可显式传 role
    from app.services.selectable_posts import get_visible_posts

    eff = effective_scan_role(user, role)
    posts = get_selectable_posts(db, region=region, furnace=furnace, role=eff)
    # visible：不过滤的 {post, roles} 明细，供后台编辑回填角色多选
    visible = get_visible_posts(db, region=region, furnace=furnace)
    return {"region": region, "furnace": furnace, "posts": posts, "visible": visible}


@router.put("/furnaces/selectable-posts")
def furnaces_selectable_posts_put(
    body: SelectablePostsBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    scope = _current_region_scope(db, user)
    if scope and body.region != scope:
        raise HTTPException(403, "无权操作其他地区工作点")
    existing = db.scalars(
        select(SelectablePost).where(
            SelectablePost.region == body.region,
            SelectablePost.furnace_no == body.furnaceNo,
        )
    ).all()
    for row in existing:
        db.delete(row)
    for post in body.posts:
        db.add(
            SelectablePost(region=body.region, furnace_no=body.furnaceNo, post=post)
        )
    db.commit()
    return {"ok": True, "posts": body.posts}


@router.put("/furnaces/{furnace_id}/quota")
def furnaces_quota_put(
    furnace_id: int,
    body: QuotaUpdateBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    f = db.get(Furnace, furnace_id)
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    _assert_furnace_region_scope(db, user, furnace_id)
    for item in body.rules:
        q_stmt = select(QuotaRule).where(
            QuotaRule.furnace_id == furnace_id, QuotaRule.post == item.post
        )
        if item.squad:
            q_stmt = q_stmt.where(QuotaRule.squad == item.squad)
        else:
            q_stmt = q_stmt.where(QuotaRule.squad.is_(None))
        rule = db.scalar(q_stmt)
        if not rule:
            raise HTTPException(404, f"quota rule not found: {item.post}/{item.squad}")
        rule.capacity = item.capacity
    # 审计：编制(配额)直接影响满编判定与算薪，记录改动
    db.add(
        AuditLog(
            actor=user["userid"],
            action="furnace_quota_put",
            payload=json.dumps(
                {
                    "furnaceId": furnace_id,
                    "rules": [
                        {"post": r.post, "squad": r.squad, "capacity": r.capacity}
                        for r in body.rules
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {"ok": True, "furnaceId": furnace_id}


class QuotaTemplateItem(BaseModel):
    post: str
    # 与 QuotaRuleUpdate 一致：编制非负且设上限，防负数/超大值打穿 int 列
    capacity: int = Field(ge=0, le=9999)
    capacity2: int | None = Field(None, ge=0, le=9999)
    capacity3: int | None = Field(None, ge=0, le=9999)
    squadless: bool = False
    visibleRoles: list[str] | None = None  # 可见角色 role.code 列表；None=按缺省（分班→worker、不分班→chief）


class QuotaTemplatePut(BaseModel):
    furnaceType: str
    region: str | None = None
    items: list[QuotaTemplateItem]


class QuotaTemplateApply(BaseModel):
    furnaceType: str
    furnaceNos: list[str] | None = None
    region: str | None = None


def _quota_template_region(db: Session, user: dict, requested: str | None) -> str | None:
    """HR/统计恒用账号地区；管理员必须显式选择有效地区。"""
    scope = _current_region_scope(db, user)
    if user.get("role") != "admin":
        # 测试模式的虚拟 HR 没有 Employee；允许测试显式提供地区，生产不会走此分支。
        return scope if scope is not None else (requested if settings.fdc_test_mode else None)
    region = (requested or "").strip()
    if not region:
        raise HTTPException(400, "管理员必须选择地区")
    exists = db.scalar(
        select(Region.code).where(Region.name == region, Region.deleted.is_(False))
    )
    if exists is None:
        raise HTTPException(400, "地区不在有效字典中")
    return region


@router.get("/admin/quota-templates")
def quota_templates_get(
    furnaceType: str,
    region: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.quota_template_service import list_templates

    scope = _quota_template_region(db, user, region)
    return {
        "furnaceType": furnaceType,
        "region": scope,
        "items": list_templates(db, furnace_type=furnaceType, region=scope),
    }


@router.put("/admin/quota-templates")
def quota_templates_put(
    body: QuotaTemplatePut,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.quota_template_service import upsert_templates

    _require_furnace_type(db, body.furnaceType)  # 模板设备类型必须出自字典
    items = [it.model_dump() for it in body.items]
    scope = _quota_template_region(db, user, body.region)
    result = upsert_templates(
        db,
        furnace_type=body.furnaceType,
        items=items,
        # HR/统计地区由服务端账号范围决定；管理员必须显式选择且已校验字典。
        region=scope,
        actor=user["userid"],
    )
    return {"furnaceType": body.furnaceType, "region": scope, "items": result}


@router.post("/admin/quota-templates/apply")
def quota_templates_apply(
    body: QuotaTemplateApply,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    from app.services.quota_template_service import apply_to_furnaces

    try:
        return apply_to_furnaces(
            db,
            furnace_type=body.furnaceType,
            furnace_nos=body.furnaceNos,
            region=_quota_template_region(db, user, body.region),
            actor=user["userid"],
        )
    except ValueError:
        raise HTTPException(404, "template not found")


class VisiblePost(BaseModel):
    post: str
    roles: list[str] = []  # role.code 列表；空=无人可扫码选该岗


class FurnaceOverwriteBody(BaseModel):
    rules: list[QuotaRuleUpdate]
    visiblePosts: list[VisiblePost] = []


@router.put("/furnaces/{furnace_id}/quota-overwrite")
def furnace_quota_overwrite_put(
    furnace_id: int,
    body: FurnaceOverwriteBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    """覆盖式重写单个工作点编制（「编辑当前工作点编制」弹框用，支持分班三班不同编制 + 可见角色）。"""
    from app.services.quota_template_service import overwrite_furnace_quota

    _assert_furnace_region_scope(db, user, furnace_id)
    try:
        rules = overwrite_furnace_quota(
            db,
            furnace_id=furnace_id,
            rules=[r.model_dump() for r in body.rules],
            visible_posts=[vp.model_dump() for vp in body.visiblePosts],
            actor=user["userid"],
        )
    except ValueError:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    return {"ok": True, "furnaceId": furnace_id, "quotaRules": rules}


def _post_catalog_items(db: Session) -> list[str]:
    rows = db.scalars(
        select(PostCatalog).where(PostCatalog.deleted.is_(False)).order_by(PostCatalog.name)
    ).all()
    return [r.name for r in rows]


@router.get("/post-catalog")
def post_catalog_get(
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    """岗位字典：编制编辑岗位下拉的可选项（仅未删除项）。"""
    return {"items": _post_catalog_items(db)}


@router.get("/roles")
def roles_get(db: Session = Depends(get_db), user=Depends(require_user)):
    """角色字典（基础字典）：code→name；供员工管理等页统一显示角色名，避免漏映射。"""
    rows = db.scalars(
        select(Role).where(Role.deleted.is_(False)).order_by(Role.sort, Role.code)
    ).all()
    return {"items": [{"code": r.code, "name": r.name, "sort": r.sort} for r in rows]}


class PostCatalogAdd(BaseModel):
    name: str


@router.post("/post-catalog")
def post_catalog_add(
    body: PostCatalogAdd,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    # 先查同名（含已标记删除的），同名则取消删除标记，避免重复
    existing = db.scalar(select(PostCatalog).where(PostCatalog.name == name))
    if existing is None:
        db.add(PostCatalog(name=name, deleted=False))
    elif existing.deleted:
        existing.deleted = False
    db.commit()
    return {"items": _post_catalog_items(db)}


@router.delete("/post-catalog/{name}")
def post_catalog_delete(
    name: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    row = db.scalar(select(PostCatalog).where(PostCatalog.name == name.strip()))
    if row:
        row.deleted = True  # 标记删除
        db.commit()
    return {"items": _post_catalog_items(db)}


@router.get("/furnace-types")
def furnace_types_list(db: Session = Depends(get_db), user=Depends(require_user)):
    """设备类型字典（只读）：工作点编制页筛选/模板卡片的数据源。
    与 /admin/dict/furnace_type（仅 admin 可维护）分离，HR 等只读使用。"""
    rows = db.scalars(
        select(FurnaceType).where(FurnaceType.deleted.is_(False)).order_by(FurnaceType.sort, FurnaceType.code)
    ).all()
    return {"items": [{"code": t.code, "name": t.name} for t in rows]}


def _require_furnace_type(db: Session, name: str) -> FurnaceType:
    """校验设备类型必须在字典中（未删除），返回字典行（业务表只存其 code）。"""
    t = db.scalar(
        select(FurnaceType).where(FurnaceType.name == name, FurnaceType.deleted.is_(False))
    )
    if not t:
        raise HTTPException(400, f"设备类型「{name}」不在字典中，请先在基础字典维护")
    return t


class FurnaceConfigBody(BaseModel):
    chiefIds: list[str] | None = None  # None=不改；[]=清空全部值班长
    furnaceType: str | None = None  # 设备类型名称，必须出自字典
    region: str | None = None  # 地区名称（改地区，须出自地区字典）
    furnaceNo: str | None = None  # 工作点编号（可改名）


@router.put("/furnaces/{furnace_id}/config")
def furnace_config_update(
    furnace_id: int,
    body: FurnaceConfigBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    """工作点配置：绑定/修改值班长（多值班长，整组替换）与设备类型。"""
    f = db.get(Furnace, furnace_id)
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    _assert_furnace_region_scope(db, user, furnace_id)
    changed: dict = {}
    old_region, old_no = f.region, f.furnace_no  # 改地区/工作点编号后联动 (region,furnace_no) 键的表
    target_region = f.region
    target_no = f.furnace_no
    if body.region is not None and body.region.strip():
        requested_region = body.region.strip()
        scope = _current_region_scope(db, user)
        if scope and requested_region != scope:
            raise HTTPException(403, "无权把工作点迁移到其他地区")
        reg = db.scalar(select(Region).where(Region.name == requested_region))
        if not reg:
            raise HTTPException(400, f"地区不存在: {body.region}")
        target_region = reg.name
    else:
        reg = None
    if body.furnaceNo is not None and body.furnaceNo.strip():
        target_no = body.furnaceNo.strip()

    # 地区或工作点编号任一发生变化时，都必须在写入前校验目标复合键，避免唯一约束
    # 以 500 暴露并把当前事务置为回滚态。
    if (target_region, target_no) != (old_region, old_no):
        dup = db.scalar(
            select(Furnace).where(
                Furnace.region == target_region,
                Furnace.furnace_no == target_no,
                Furnace.id != f.id,
            )
        )
        if dup:
            raise HTTPException(400, f"该地区已有工作点编号 {target_no}")
    if body.furnaceType is not None:
        ft = _require_furnace_type(db, body.furnaceType.strip())
        f.furnace_type_code = ft.code
        changed["furnaceType"] = ft.name
    # 改地区：须出自地区字典，同步 region 名与 region_code
    if reg is not None:
        f.region = reg.name
        f.region_code = reg.code
        changed["region"] = reg.name
    # 改工作点编号：同地区内工作点编号唯一
    if body.furnaceNo is not None and body.furnaceNo.strip():
        f.furnace_no = target_no
        changed["furnaceNo"] = target_no
    # 地区/工作点编号变更联动：SelectablePost 以 (region, furnace_no) 为键(非 furnace_id)，
    # 不联动会让本工作点「可选岗位」配置失联。其余按工作点(QuotaRule)/记录(furnace_id)关联，不受影响。
    if (f.region != old_region or f.furnace_no != old_no):
        db.query(SelectablePost).filter(
            SelectablePost.region == old_region, SelectablePost.furnace_no == old_no
        ).update(
            {SelectablePost.region: f.region, SelectablePost.furnace_no: f.furnace_no},
            synchronize_session=False,
        )
    if f.region != old_region:
        inherit_locked_months_for_region(
            db, f.id, f.region, actor=f"system:migrate-furnace:{user['userid']}"
        )
    if body.chiefIds is not None:
        ids = list(dict.fromkeys(x.strip() for x in body.chiefIds if x.strip()))
        if ids:
            found = set(
                db.scalars(
                    select(Employee.emp_id).where(
                        Employee.emp_id.in_(ids), Employee.deleted.is_(False)
                    )
                ).all()
            )
            missing = [x for x in ids if x not in found]
            if missing:
                raise HTTPException(400, f"员工不存在: {', '.join(missing)}")
        old_ids = [
            fc.emp_id
            for fc in db.scalars(
                select(FurnaceChief).where(FurnaceChief.furnace_id == f.id)
            ).all()
        ]
        for fc in db.scalars(
            select(FurnaceChief).where(FurnaceChief.furnace_id == f.id)
        ).all():
            db.delete(fc)
        # 先 flush 删除再插入：否则新旧值班长集合有交集时，SQLAlchemy 可能把 INSERT 排在
        # DELETE 之前，撞 (furnace_id,emp_id) 唯一约束 500，导致 HR 无法增量管理多值班长。
        db.flush()
        for eid in ids:
            db.add(
                FurnaceChief(
                    furnace_id=f.id,
                    emp_id=eid,
                    created_by=user["userid"],
                    updated_by=user["userid"],
                )
            )
        f.chief_user_id = ids[0] if ids else None
        db.flush()
        # 角色联动：新绑定的工人升为值班长；解绑后不再担任任何工作点值班长的降回工人。
        # 会话缓存了登录时的角色，角色实际变化时清掉旧会话，下次免登按新角色建会话。
        for eid in ids:
            e = db.scalar(select(Employee).where(Employee.emp_id == eid))
            if e and e.role in (None, "", "worker"):
                e.role = "chief"
                invalidate_sessions_for_employee(db, e.emp_id, e.dd_user_id)
        for eid in set(old_ids) - set(ids):
            still = db.scalar(select(FurnaceChief).where(FurnaceChief.emp_id == eid))
            e = db.scalar(select(Employee).where(Employee.emp_id == eid))
            if not still and e and e.role == "chief":
                e.role = "worker"
                invalidate_sessions_for_employee(db, e.emp_id, e.dd_user_id)
        changed["chiefs"] = {"old": old_ids, "new": ids}
    db.add(
        AuditLog(
            actor=user["userid"],
            action="furnace_config",
            payload=json.dumps(
                {"furnaceId": f.id, "code": f.code, "furnace": f.furnace_no, **changed},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {"ok": True}


@router.get("/furnaces")
def furnaces_list(
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_FURNACES_READ)),
):
    stmt = select(Furnace)
    scope = _current_region_scope(db, user)
    if scope:
        stmt = stmt.where(Furnace.region == scope)
    rows = db.scalars(
        stmt.order_by(Furnace.region, Furnace.furnace_no_order, Furnace.furnace_no)
    ).all()
    furnace_ids = {f.id for f in rows}
    chiefs = list(db.scalars(select(FurnaceChief).where(
        FurnaceChief.furnace_id.in_(furnace_ids)
    )))
    chief_ids = {fc.emp_id for fc in chiefs} | {f.chief_user_id for f in rows if f.chief_user_id}
    # 仅读取返回工作点的值班长；编制批量读取，避免每个工作点单独查询。
    chief_names = {e.emp_id: e.name for e in db.scalars(
        select(Employee).where(Employee.emp_id.in_(chief_ids))
    )}
    rules_by_furnace: dict[int, list[QuotaRule]] = {}
    for rule in db.scalars(select(QuotaRule).where(
        QuotaRule.furnace_id.in_(furnace_ids)
    ).order_by(QuotaRule.id)):
        rules_by_furnace.setdefault(rule.furnace_id, []).append(rule)
    # 设备类型名称一律 join 字典（业务表只存 code）
    ftype_names = {t.code: t.name for t in db.scalars(select(FurnaceType)).all()}
    # 一工作点多值班长：furnace_id → [emp_id]
    fc_map: dict[int, list[str]] = {}
    for fc in chiefs:
        fc_map.setdefault(fc.furnace_id, []).append(fc.emp_id)

    # 按地区+设备类型展开期望编制集合 {(squad, post): capacity}，判断工作点是否被改动过。
    SQUADS = ("1班", "2班", "3班")
    tpl_expected: dict[
        tuple[str | None, str], dict[tuple[str | None, str], int]
    ] = {}
    for t in db.scalars(select(QuotaTemplate)).all():
        exp = tpl_expected.setdefault((t.region, t.furnace_type_code), {})
        if t.squadless:
            exp[(None, t.post)] = t.capacity
        else:
            caps = {
                "1班": t.capacity,
                "2班": t.capacity2 if t.capacity2 is not None else t.capacity,
                "3班": t.capacity3 if t.capacity3 is not None else t.capacity,
            }
            for squad in SQUADS:
                exp[(squad, t.post)] = caps[squad]

    items = []
    for f in rows:
        rules = rules_by_furnace.get(f.id, [])
        actual = {(r.squad, r.post): r.capacity for r in rules}
        # 地区模板优先；尚未完成存量迁移或新增地区时，显式回退全局模板。
        # key 含 region，避免不同地区同设备类型模板互相覆盖 customized 判定。
        expected = tpl_expected.get((f.region, f.furnace_type_code))
        if expected is None:
            expected = tpl_expected.get((None, f.furnace_type_code))
        has_template = expected is not None
        # customized 仅按「编制(quota)与模板的差异」判定——可靠信号。原先还比较可选岗集合，
        # 但 expected_sel(模板 visible_roles) 与 actual_sel(quota规则回退,排除squadless) 走两条
        # 不同口径且受跨地区同名工作点 SelectablePost 影响，恒不相等→几乎所有工作点误报「已改动」。
        customized = bool(has_template and actual != expected)
        items.append(
            {
                "id": f.id,
                "code": f.code,
                "region": f.region,
                "regionCode": f.region_code,
                "furnaceNo": f.furnace_no,
                "furnaceNoOrder": f.furnace_no_order,
                "furnaceType": ftype_names.get(f.furnace_type_code, ""),
                "furnaceTypeCode": f.furnace_type_code,
                "name": f.name,
                "chiefUserId": (fc_map.get(f.id) or [f.chief_user_id])[0] if (fc_map.get(f.id) or f.chief_user_id) else None,
                "chiefName": chief_names.get((fc_map.get(f.id) or [f.chief_user_id])[0]) if (fc_map.get(f.id) or f.chief_user_id) else None,
                "chiefs": [
                    {"empId": eid, "name": chief_names.get(eid)} for eid in fc_map.get(f.id, [])
                ],
                "hasTemplate": has_template,
                "customized": customized,
                "quotaRules": [
                    {
                        "squad": r.squad,
                        "post": r.post,
                        "capacity": r.capacity,
                        "squadless": r.squad is None,
                    }
                    for r in rules
                ],
            }
        )
    return {"items": items}


@router.get("/confirm")
def confirm_get(
    furnace: str,
    date: str,
    region: str | None = "示例厂区A",
    db: Session = Depends(get_db),
    user=Depends(current_user),
):
    from app.services.anomaly_service import resolve_demo_date
    from app.services.employee_visibility_service import show_departed_to_chief

    return get_confirm_sections(
        db,
        furnace_no=furnace,
        date=resolve_demo_date(date),
        region=region,
        include_departed_candidates=(
            user.get("role") != "chief" or show_departed_to_chief(db)
        ),
    )


class ConfirmSubmit(BaseModel):
    furnace: str
    region: str | None = "示例厂区A"
    date: str
    locked: bool = True  # True=锁定/定版(默认)，False=解锁(纠错重开)


@router.post("/confirm/submit")
def confirm_submit(
    body: ConfirmSubmit,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    from app.services.anomaly_service import resolve_demo_date

    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    f = get_furnace(db, body.furnace, body.region)
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    natual_date = resolve_demo_date(body.date)
    try:
        assert_month_export_unlocked(db, natual_date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    existing = db.scalar(
        select(ChiefConfirm).where(
            ChiefConfirm.furnace_id == f.id, ChiefConfirm.natual_date == natual_date
        )
    )
    confirmed_by = user["userid"]
    if existing:
        existing.locked = body.locked
        existing.confirmed_by = confirmed_by
        existing.confirmed_at = datetime.now(timezone.utc)
        existing.natual_date = natual_date
    else:
        db.add(
            ChiefConfirm(
                furnace_id=f.id,
                natual_date=natual_date,
                locked=body.locked,
                confirmed_by=confirmed_by,
                confirmed_at=datetime.now(timezone.utc),
            )
        )
    db.add(
        AuditLog(
            actor=confirmed_by,
            action="confirm_submit" if body.locked else "confirm_unlock",
            payload=json.dumps(
                {"furnace": body.furnace, "region": body.region, "date": natual_date, "locked": body.locked},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    return {"locked": body.locked, "confirmedBy": confirmed_by}


def _chief_furnace_nos(db: Session, emp_id: str) -> set[str]:
    """值班长本人绑定的工作点编号集合（异常工作台值班长访问时限定可见范围）。"""
    rows = db.scalars(
        select(FurnaceChief.furnace_id).where(
            FurnaceChief.emp_id == emp_id, FurnaceChief.deleted.is_(False)
        )
    ).all()
    out: set[str] = set()
    for fid in rows:
        f = db.get(Furnace, fid)
        if f:
            out.add(f.furnace_no)
    return out


def _chief_furnace_ids(db: Session, emp_id: str) -> set[int]:
    """值班长本人绑定的 furnace_id 集合（全局唯一，用于跨地区精确归属判定）。
    工作点编号在多地区可同名，确认归属必须按 id 比对，否则值班长可越权确认他地区同名工作点员工。"""
    return set(
        db.scalars(
            select(FurnaceChief.furnace_id).where(
                FurnaceChief.emp_id == emp_id, FurnaceChief.deleted.is_(False)
            )
        ).all()
    )


def _chief_bound_regions(db: Session, userid: str) -> list[str]:
    """值班长绑定工作点所在地区集合（与 /me 的工作点回显口径一致：先 FurnaceChief，回退 chief_user_id）。
    无绑定返回空列表——调用方据此把可见数据收窄到「仅全局」而不是放开为全部。"""
    regions: set[str] = set()
    furnace_ids = _chief_furnace_ids(db, userid)
    if furnace_ids:
        regions.update(
            db.scalars(
                select(Furnace.region).where(
                    Furnace.id.in_(furnace_ids), Furnace.region.is_not(None)
                )
            ).all()
        )
    else:
        f = db.scalar(select(Furnace).where(Furnace.chief_user_id == userid))
        if f and f.region:
            regions.add(f.region)
    return sorted(r for r in regions if r)


def _assert_chief_owns_furnace(db: Session, user: dict, furnace_no: str, region: str | None) -> None:
    """值班长日结操作的工作点归属校验：值班长只能操作本辖区工作点，按 furnace_id(全局唯一)比对，
    避免跨地区同名工作点编号越权篡改其他工作点出勤。hr/stat 只能操作本地区工作点；admin 放行。"""
    if user.get("role") == "chief":
        f = get_furnace(db, furnace_no, region)
        if not f or f.id not in _chief_furnace_ids(db, user["userid"]):
            raise HTTPException(403, "该工作点不在您的管辖范围，无法操作")
        return
    scope = _current_region_scope(db, user)
    if scope:
        f = get_furnace(db, furnace_no, region)
        if not f or f.region != scope:
            raise HTTPException(403, "无权访问其他地区工作点")


@router.get("/anomalies")
@report_endpoint(scope_key=_report_scope_key)
def anomalies(
    date: str | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    furnace: str | None = None,
    region: str | None = None,
    name: str | None = None,
    chiefName: str | None = None,
    status: str | None = None,
    dataSource: str | None = None,
    hrStatus: str | None = None,
    chiefStatus: str | None = None,
    scanData: str | None = None,
    laoqin: str | None = None,
    validKind: str | None = None,
    page: int = 1,
    pageSize: int = 50,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    if not date and not dateFrom and not dateTo:
        raise HTTPException(400, "劳勤工作台必须传入日期范围")
    filters = {
        "furnace": furnace,
        "region": _current_region_scope(db, user) or region,
        "name": name,
        "chiefName": chiefName,
        "status": status,
        "dataSource": dataSource,
        "hrStatus": hrStatus,
        "chiefStatus": chiefStatus,
        "scanData": scanData,
        "laoqin": laoqin,
        "validKind": validKind,
    }
    # 值班长只看本人工作点（按 furnace_id 限定，跨地区同名工作点编号不误入）；HR/统计/管理员看全部
    restrict = (
        _chief_furnace_ids(db, user["userid"]) if user.get("role") == "chief" else None
    )
    return get_anomalies(
        db,
        date,
        date_from=dateFrom,
        date_to=dateTo,
        filters=filters,
        restrict_furnace_ids=restrict,
        page=page,
        page_size=pageSize,
    )


class ConfirmExcludeBody(BaseModel):
    furnace: str
    region: str | None = "示例厂区A"
    date: str
    empId: str
    excluded: bool = True
    reason: str | None = None


@router.post("/confirm/exclude")
def confirm_exclude(
    body: ConfirmExcludeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    try:
        return toggle_confirm_exclusion(
            db,
            furnace_no=body.furnace,
            region=body.region,
            date=body.date,
            emp_id=body.empId,
            excluded=body.excluded,
            reason=body.reason,
            actor=user["userid"],
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


class ConfirmReassignBody(BaseModel):
    furnace: str
    region: str | None = "示例厂区A"
    date: str
    empId: str
    post: str
    squad: str | None = None


@router.post("/confirm/reassign")
def confirm_reassign(
    body: ConfirmReassignBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    try:
        return reassign_scan_post(
            db,
            furnace_no=body.furnace,
            region=body.region,
            date=body.date,
            emp_id=body.empId,
            post=body.post,
            squad=body.squad,
            actor=user["userid"],
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


class ConfirmHoursBody(BaseModel):
    furnace: str
    region: str | None = "示例厂区A"
    date: str
    empId: str
    workHours: float  # 0~24；0=取消打卡，4=半班，8=整班，>8=班次延时
    recordId: int | None = None  # 指定时只改该单条记录（编辑/取消某条加班）


@router.post("/confirm/hours")
def confirm_hours(
    body: ConfirmHoursBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    try:
        return set_work_hours(
            db,
            furnace_no=body.furnace,
            region=body.region,
            date=body.date,
            emp_id=body.empId,
            work_hours=body.workHours,
            record_id=body.recordId,
            actor=user["userid"],
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))


class OvertimeBody(BaseModel):
    empId: str
    date: str
    furnace: str
    region: str | None = "示例厂区A"
    squad: str | None = None  # 占编班组；为空只允许不分班岗
    shiftSlot: str
    post: str
    employType: str = "正式"
    workHours: float = 8.0  # 0~24


@router.post("/attendance/overtime", status_code=201)
def add_overtime(
    body: OvertimeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    """模式2「跨岗位加班」：为当天已有记录的工人，在另一岗位（同工作点或跨工作点）再上岗。

    创建 record_kind='加班' 记录。去重维度=班次+岗位：同一班次内可上不同岗位（各占一条
    active、各占编制），仅完全相同的「班次+岗位」算重复被拒。正式加班占编制、满编硬拦。
    不分班岗（squad=None）的加班请改走日结「改时长」。工人无权发起（role 已挡）。
    """
    from app.services.anomaly_service import resolve_demo_date

    _assert_chief_owns_furnace(db, user, body.furnace, body.region)
    natual_date = resolve_demo_date(body.date)
    if body.workHours <= 0:
        raise HTTPException(400, "workHours must be > 0")
    if body.workHours > 24:
        raise HTTPException(400, "加班时长不能超过 24h")
    if body.employType not in ("正式", "实习", "待岗"):
        raise HTTPException(400, "invalid employType")
    if body.shiftSlot not in SHIFT_SLOTS:
        raise HTTPException(400, "invalid shiftSlot")
    if not db.scalar(
        select(Employee).where(Employee.emp_id == body.empId, Employee.deleted.is_(False))
    ):
        raise HTTPException(404, "employee not found")
    f = db.scalar(
        select(Furnace).where(Furnace.region == body.region, Furnace.furnace_no == body.furnace)
    )
    if not f:
        raise HTTPException(404, "未找到该工作点（可能已停用或二维码失效）")
    try:
        assert_month_export_unlocked(db, natual_date, f.id)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    locked = db.scalar(
        select(ChiefConfirm).where(
            ChiefConfirm.furnace_id == f.id, ChiefConfirm.natual_date == natual_date
        )
    )
    if locked and locked.locked:
        raise HTTPException(409, "本日已锁定确认，不能再添加加班")

    # 必须已有当天记录（active 出勤/加班 或 待审补录）——加班是在已有出勤基础上跨岗补人
    existing = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == body.empId,
            AttendanceRecord.natual_date == natual_date,
            AttendanceRecord.status.in_(["active", "待审"]),
        )
    ).all()
    if not existing:
        raise HTTPException(400, "该员工当天无任何记录，加班需在已有出勤基础上添加，请用代签")
    try:
        assert_daily_work_hours_limit(
            db,
            emp_id=body.empId,
            date=natual_date,
            incoming_hours=body.workHours,
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))

    squad = body.squad
    post = resolve_quota_post(
        db,
        furnace_id=f.id,
        region=body.region,
        post=body.post,
        squad=squad,
    )
    # 不分班(squadless)岗归一为 None
    if squad is not None:
        squadless_rule = db.scalar(
            select(QuotaRule.id).where(
                QuotaRule.furnace_id == f.id, QuotaRule.post == post, QuotaRule.squad.is_(None)
            )
        )
        if squadless_rule:
            squad = None

    # 时段+岗位不可重复：同一班组/岗位可在不同时段加班；同一时段同岗重复拒绝。
    occupied = {
        (s.shift_slot or "__base__", s.post)
        for s in existing
        if s.status == "active" and s.record_kind == "加班"
    }
    if (body.shiftSlot, post) in occupied:
        raise HTTPException(
            409, "该员工当天该时段该岗位已有加班"
        )

    # 越编硬拦：正式加班占编制，满编则拒绝（工时按 8h 封顶判断入场）
    if body.employType == "正式":
        cap = find_quota(
            db,
            region=body.region,
            furnace=body.furnace,
            post=post,
            squad=squad,
            natual_date=natual_date,
            incoming_hours=min(body.workHours, 8.0),
        )
        if cap is None:
            raise HTTPException(404, "该岗位在本班组未配置编制，正式工无法上岗，请联系管理员配置编制或改用实习/待岗")
        if not cap["formalAllowed"]:
            raise HTTPException(409, "本班组正式岗已满编，无法越编加班")

    rec = AttendanceRecord(
        emp_id=body.empId,
        furnace_id=f.id,
        squad=squad,
        shift_slot=body.shiftSlot,
        post=post,
        employ_type=body.employType,
        region_code=f.region_code,
        scan_time=datetime.fromisoformat(f"{natual_date}T12:00:00+08:00"),
        source="加班",
        status="active",
        record_kind="加班",
        natual_date=natual_date,
        work_hours=body.workHours,
        recon_dingtalk=True,
        reviewed_by=user["userid"],
        reviewed_at=datetime.now(timezone.utc),
    )
    db.add(rec)
    db.add(
        AuditLog(
            actor=user["userid"],
            action="overtime_add",
            payload=json.dumps(
                {
                    "empId": body.empId,
                    "furnace": body.furnace,
                    "region": body.region,
                    "date": natual_date,
                    "post": post,
                    "squad": squad,
                    "shiftSlot": body.shiftSlot,
                    "workHours": body.workHours,
                },
                ensure_ascii=False,
            ),
        )
    )
    try:
        db.flush()
        run_recon_for_date(db, natual_date, commit=False, employee_ids={body.empId})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该员工当天该时段该岗位已有加班")
    return {"ok": True, "id": rec.id, "workHours": body.workHours}


@router.get("/attendance/worker-day")
def worker_day(
    empId: str,
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    """某工人某日考勤概况：供「添加工人·加班」搜索任意工人后判断是否已有记录、已占哪些班次。"""
    from app.services.anomaly_service import resolve_demo_date

    nd = resolve_demo_date(date)
    emp = db.scalar(select(Employee).where(Employee.emp_id == empId, Employee.deleted.is_(False)))
    if not emp:
        raise HTTPException(404, "employee not found")
    rows = db.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.emp_id == empId,
            AttendanceRecord.natual_date == nd,
            AttendanceRecord.status.in_(["active", "待审"]),
        )
    ).all()
    squads = sorted({(r.squad or "不分班") for r in rows if r.status == "active"})
    base = next((r for r in rows if r.status == "active" and r.record_kind != "加班"), None)
    slots = sorted({r.shift_slot for r in rows if r.status == "active" and r.record_kind == "加班" and r.shift_slot})
    used_hours = daily_work_hours_used(db, emp_id=empId, date=nd)
    return {
        "empId": empId,
        "name": emp.name,
        "hasRecord": bool(rows),
        "squads": squads,
        "baseSquad": base.squad if base else None,
        "slots": slots,
        "usedHours": used_hours,
        "remainingHours": max(0.0, round(MAX_DAILY_WORK_HOURS - used_hours, 2)),
    }


@router.get("/scan/suggest")
def scan_suggest(
    region: str,
    furnace: str,
    empId: str | None = None,
    date: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """扫码上岗自动预选：班次/用工类型/工种（不分班优先；工种取常用→上次且可选时）。"""
    emp = (empId or user.get("userid") or "").strip()
    return suggest_scan_defaults(db, region=region, furnace_no=furnace, emp_id=emp, date=date)


@router.get("/anomalies/evidence")
def anomalies_evidence(
    empId: str,
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    from app.services.anomaly_service import (
        _furnace_id_for_emp,
        _furnace_id_for_emp_in_region,
        resolve_demo_date,
    )

    natual_date = resolve_demo_date(date)
    # 值班长只能查本辖区工作点员工的证据（与 confirm/makeup 写端点归属口径一致，按 furnace_id）；
    # stat/hr/admin 看全部。原先值班长可读任意工人证据=水平越权信息泄露。
    fid = _furnace_id_for_emp(db, empId, natual_date)
    if user.get("role") in ("hr", "stat") and fid is not None:
        scope = _current_region_scope(db, user)
        if scope:
            furnace = db.get(Furnace, fid)
            if not furnace or furnace.region != scope:
                local_fid = _furnace_id_for_emp_in_region(db, empId, natual_date, scope)
                if local_fid is not None:
                    fid = local_fid
    if user.get("role") == "chief":
        if fid is None or fid not in _chief_furnace_ids(db, user["userid"]):
            raise HTTPException(403, "该工作点不在您的管辖范围，无法操作")
    _assert_employee_day_region_scope(db, user, empId, fid)
    return get_anomaly_evidence(db, emp_id=empId, date=natual_date)


class AnomalyConfirmBody(BaseModel):
    empId: str
    date: str
    kind: str  # hr | chief
    status: str  # 通过 | 不通过 | 未确认 | (hr) 待值班长确认
    chiefEmpId: str | None = None  # HR「待值班长确认」且该工作点无值班长时，指定值班长
    furnaceId: int | None = None  # 前端看到的固定委托工作点，用于陈旧页面校验


@router.post("/anomalies/confirm")
def anomalies_confirm(
    body: AnomalyConfirmBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_ANOMALY)),
):
    """部门确认 / 值班长确认（相互独立）。部门可设第四态「待值班长确认」（推送值班长）。
    hr 维度限 stat/hr/admin；chief 维度限 chief/admin 且仅本人工作点。"""
    from app.services.anomaly_service import (
        _furnace_id_for_emp,
        _furnace_id_for_emp_in_region,
        furnace_ids_for_emp_on_date,
        resolve_review_furnace_id,
        resolve_demo_date,
    )

    natual_date = resolve_demo_date(body.date)
    fid = _furnace_id_for_emp(db, body.empId, natual_date)
    delegate_fid = fid
    if body.kind == "chief":
        # 委托后考勤可能变化；值班长写权限必须与其待办固定工作点保持一致。
        fid = resolve_review_furnace_id(db, body.empId, natual_date)
    role = user.get("role")
    scope = _current_region_scope(db, user) if role in ("hr", "stat") else None
    if body.kind == "hr":
        if role not in ("stat", "hr", "admin"):
            raise HTTPException(403, "仅统计/人事/管理员可修改部门确认")
    elif body.kind == "chief":
        if role not in ("chief", "admin"):
            raise HTTPException(403, "only chief can set chief confirm")
        if role == "chief":
            # 按 furnace_id（全局唯一）判定归属，避免跨地区同名工作点编号误命中导致越权确认
            allowed = _chief_furnace_ids(db, user["userid"])
            if fid is None or fid not in allowed:
                raise HTTPException(403, "该工作点不在您的管辖范围，无法操作")
    else:
        raise HTTPException(400, "kind must be hr or chief")
    if role in ("hr", "stat") and fid is not None:
        # 除「待值班长确认」外，写路径与读路径同口径：最近扫码落在外区时改按
        # 本地区工作点做地区校验，避免普通确认出现「看得见、点确认却 403」。
        if scope:
            furnace = db.get(Furnace, fid)
            if not furnace or furnace.region != scope:
                local_fid = _furnace_id_for_emp_in_region(db, body.empId, natual_date, scope)
                if local_fid is not None:
                    fid = local_fid
    if role in ("hr", "stat"):
        # 地区校验先于月锁/归属兜底：跨地区员工保持 403 语义，且 fid=None 时
        # 仍按员工收藏地区兜底判断（_assert_employee_day_region_scope 自带回退）。
        _assert_employee_day_region_scope(db, user, body.empId, fid)
    if body.kind == "hr" and body.status == "待值班长确认":
        # 委托刻意不走本地区回退：通知、固定待办和后续值班长写权限必须落在同一工作点。
        resolved_delegate_fid = resolve_review_furnace_id(
            db, body.empId, natual_date
        )
        if body.furnaceId is not None and body.furnaceId != resolved_delegate_fid:
            raise HTTPException(409, "委托目标工作点已变化，请刷新工作台后重新确认")
        delegate_fid = resolved_delegate_fid
        delegate_furnace = db.get(Furnace, delegate_fid) if delegate_fid is not None else None
        if delegate_furnace is None:
            raise HTTPException(
                400, "无法定位该员工当日工作点，请先修复考勤归属后再委托值班长"
            )
        if scope and delegate_furnace.region != scope:
            raise HTTPException(
                403,
                "员工当日工作点不在您的管辖地区，请联系该工作点所属地区的 HR 发起委托",
            )
    if fid is None and role == "chief":
        # 与既有归属校验一致：值班长视角按「不在管辖范围」拒绝，语义不变。
        raise HTTPException(403, "该工作点不在您的管辖范围，无法操作")
    # 月锁只覆盖该员工日真实考勤所影响的导出工作点；固化委托工作点仅决定审核
    # 权限，不应因一条已失效/跨工作点的历史委托额外阻塞当前数据确认。
    lock_fids = furnace_ids_for_emp_on_date(db, body.empId, natual_date)
    if not lock_fids:
        # 无法定位员工日工作点时，月锁按工作点粒度无法校验。该月不存在任何封账
        # 工作点时放行（无工作点证据的确认不触及任何工作点级导出数据）；封账期间
        # 则保守拒绝。冻结口径用全局封账工作点，不用地区权限 scope。
        if locked_furnace_ids_for_range(db, natual_date, natual_date, None):
            raise HTTPException(
                409,
                "该员工当日工作点归属不明确，且该月份存在已封账工作点，无法执行月度导出锁校验；"
                "请先修复归属（确认该员工当日存在有效工作点考勤记录）后再确认",
            )
    else:
        try:
            for lock_fid in lock_fids:
                assert_month_export_unlocked(db, natual_date, lock_fid)
        except DomainError as e:
            raise HTTPException(e.status_code, str(e))
    try:
        return set_confirm(
            db,
            emp_id=body.empId,
            date=body.date,
            kind=body.kind,
            status=body.status,
            actor=user["userid"],
            chief_emp_id=body.chiefEmpId,
            furnace_id=fid if body.kind == "chief" else delegate_fid,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except Exception:
        # set_confirm 在提交前会同时暂存确认状态、委托工作点和值班长绑定；任何
        # 非业务异常都必须显式回滚，避免复用会话时把半成品状态带入后续提交。
        db.rollback()
        raise


@router.get("/chief/review")
def chief_review(
    date: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "admin")),
):
    """值班长 H5 审核确认页：本人辖工作点的 待审补录 + HR 转交「待值班长确认」项。
    date 省略 = 全部日期（前端默认不选日期、看所有待办并翻页）。"""
    from app.services.anomaly_service import get_chief_review_queue

    return get_chief_review_queue(db, chief_emp_id=user["userid"], date=date)


@router.get("/audit-logs")
def audit_logs(
    action: str | None = None,
    actor: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    # HR/统计只看「已配置明确操作」的业务日志（ACTION_LABELS 白名单）；admin 看全部（含 impersonate 等系统操作）
    is_admin = user.get("role") == "admin"
    only_labeled = not is_admin
    # 统计仅监督一线操作（统计/值班长/班组长/工人），HR 与 admin 的操作对统计不可见
    exclude_roles = {"hr", "admin"} if user.get("role") == "stat" else None
    return list_audit_logs(
        db,
        action=action,
        limit=limit,
        only_labeled=only_labeled,
        actor=actor,
        target=target,
        detail=detail,
        start=start,
        end=end,
        exclude_actor_roles=exclude_roles,
        include_request_logs=is_admin,
        include_request_detail=is_admin,
    )


# ---- 代签月度额度（quota）----


@router.get("/makeup/proxy-quota")
def proxy_quota(
    empId: str,
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Chief reads remaining 代签 allowance for a worker (used in the 日结 modal)."""
    return proxy_q.quota_for(db, empId, date)


class ProxyConfigBody(BaseModel):
    value: int


@router.get("/admin/proxy-signin/config")
def proxy_config_get(
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    return {"defaultLimit": proxy_q.get_default_limit(db)}


@router.put("/admin/proxy-signin/config")
def proxy_config_set(
    body: ProxyConfigBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    value = proxy_q.set_default_limit(db, body.value, actor=user["userid"])
    return {"defaultLimit": value}


@router.get("/admin/proxy-signin/usage")
def proxy_usage(
    month: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    return proxy_q.usage(db, month)


class ProxyGrantBody(BaseModel):
    empId: str
    month: str
    extra: int = 1
    reason: str | None = None


@router.post("/admin/proxy-signin/grant")
def proxy_grant(
    body: ProxyGrantBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    return proxy_q.add_grant(
        db,
        emp_id=body.empId,
        month=body.month,
        extra=body.extra,
        reason=body.reason,
        actor=user["userid"],
    )


@router.get("/me")
def me(user=Depends(current_user), db: Session = Depends(get_db)):
    out = {"userid": user["userid"], "role": user["role"],
           "adminTestBanner": settings.fdc_admin_test_banner}
    # 超级管理员会话标记：super=该会话由密钥发起；impersonating=当前正模拟他人
    if user.get("super"):
        out["super"] = True
        out["impersonating"] = user.get("userid") != "admin"
    # 透出员工姓名/工号（扫码上岗、各页展示用）
    if user.get("userid"):
        emp = db.scalar(
            select(Employee).where(
                (Employee.dd_user_id == user["userid"]) | (Employee.emp_id == user["userid"]),
                Employee.deleted.is_(False),
            )
        )
        if emp:
            out["name"] = emp.name
            out["empId"] = emp.emp_id
            raw_emp = db.scalar(
                select(DingtalkEmployeeRaw).where(
                    DingtalkEmployeeRaw.userid == (emp.dd_user_id or user["userid"])
                )
            )
            if raw_emp and raw_emp.title:
                out["title"] = raw_emp.title
            if user.get("role") in ("hr", "stat"):
                favorite_regions = set(_employee_favorites(db, emp.emp_id)["region"])
                active_regions = set(
                    db.scalars(select(Region.name).where(Region.deleted.is_(False))).all()
                )
                regions = sorted(favorite_regions & active_regions)
                out["region"] = regions[0] if regions else None
                out["regionConflict"] = len(regions) > 1
                out["regionConfirmed"] = len(regions) == 1
                out["requiresRegionSelection"] = len(regions) != 1
                if len(regions) != 1 and emp.region_confirmed_at is not None:
                    # 选区页需按真实原因展示文案，避免误导排障：
                    # 多地区与「无有效地区」是两种不同故障，处理方式不同。
                    if len(regions) > 1:
                        out["regionMessage"] = (
                            f"账号配置了多个地区（{'、'.join(regions)}），"
                            "请在选区页从已有地区中选择一个完成确认"
                        )
                    else:
                        out["regionMessage"] = (
                            "账号地区已确认但当前无有效地区（可能已被停用），"
                            "请联系管理员重新指派地区"
                        )
        elif user.get("role") in ("hr", "stat"):
            out.update({
                "region": None,
                "regionConflict": False,
                "regionConfirmed": False,
                "requiresRegionSelection": True,
                "regionMessage": "当前账号未关联有效员工，请联系管理员处理",
            })
    # 透出其负责的工作点（一工作点多值班长 → 可能多个），日确认页据此定位。
    # 值班长/管理员等只要在 furnace_chief 有绑定即返回。
    if user.get("userid"):
        fs = db.scalars(
            select(Furnace)
            .join(FurnaceChief, FurnaceChief.furnace_id == Furnace.id)
            .where(FurnaceChief.emp_id == user["userid"])
            .order_by(Furnace.furnace_no_order)
        ).all()
        if not fs:
            f = db.scalar(select(Furnace).where(Furnace.chief_user_id == user["userid"]))
            fs = [f] if f else []
        if fs:
            out["furnaces"] = [{"region": f.region, "furnaceNo": f.furnace_no} for f in fs]
            out["furnace"] = fs[0].furnace_no
            out["region"] = fs[0].region
    return out


@router.get("/regions")
def regions_for_selection(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """登录用户可读的有效地区名称，供首次选区与普通筛选使用。"""
    rows = db.scalars(
        select(Region).where(Region.deleted.is_(False)).order_by(Region.sort, Region.code)
    ).all()
    return {"items": [{"code": row.code, "name": row.name} for row in rows]}


class MyRegionBody(BaseModel):
    region: str = Field(min_length=1, max_length=64)


@router.post("/me/region")
def confirm_my_region(
    body: MyRegionBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "stat")),
):
    """HR/统计仅可首次确认一个页面展示地区；确认后只能由 admin 修改。"""
    emp = db.scalar(
        select(Employee).where(
            (Employee.dd_user_id == user["userid"]) | (Employee.emp_id == user["userid"]),
            Employee.deleted.is_(False),
        )
    )
    if not emp:
        raise HTTPException(404, "当前账号未关联员工")
    # 先锁员工行，防止两个首次选择请求并发改选。
    emp = db.scalar(select(Employee).where(Employee.id == emp.id).with_for_update())
    favorite_regions = set(_employee_favorites(db, emp.emp_id)["region"])
    active_regions = set(
        db.scalars(select(Region.name).where(Region.deleted.is_(False))).all()
    )
    effective_regions = favorite_regions & active_regions
    # 自助选区闸门：
    # - 恰有一个有效地区收藏 → 视为已确认，不得改选（改任意地区=直接扩权）。
    # - 已确认但有效地区全空 → 地区被停用/清空，只能管理员重新指派。
    # - 多个有效地区收藏（含历史多地区回填账号）→ 允许自助从已有地区中收窄确认
    #   其一；这是收窄而非扩权，多地区账号由此自助恢复，不必等管理员介入。
    # - 从未确认且无有效地区 → 首次选择，可从全部有效地区中挑一个。
    if len(effective_regions) == 1:
        raise HTTPException(409, "所在地区已经确认，如需修改请联系管理员")
    if not effective_regions and emp.region_confirmed_at is not None:
        raise HTTPException(
            409,
            "账号已确认过地区，原所在地区现已失效；请联系管理员重新指派地区",
        )
    if not effective_regions:
        raise HTTPException(
            409,
            "当前账号未指派授权地区，请联系管理员配置地区后才能进入业务页",
        )
    narrowing = len(effective_regions) > 1
    region = body.region.strip()
    if narrowing and region not in effective_regions:
        raise HTTPException(
            422,
            f"账号配置了多个地区，请从已有地区中选择一个：{'、'.join(sorted(effective_regions))}",
        )
    if not db.scalar(select(Region.code).where(Region.name == region, Region.deleted.is_(False))):
        raise HTTPException(422, "请选择有效地区")
    _sync_employee_favorite_kind(db, emp.emp_id, "region", [region])
    emp.region_confirmed_at = datetime.now(timezone.utc)
    emp.updated_by = user["userid"]
    db.add(
        AuditLog(
            actor=user["userid"],
            action="employee_region_confirm",
            payload=json.dumps({"empId": emp.emp_id, "region": region}, ensure_ascii=False),
        )
    )
    db.commit()
    return {"ok": True, "region": region, "regionConfirmed": True}


def _self_employee(db: Session, user: dict) -> Employee | None:
    uid = user.get("userid") or ""
    return db.scalar(
        select(Employee).where((Employee.emp_id == uid) | (Employee.dd_user_id == uid))
    )


@router.get("/me/notify-setting")
def get_my_notify_setting(db: Session = Depends(get_db), user=Depends(require_user)):
    """当前用户的钉钉通知开关（个人设置页读取）。"""
    emp = _self_employee(db, user)
    return {"pushEnabled": bool(emp.push_enabled) if emp else False}


class MyNotifySettingBody(BaseModel):
    pushEnabled: bool


@router.post("/me/notify-setting")
def set_my_notify_setting(
    body: MyNotifySettingBody, db: Session = Depends(get_db), user=Depends(require_user)
):
    """本人开关自己的钉钉通知（个人设置页）。仅改自己，按会话身份定位员工。"""
    emp = _self_employee(db, user)
    if not emp:
        raise HTTPException(404, "未找到你的员工档案，请重新登录或联系 HR")
    emp.push_enabled = body.pushEnabled
    emp.updated_by = user["userid"]
    db.add(AuditLog(actor=user["userid"], action="self_notify_setting",
                    payload=json.dumps({"pushEnabled": body.pushEnabled}, ensure_ascii=False)))
    db.commit()
    return {"pushEnabled": emp.push_enabled}


@router.get("/my-notifications")
def my_notifications(
    limit: int = 30,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """当前用户收到的通知（缺编/委托等），供前端消费——原通知只落库无拉取接口，
    导致『发了前端看不到』。按 target=本人工号倒序返回，解析 subject/body。"""
    rows = db.scalars(
        select(NotificationRecord)
        .where(NotificationRecord.target == user["userid"])
        .order_by(NotificationRecord.id.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    items = []
    for r in rows:
        kind = r.kind or ""  # 班次推送等在列上带 kind；缺编等老格式 kind 在 body 里
        subject, detail, ndate = "", "", None
        try:
            data = json.loads(r.payload) if r.payload else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        ndate = data.get("date")
        push_note = data.get("pushNote", "")  # 如「钉钉推送失败：该员工无钉钉号」
        if "body" in data:
            # MockChannel 包装格式：{subject, body(json字符串)}（缺编提醒等走 channel.send）
            subject = data.get("subject", "")
            try:
                body = json.loads(data.get("body") or "") or {}
            except (json.JSONDecodeError, TypeError):
                body = {}
            if isinstance(body, dict):
                kind = kind or body.get("kind", "")
                detail = body.get("detail", "")
                ndate = ndate or body.get("date")  # 上岗提醒/缺编等把 date 放在 body 里
        else:
            # 原始 schema（班次结束推送 shift_push 等直接落库）：之前被当成空 subject/detail 显示成空白「通知」
            if kind == "shift_push":
                subject = f"班次结束小结 · {data.get('furnace', '')} {data.get('squad') or ''}".strip()
            else:
                subject = data.get("subject", "")
            detail = data.get("text") or data.get("detail") or ""
        items.append(
            {
                "id": r.id,
                "ts": r.ts.isoformat() if r.ts else None,
                "date": ndate,
                "subject": subject or "通知",
                "kind": kind,
                "detail": detail,
                "pushNote": push_note,
            }
        )
    return {"items": items, "count": len(items)}


# ----------------------- 班次结束推送（超管管理页） -----------------------
class ShiftPushConfigBody(BaseModel):
    enabled: bool = True
    timePoints: list[str] = []
    excludedEmpIds: list[str] = []


@router.get("/admin/shift-push/config")
def shift_push_config_get(db: Session = Depends(get_db), user=Depends(require_super)):
    from app.services import shift_push_service as sp

    return sp.get_config(db)


@router.put("/admin/shift-push/config")
def shift_push_config_put(
    body: ShiftPushConfigBody, db: Session = Depends(get_db), user=Depends(require_super)
):
    from app.services import shift_push_service as sp

    return sp.set_config(
        db, enabled=body.enabled, time_points=body.timePoints, excluded=body.excludedEmpIds
    )


@router.get("/admin/shift-push/recipients")
def shift_push_recipients(db: Session = Depends(get_db), user=Depends(require_super)):
    from app.services import shift_push_service as sp

    return {"items": sp.recipients(db)}


@router.get("/admin/shift-push/records")
def shift_push_records(
    date: str | None = None, db: Session = Depends(get_db), user=Depends(require_super)
):
    from app.services import shift_push_service as sp
    from app.services.anomaly_service import resolve_demo_date

    d = resolve_demo_date(date) if date else None
    return {"items": sp.list_records(db, date=d)}


@router.get("/admin/shift-push/furnaces")
def shift_push_furnaces(db: Session = Depends(get_db), user=Depends(require_super)):
    from app.services import shift_push_service as sp

    return {"items": sp.furnace_push_status(db)}


class ShiftPushTestBody(BaseModel):
    empId: str


@router.post("/admin/shift-push/test")
def shift_push_test(
    body: ShiftPushTestBody, db: Session = Depends(get_db), user=Depends(require_super)
):
    """给指定值班长发一条钉钉测试推送（调试推送链路）。"""
    from app.services import shift_push_service as sp

    return sp.send_test_push(db, emp_id=body.empId, actor=user["userid"])


@router.get("/admin/shift-push/logs")
def shift_push_logs(
    kind: str | None = None,
    date: str | None = None,
    q: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_super),
):
    """推送日志（全部钉钉推送），含真实投递结果。支持按类型/日期/统一搜索框(目标名字/钉钉ID/工号/内容)筛选，默认不限数量。"""
    from app.services import shift_push_service as sp

    return sp.list_push_logs(db, kind=kind, date=date, q=q, limit=limit)


class ShiftPushRunBody(BaseModel):
    timePoint: str | None = None
    date: str | None = None


@router.post("/admin/shift-push/run")
def shift_push_run(
    body: ShiftPushRunBody | None = None, db: Session = Depends(get_db), user=Depends(require_super)
):
    """手动触发推送（测试/补推）：指定 timePoint 则只跑该点，否则跑配置所有时间点；force 忽略 enabled 与幂等。"""
    from app.services import shift_push_service as sp
    from app.services.anomaly_service import get_demo_context, resolve_demo_date

    date = resolve_demo_date(body.date) if body and body.date else get_demo_context()["today"]
    cfg = sp.get_config(db)
    points = [body.timePoint] if body and body.timePoint else cfg["timePoints"]
    results = [
        sp.run_push(db, time_point=tp, date=date, actor=user["userid"], force=True) for tp in points
    ]
    return {"date": date, "results": results}


@router.get("/employees")
def employees(db: Session = Depends(get_db), user=Depends(require_role(*ROLE_ANOMALY))):
    # 全量花名册仅管理角色可读（工人不应枚举全员，避免信息暴露）；选人用 /employees/search
    rows = db.scalars(select(Employee)).all()
    return {
        "items": [
            {
                "empId": e.emp_id,
                "name": e.name,
                "role": e.role or infer_role(e.dd_user_id or e.emp_id),
                "syncStatus": e.sync_status,
                "employmentStatus": e.employment_status,
            }
            for e in rows
        ]
    }


@router.get("/employees/search")
def employees_search(q: str, db: Session = Depends(get_db), user=Depends(require_user)):
    """按工号/姓名模糊搜索员工（限量），代签选人用——不一次性返回全部。"""
    kw = (q or "").strip()
    if not kw:
        return {"items": []}
    like = f"%{kw}%"
    stmt = select(Employee).where(
        Employee.deleted.is_(False),
        or_(Employee.name.like(like), Employee.emp_id.like(like)),
    )
    if user.get("role") == "chief":
        from app.services.employee_visibility_service import show_departed_to_chief

        if not show_departed_to_chief(db):
            stmt = stmt.where(Employee.employment_status == "active")
    rows = db.scalars(stmt.order_by(Employee.emp_id).limit(20)).all()
    return {
        "items": [
            {
                "empId": e.emp_id,
                "name": e.name,
                "role": e.role or infer_role(e.dd_user_id or e.emp_id),
                "employmentStatus": e.employment_status,
            }
            for e in rows
        ]
    }


def _resolve_chief(user: dict, chief_user_id: str | None) -> str:
    """HR/管理员可指定 chiefUserId；值班长只能操作自己。"""
    if user.get("role") in ("hr", "admin") and chief_user_id:
        return chief_user_id
    return user["userid"]


def _chief_worker_items(
    db: Session, chief_user_id: str, *, include_departed: bool = True
) -> list[dict]:
    stmt = (
        select(ChiefWorker, Employee.name, Employee.employment_status)
        .join(Employee, Employee.emp_id == ChiefWorker.emp_id)
        .where(
            ChiefWorker.chief_user_id == chief_user_id,
            Employee.deleted.is_(False),
        )
    )
    if not include_departed:
        stmt = stmt.where(Employee.employment_status == "active")
    rows = db.execute(stmt.order_by(ChiefWorker.id)).all()
    return [
        {"empId": row.emp_id, "name": name, "employmentStatus": employment_status}
        for row, name, employment_status in rows
    ]


def _chief_worker_employee(db: Session, identity: str) -> Employee:
    employee = db.scalar(
        select(Employee).where(
            or_(Employee.emp_id == identity, Employee.dd_user_id == identity),
            Employee.deleted.is_(False),
        )
    )
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


@router.get("/chief-workers")
def chief_workers_get(
    chief: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    """值班长常用员工映射：值班长看自己的；HR/管理员可传 chief 查指定值班长。"""
    target = _resolve_chief(user, chief)
    # 与写接口同口径：HR/统计只能查看本地区值班长的常用员工，避免读宽写严。
    scope = _current_region_scope(db, user)
    if scope:
        chief_employee = db.scalar(
            select(Employee).where(
                or_(Employee.emp_id == target, Employee.dd_user_id == target),
                Employee.deleted.is_(False),
            )
        )
        if chief_employee is not None:
            _assert_employee_region_scope(db, user, chief_employee)
    include_departed = True
    if user.get("role") == "chief":
        from app.services.employee_visibility_service import show_departed_to_chief

        include_departed = show_departed_to_chief(db)
    return {
        "chief": target,
        "items": _chief_worker_items(db, target, include_departed=include_departed),
    }


class ChiefWorkerAdd(BaseModel):
    empId: str
    chiefUserId: str | None = None


@router.post("/chief-workers")
def chief_workers_add(
    body: ChiefWorkerAdd,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    target = _resolve_chief(user, body.chiefUserId)
    emp = body.empId.strip()
    if not target or not emp:
        raise HTTPException(400, "chief and empId required")
    chief_employee = _chief_worker_employee(db, target)
    worker_employee = _chief_worker_employee(db, emp)
    if user.get("role") == "chief":
        from app.services.employee_visibility_service import show_departed_to_chief

        if (
            worker_employee.employment_status == "departed"
            and not show_departed_to_chief(db)
        ):
            raise HTTPException(404, "employee not found")
    # 与员工管理页口径一致（allow_unassigned=True）：尚无地区收藏的值班长/工人
    # 允许本地区 HR 维护绑定，避免存量未归属数据上功能整体 403 不可用。
    _assert_employee_region_scope(db, user, chief_employee)
    _assert_employee_region_scope(db, user, worker_employee)
    exists = db.scalar(
        select(ChiefWorker).where(
            ChiefWorker.chief_user_id == target, ChiefWorker.emp_id == emp
        )
    )
    if not exists:
        db.add(ChiefWorker(chief_user_id=target, emp_id=emp))
        db.commit()
    include_departed = True
    if user.get("role") == "chief":
        from app.services.employee_visibility_service import show_departed_to_chief

        include_departed = show_departed_to_chief(db)
    return {
        "chief": target,
        "items": _chief_worker_items(db, target, include_departed=include_departed),
    }


@router.delete("/chief-workers/{chief_user_id}/{emp_id}")
def chief_workers_delete(
    chief_user_id: str,
    emp_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_role("chief", "hr", "admin")),
):
    if user.get("role") not in ("hr", "admin") and user["userid"] != chief_user_id:
        raise HTTPException(403, "forbidden")
    row = db.scalar(
        select(ChiefWorker).where(
            ChiefWorker.chief_user_id == chief_user_id, ChiefWorker.emp_id == emp_id
        )
    )
    chief_employee = _chief_worker_employee(db, chief_user_id)
    # 与新增同口径：未归属员工允许本地区 HR 删除绑定（见 chief_workers_add 说明）。
    _assert_employee_region_scope(db, user, chief_employee)
    # 删除必须能清理已软删或花名册已不存在的历史绑定。员工仍存在时继续执行地区校验；
    # 不存在时以绑定所属值班长的地区作为授权边界，不把残留关系变成永久不可删。
    worker_employee = db.scalar(
        select(Employee).where(
            or_(Employee.emp_id == emp_id, Employee.dd_user_id == emp_id)
        )
    )
    if worker_employee is not None:
        _assert_employee_region_scope(db, user, worker_employee)
    if row:
        db.delete(row)
        db.commit()
    include_departed = True
    if user.get("role") == "chief":
        from app.services.employee_visibility_service import show_departed_to_chief

        include_departed = show_departed_to_chief(db)
    return {
        "chief": chief_user_id,
        "items": _chief_worker_items(
            db, chief_user_id, include_departed=include_departed
        ),
    }


@router.get("/my-chiefs")
def my_chiefs(db: Session = Depends(get_db), user=Depends(require_user)):
    """当前工人绑定到的值班长（模拟扫码页按值班长筛选用）。"""
    names = {e.emp_id: e.name for e in db.scalars(select(Employee)).all()}
    rows = db.scalars(
        select(ChiefWorker).where(ChiefWorker.emp_id == user["userid"]).order_by(ChiefWorker.id)
    ).all()
    return {"items": [{"userid": r.chief_user_id, "name": names.get(r.chief_user_id)} for r in rows]}


# ---- 员工管理（人事 HR）：CRUD + 常用关联 ----
FAVORITE_KINDS = ("post", "furnace", "region")


def _employee_favorites(db: Session, emp_id: str) -> dict[str, list[str]]:
    rows = db.scalars(
        select(EmployeeFavorite).where(EmployeeFavorite.emp_id == emp_id)
    ).all()
    out: dict[str, list[str]] = {k: [] for k in FAVORITE_KINDS}
    for r in rows:
        out.setdefault(r.kind, []).append(r.value)
    return out


def _sync_employee_favorite_kind(
    db: Session, emp_id: str, kind: str, values: list[str]
) -> None:
    """按差集同步单类常用项；相同内容重复保存不删除重插，避免唯一键冲突。"""
    desired = set(dict.fromkeys(x.strip() for x in values if x.strip()))
    existing = db.scalars(
        select(EmployeeFavorite).where(
            EmployeeFavorite.emp_id == emp_id, EmployeeFavorite.kind == kind
        )
    ).all()
    existing_values = {fav.value for fav in existing}
    for fav in existing:
        if fav.value not in desired:
            db.delete(fav)
    for value in desired - existing_values:
        db.add(EmployeeFavorite(emp_id=emp_id, kind=kind, value=value))


def _sync_employee_favorites(
    db: Session, emp_id: str, favorites: "EmployeeFavoritesBody"
) -> None:
    for kind in FAVORITE_KINDS:
        _sync_employee_favorite_kind(db, emp_id, kind, getattr(favorites, kind))


def _employee_dict(db: Session, e: Employee) -> dict:
    d = {
        "empId": e.emp_id,
        "name": e.name,
        "ddUserId": e.dd_user_id,
        "role": e.role or infer_role(e.dd_user_id or e.emp_id),
        "employmentStatus": e.employment_status,
        "pushEnabled": bool(e.push_enabled),
        "favorites": _employee_favorites(db, e.emp_id),
    }
    emps = {x.emp_id: x for x in db.scalars(select(Employee)).all()}
    names = {k: v.name for k, v in emps.items()}
    if (e.role or "") == "chief":
        bound = db.scalars(
            select(ChiefWorker).where(ChiefWorker.chief_user_id == e.emp_id)
        ).all()
        d["chiefWorkers"] = [
            {
                "empId": b.emp_id,
                "name": names.get(b.emp_id),
                "employmentStatus": emps[b.emp_id].employment_status
                if b.emp_id in emps
                else "active",
            }
            for b in bound
        ]
    # 反向：被哪些值班长关联（常用员工/代签名单）——与值班长详情里的关联工人互为正反。
    # 仅保留「当前仍是值班长」者：值班长被换绑降级为工人后 ChiefWorker 残留，否则会把已非值班长的人
    # 错误地显示为某工人的「所属值班长」（与正向 chiefWorkers 只在 role=chief 时展示口径不一致）。
    def _still_chief(cid: str) -> bool:
        x = emps.get(cid)
        return bool(x) and (x.role or infer_role(x.dd_user_id or cid)) == "chief"

    rev = db.scalars(select(ChiefWorker).where(ChiefWorker.emp_id == e.emp_id)).all()
    d["belongChiefs"] = [
        {
            "empId": r.chief_user_id,
            "name": names.get(r.chief_user_id),
            "employmentStatus": emps[r.chief_user_id].employment_status,
        }
        for r in rev
        if _still_chief(r.chief_user_id)
    ]
    return d


@router.get("/admin/employees")
def admin_employees_list(
    q: str = "",
    regions: list[str] = Query(default=[]),
    roles: list[str] = Query(default=[]),
    push_enabled: list[bool] = Query(default=[]),
    employment_status: str | None = Query(
        default=None, pattern="^(active|departed)$"
    ),
    db: Session = Depends(get_db), user=Depends(require_role(*ROLE_STAT_HR))
):
    # 只读名单：stat 也可读（劳勤工作台筛选/HR确认需要）；增删改仍限 HR/admin（见下方 POST/PUT/DELETE）。
    stmt = select(Employee).where(Employee.deleted.is_(False))
    keyword = q.strip()
    if keyword:
        pattern = f"%{keyword}%"
        search_conditions = [
            Employee.emp_id.ilike(pattern),
            Employee.name.ilike(pattern),
            Employee.employment_status.ilike(pattern),
        ]
        status_keyword = keyword.lower()
        if status_keyword in ("在职", "在职人员"):
            search_conditions.append(Employee.employment_status == "active")
        elif status_keyword in ("离职", "已离职", "离职人员"):
            search_conditions.append(Employee.employment_status == "departed")
        stmt = stmt.where(or_(*search_conditions))
    if push_enabled:
        stmt = stmt.where(Employee.push_enabled.in_(set(push_enabled)))
    if employment_status:
        stmt = stmt.where(Employee.employment_status == employment_status)
    scope = _current_region_scope(db, user)
    if scope:
        # HR/统计按地区收窄；值班长候选还须放行已实际绑定本地区工作点的员工，
        # 避免其地区收藏陈旧时前端候选与后端绑定校验口径不一致。
        stmt = stmt.where(
            or_(
                select(EmployeeFavorite.id).where(
                    EmployeeFavorite.emp_id == Employee.emp_id,
                    EmployeeFavorite.kind == "region",
                    EmployeeFavorite.value == scope,
                ).exists(),
                select(FurnaceChief.id)
                .join(Furnace, Furnace.id == FurnaceChief.furnace_id)
                .where(
                    FurnaceChief.emp_id == Employee.emp_id,
                    FurnaceChief.deleted.is_(False),
                    Furnace.region == scope,
                )
                .exists(),
                ~select(EmployeeFavorite.id).where(
                    EmployeeFavorite.emp_id == Employee.emp_id,
                    EmployeeFavorite.kind == "region",
                ).exists(),
            )
        )
    elif regions:
        stmt = stmt.where(
            select(EmployeeFavorite.id).where(
                EmployeeFavorite.emp_id == Employee.emp_id,
                EmployeeFavorite.kind == "region",
                EmployeeFavorite.value.in_(set(regions)),
            ).exists()
        )
    rows = db.scalars(stmt.order_by(Employee.emp_id)).all()
    if roles:
        role_set = set(roles)
        # 旧数据 role 可能为空，列表展示会按工号/钉钉 ID 推断；筛选必须与展示口径一致。
        rows = [e for e in rows if (e.role or infer_role(e.dd_user_id or e.emp_id)) in role_set]
    region_map: dict[str, list[str]] = {}
    active_region_map: dict[str, set[str]] = {}
    chief_region_map: dict[str, set[str]] = {}
    if rows:
        for favorite in db.scalars(
            select(EmployeeFavorite).where(
                EmployeeFavorite.emp_id.in_([e.emp_id for e in rows]),
                EmployeeFavorite.kind == "region",
            )
        ).all():
            region_map.setdefault(favorite.emp_id, []).append(favorite.value)
        for values in region_map.values():
            values.sort()
        for emp_id, region in db.execute(
            select(EmployeeFavorite.emp_id, EmployeeFavorite.value)
            .join(Region, Region.name == EmployeeFavorite.value)
            .where(
                EmployeeFavorite.emp_id.in_([e.emp_id for e in rows]),
                EmployeeFavorite.kind == "region",
                Region.deleted.is_(False),
            )
        ).all():
            active_region_map.setdefault(emp_id, set()).add(region)
        for emp_id, region in db.execute(
            select(FurnaceChief.emp_id, Furnace.region)
            .join(Furnace, Furnace.id == FurnaceChief.furnace_id)
            .join(Region, Region.name == Furnace.region)
            .where(
                FurnaceChief.emp_id.in_([e.emp_id for e in rows]),
                FurnaceChief.deleted.is_(False),
                Region.deleted.is_(False),
            )
        ).all():
            chief_region_map.setdefault(emp_id, set()).add(region)
    return {
        "candidateContractVersion": 2,
        "items": [
            {
                "id": e.id,
                "empId": e.emp_id,
                "name": e.name,
                "ddUserId": e.dd_user_id,
                "role": e.role or infer_role(e.dd_user_id or e.emp_id),
                "explicitRole": e.role,
                "employmentStatus": e.employment_status,
                "regions": region_map.get(e.emp_id, []),
                "chiefRegions": sorted(
                    active_region_map.get(e.emp_id, set())
                    | chief_region_map.get(e.emp_id, set())
                ),
                "chiefCandidateEligible": (
                    e.role == "chief" or bool(chief_region_map.get(e.emp_id))
                ),
                "pushEnabled": bool(e.push_enabled),
                "updatedAt": e.updated_at.isoformat() if e.updated_at else None,
                "updatedBy": e.updated_by,
            }
            for e in rows
        ]
    }


class ChiefDepartedEmployeeVisibilityBody(BaseModel):
    showDepartedToChief: bool


@router.get("/admin/employees/chief-departed-visibility")
def chief_departed_employee_visibility_get(
    db: Session = Depends(get_db), user=Depends(require_role(*ROLE_HR))
):
    from app.services.employee_visibility_service import get_config

    return get_config(db)


@router.put("/admin/employees/chief-departed-visibility")
def chief_departed_employee_visibility_put(
    body: ChiefDepartedEmployeeVisibilityBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_HR)),
):
    from app.services.employee_visibility_service import set_config

    result = set_config(db, body.showDepartedToChief)
    db.add(
        AuditLog(
            actor=user["userid"],
            action="chief_departed_employee_visibility",
            payload=json.dumps(result, ensure_ascii=False),
        )
    )
    db.commit()
    return result


@router.get("/admin/employees/{emp_id}")
def admin_employee_get(
    emp_id: str, db: Session = Depends(get_db), user=Depends(require_role(*ROLE_HR))
):
    e = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    if not e:
        raise HTTPException(404, "employee not found")
    _assert_employee_region_scope(db, user, e)
    return _employee_dict(db, e)


class EmployeeFavoritesBody(BaseModel):
    post: list[str] = Field(default_factory=list)
    furnace: list[str] = Field(default_factory=list)
    region: list[str] = Field(default_factory=list)


class EmployeeBody(BaseModel):
    # 长度上限：超长字段在 Pydantic 层 422，不再直达 DB 触发 varchar 截断 500
    empId: str = Field(max_length=32)
    name: str = Field(max_length=64)
    ddUserId: str | None = Field(None, max_length=64)
    role: str = "worker"
    favorites: EmployeeFavoritesBody | None = None


@router.post("/admin/employees")
def admin_employee_create(
    body: EmployeeBody, db: Session = Depends(get_db), user=Depends(require_role("admin"))
):
    emp_id = body.empId.strip()
    if not emp_id:
        raise HTTPException(400, "empId required")
    if db.scalar(select(Employee).where(Employee.emp_id == emp_id)):
        raise HTTPException(409, "员工工号已存在")
    role = body.role if body.role in VALID_ROLES else "worker"
    favorite_regions = body.favorites.region if body.favorites is not None else []
    if role in ("hr", "stat") and len(set(favorite_regions)) > 1:
        raise HTTPException(422, "HR/统计只能配置一个所在地区")
    employee = Employee(
        emp_id=emp_id,
        name=body.name.strip(),
        dd_user_id=(body.ddUserId or "").strip() or emp_id,
        role=role,
        sync_status="手工",
        created_by=user["userid"],
        updated_by=user["userid"],
        region_confirmed_at=(
            datetime.now(timezone.utc) if role in ("hr", "stat") and favorite_regions else None
        ),
    )
    db.add(employee)
    if body.favorites is not None:
        _sync_employee_favorites(db, emp_id, body.favorites)
    db.add(
        AuditLog(
            actor=user["userid"],
            action="employee_create",
            payload=json.dumps({"empId": emp_id, "name": body.name, "role": role}, ensure_ascii=False),
        )
    )
    db.commit()
    return _employee_dict(db, db.scalar(select(Employee).where(Employee.emp_id == emp_id)))


class EmployeeUpdateBody(BaseModel):
    name: str | None = Field(None, max_length=64)
    ddUserId: str | None = Field(None, max_length=64)
    role: str | None = None
    pushEnabled: bool | None = None
    favorites: EmployeeFavoritesBody | None = None


@router.put("/admin/employees/{emp_id}")
def admin_employee_update(
    emp_id: str,
    body: EmployeeUpdateBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_HR)),
):
    e = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    if not e:
        raise HTTPException(404, "employee not found")
    _assert_employee_region_scope(db, user, e)
    is_hr = user.get("role") == "hr" and not user.get("super")
    if is_hr and body.favorites is not None:
        scope = _current_region_scope(db, user)
        if scope and any(region != scope for region in set(body.favorites.region)):
            raise HTTPException(403, "HR 只能把员工归属到本人所在地区")
    current_role = e.role or infer_role(e.dd_user_id or e.emp_id)
    old_dd_user_id = e.dd_user_id
    if is_hr and current_role in HR_PROTECTED_ROLES:
        raise HTTPException(403, "HR 无权修改管理员、人事或统计角色员工")
    if is_hr and body.ddUserId is not None:
        raise HTTPException(403, "HR 无权修改钉钉 UserId")
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(422, f"非法角色：{body.role}")
        if is_hr and body.role in HR_PROTECTED_ROLES:
            raise HTTPException(403, "HR 不能授予管理员、人事或统计角色")
        if body.role in PRIVILEGED_ROLES and not (user.get("super") or user.get("role") == "admin"):
            raise HTTPException(403, "仅管理员可授予 admin 角色")
    next_role = body.role if body.role is not None else current_role
    if user.get("role") == "admin" and next_role in ("hr", "stat"):
        final_regions = (
            set(body.favorites.region)
            if body.favorites is not None
            else set(_employee_favorites(db, emp_id)["region"])
        )
        if len(final_regions) > 1:
            raise HTTPException(422, "HR/统计只能配置一个所在地区")
    if body.name is not None:
        e.name = body.name.strip()
    if body.ddUserId is not None:
        # 允许清空钉钉 userId（不再回落工号）：admin 可把它编辑为空
        e.dd_user_id = body.ddUserId.strip()
    if body.role is not None:
        e.role = body.role
        if (
            user.get("role") == "admin"
            and body.role in ("hr", "stat")
            and _employee_favorites(db, emp_id)["region"]
        ):
            e.region_confirmed_at = datetime.now(timezone.utc)
    if body.pushEnabled is not None:
        e.push_enabled = body.pushEnabled
    if body.favorites is not None:
        _sync_employee_favorites(db, emp_id, body.favorites)
        if user.get("role") == "admin" and next_role in ("hr", "stat"):
            e.region_confirmed_at = (
                datetime.now(timezone.utc) if body.favorites.region else None
            )
    if body.role is not None and body.role != current_role:
        # 会话中缓存了登录时的角色；管理员改角色后必须强制重新登录，
        # 同时覆盖以工号、旧/新钉钉 UserId 建立的会话。
        invalidate_sessions_for_employee(db, e.emp_id, old_dd_user_id, e.dd_user_id)
    e.updated_by = user["userid"]
    db.add(
        AuditLog(
            actor=user["userid"],
            action="employee_update",
            payload=json.dumps(
                {"empId": emp_id, "name": body.name, "role": body.role}, ensure_ascii=False
            ),
        )
    )
    db.commit()
    return _employee_dict(db, e)


@router.delete("/admin/employees/{emp_id}")
def admin_employee_delete(
    emp_id: str, db: Session = Depends(get_db), user=Depends(require_role("admin"))
):
    e = db.scalar(select(Employee).where(Employee.emp_id == emp_id, Employee.deleted.is_(False)))
    if not e:
        raise HTTPException(404, "employee not found")
    if e.emp_id == "admin":
        raise HTTPException(400, "不能删除 admin 账号")
    # 软删除 + 解绑其作为值班长的工作点
    e.deleted = True
    e.updated_by = user["userid"]
    for f in db.scalars(select(Furnace).where(Furnace.chief_user_id == emp_id)).all():
        f.chief_user_id = None
    for fc in db.scalars(select(FurnaceChief).where(FurnaceChief.emp_id == emp_id)).all():
        db.delete(fc)
    # 解绑「常用员工/代签名单」双向关联：否则被删工人仍出现在值班长代签候选、被删值班长仍挂着名单
    for cw in db.scalars(
        select(ChiefWorker).where(
            or_(ChiefWorker.chief_user_id == emp_id, ChiefWorker.emp_id == emp_id)
        )
    ).all():
        db.delete(cw)
    # 清理其个人常用收藏（岗位/工作点/地区）
    for fav in db.scalars(
        select(EmployeeFavorite).where(EmployeeFavorite.emp_id == emp_id)
    ).all():
        db.delete(fav)
    db.add(
        AuditLog(
            actor=user["userid"],
            action="employee_delete",
            payload=json.dumps({"empId": emp_id}, ensure_ascii=False),
        )
    )
    db.commit()
    return {"ok": True}


_DICT_MODELS = {
    "role": Role,
    "position": Position,
    "region": Region,
    "squad": Squad,
    "furnace_type": FurnaceType,
}
_DICT_PREFIX = {"position": "POS", "region": "REG", "squad": "SQ", "furnace_type": "FT"}


def _dict_model(kind: str):
    m = _DICT_MODELS.get(kind)
    if not m:
        raise HTTPException(404, "unknown dict")
    return m


@router.get("/admin/dict/{kind}")
def dict_list(kind: str, db: Session = Depends(get_db), user=Depends(require_role("admin"))):
    if kind == "furnace":
        rows = db.scalars(
            select(Furnace).order_by(Furnace.region, Furnace.furnace_no_order, Furnace.furnace_no)
        ).all()
        ftype_names = {t.code: t.name for t in db.scalars(select(FurnaceType)).all()}
        return {
            "items": [
                {
                    "code": f.code,
                    "name": f.furnace_no,
                    "region": f.region,
                    "regionCode": f.region_code,
                    "furnaceType": ftype_names.get(f.furnace_type_code, ""),
                    "furnaceTypeCode": f.furnace_type_code,
                    "status": f.status,
                    "sort": f.furnace_no_order,
                }
                for f in rows
            ]
        }
    M = _dict_model(kind)
    rows = db.scalars(select(M).where(M.deleted.is_(False)).order_by(M.sort, M.code)).all()
    return {
        "items": [
            {
                "code": r.code,
                "name": r.name,
                "sort": r.sort,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
                "updatedBy": r.updated_by,
            }
            for r in rows
        ]
    }


class DictBody(BaseModel):
    # 加长度上限：超长名在 Pydantic 层 422，不再直达 DB 触发 varchar 截断 500
    name: str = Field("", max_length=64)
    code: str | None = Field(None, max_length=32)
    sort: int | None = None
    # 仅工作点字典使用
    regionCode: str | None = None
    furnaceType: str | None = None
    status: str | None = None


def _dict_create_furnace(body: DictBody, db: Session) -> dict:
    """新建工作点：地区(code)+工作点编号 唯一；FUR 编码由 code_resolver 自动分配。"""
    furnace_no = body.name.strip()
    if not furnace_no:
        raise HTTPException(400, "工作点编号必填")
    region = db.get(Region, body.regionCode or "")
    if not region or region.deleted:
        raise HTTPException(400, "regionCode 无效")
    if db.scalar(
        select(Furnace).where(Furnace.region == region.name, Furnace.furnace_no == furnace_no)
    ):
        raise HTTPException(409, "该地区下工作点编号已存在")
    digits = "".join(ch for ch in furnace_no if ch.isdigit())
    ftype = _require_furnace_type(db, (body.furnaceType or "").strip() or "标准型A")
    f = Furnace(
        region=region.name,
        region_code=region.code,
        furnace_no=furnace_no,
        furnace_no_order=int(digits) if digits else 0,
        furnace_type_code=ftype.code,
        name=f"{region.name}{furnace_no}",
        status="active",
    )
    db.add(f)
    db.flush()
    inherit_locked_months_for_region(db, f.id, region.name, actor="system:new-furnace")
    db.commit()
    return {"ok": True, "code": f.code}


@router.post("/admin/dict/{kind}")
def dict_create(
    kind: str, body: DictBody, db: Session = Depends(get_db), user=Depends(require_role("admin"))
):
    if kind == "furnace":
        return _dict_create_furnace(body, db)
    M = _dict_model(kind)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    if db.scalar(select(M).where(M.name == name, M.deleted.is_(False))):
        raise HTTPException(409, "已存在同名项")
    code = (body.code or "").strip()
    if not code:
        prefix = _DICT_PREFIX.get(kind)
        if not prefix:
            raise HTTPException(400, "该字典需显式提供 code")
        nums = [
            int(c[len(prefix):])
            for c in db.scalars(select(M.code)).all()
            if c.startswith(prefix) and c[len(prefix):].isdigit()
        ]
        n = (max(nums) + 1) if nums else 1
        code = f"{prefix}{n}" if kind == "squad" else f"{prefix}{n:02d}"
    if db.get(M, code):
        raise HTTPException(409, "code 已存在")
    db.add(M(code=code, name=name, sort=body.sort or 0, created_by=user["userid"], updated_by=user["userid"]))
    db.add(AuditLog(actor=user["userid"], action="dict_create",
                    payload=json.dumps({"kind": kind, "code": code, "name": name}, ensure_ascii=False)))
    db.commit()
    return {"ok": True, "code": code}


@router.put("/admin/dict/{kind}/{code}")
def dict_update(
    kind: str,
    code: str,
    body: DictBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_HR)),
):
    if kind == "furnace":
        f = db.scalar(select(Furnace).where(Furnace.code == code))
        if not f:
            raise HTTPException(404, "not found")
        if body.furnaceType:
            f.furnace_type_code = _require_furnace_type(db, body.furnaceType.strip()).code
        if body.status in ("active", "inactive"):
            f.status = body.status
        db.commit()
        return {"ok": True}
    M = _dict_model(kind)
    r = db.get(M, code)
    if not r or r.deleted:
        raise HTTPException(404, "not found")
    if body.name:
        new_name = body.name.strip()
        old_name = r.name
        # 地区/班次名是冗余列：多表同时存 名 + code 外键。改名须联动这些冗余名列，否则
        # 显示旧名，且班次名是花名册/编制/唯一索引的匹配键，错配会让记录从对应分组消失。
        # 按 code 定位；不少旧数据 code 为空(如 quota_template.region_code、quota_rule.squad_code)，
        # 故对 code 为空者兜底按旧名匹配。记录只存 code 的表(如 attendance.region_code)不受影响。
        CASCADES: dict[str, list] = {
            "region": [
                (Furnace, Furnace.region, Furnace.region_code),
                (SquadSchedule, SquadSchedule.region, SquadSchedule.region_code),
                (QuotaTemplate, QuotaTemplate.region, QuotaTemplate.region_code),
                (QuotaTemplateScope, QuotaTemplateScope.region, QuotaTemplateScope.region_code),
                (SelectablePost, SelectablePost.region, SelectablePost.region_code),
                (ShiftWindow, ShiftWindow.region, ShiftWindow.region_code),
            ],
            "squad": [
                (QuotaRule, QuotaRule.squad, QuotaRule.squad_code),
                (QuotaTemplate, QuotaTemplate.squad, QuotaTemplate.squad_code),
                (AttendanceRecord, AttendanceRecord.squad, AttendanceRecord.squad_code),
                (ScanRecord, ScanRecord.squad, ScanRecord.squad_code),
                (MakeupRequest, MakeupRequest.squad, MakeupRequest.squad_code),
            ],
        }
        if new_name != old_name and kind in CASCADES:
            for M, name_col, code_col in CASCADES[kind]:
                db.query(M).filter(
                    or_(code_col == code, and_(code_col.is_(None), name_col == old_name))
                ).update({name_col: new_name}, synchronize_session=False)
            if kind == "region":
                # 地区收藏是账号权限与员工归属的权威来源。若员工已同时收藏目标地区，
                # 先删旧值再批量改名，避免命中 (emp_id, kind, value) 唯一约束。
                target_employee_ids = set(
                    db.scalars(
                        select(EmployeeFavorite.emp_id).where(
                            EmployeeFavorite.kind == "region",
                            EmployeeFavorite.value == new_name,
                        )
                    ).all()
                )
                if target_employee_ids:
                    db.query(EmployeeFavorite).filter(
                        EmployeeFavorite.kind == "region",
                        EmployeeFavorite.value == old_name,
                        EmployeeFavorite.emp_id.in_(target_employee_ids),
                    ).delete(synchronize_session=False)
                db.query(EmployeeFavorite).filter(
                    EmployeeFavorite.kind == "region",
                    EmployeeFavorite.value == old_name,
                ).update(
                    {EmployeeFavorite.value: new_name}, synchronize_session=False
                )
        r.name = new_name
    if body.sort is not None:
        r.sort = body.sort
    r.updated_by = user["userid"]
    db.add(AuditLog(actor=user["userid"], action="dict_update",
                    payload=json.dumps({"kind": kind, "code": code, "name": body.name}, ensure_ascii=False)))
    db.commit()
    return {"ok": True}


@router.delete("/admin/dict/{kind}/{code}")
def dict_delete(
    kind: str, code: str, db: Session = Depends(get_db), user=Depends(require_role("admin"))
):
    if kind == "furnace":
        f = db.scalar(select(Furnace).where(Furnace.code == code))
        if not f:
            raise HTTPException(404, "not found")
        f.status = "inactive"  # 工作点不物理删，置停用
        db.commit()
        return {"ok": True}
    M = _dict_model(kind)
    r = db.get(M, code)
    if not r:
        raise HTTPException(404, "not found")
    # 设备类型被工作点/编制模板引用时禁止删除
    if kind == "furnace_type":
        n_furnace = len(db.scalars(select(Furnace).where(Furnace.furnace_type_code == code)).all())
        n_tpl = len(
            db.scalars(select(QuotaTemplate).where(QuotaTemplate.furnace_type_code == code)).all()
        )
        if n_furnace or n_tpl:
            raise HTTPException(409, f"该设备类型仍被引用（工作点 {n_furnace}、编制模板 {n_tpl}），不可删除")
    # 地区/班次同理：仍被引用时禁止删除，否则工作点/编制/考勤会指向已删字典（code 为空者按名兜底）
    IN_USE = {
        "region": [(Furnace, Furnace.region, Furnace.region_code, "工作点")],
        "squad": [
            (QuotaRule, QuotaRule.squad, QuotaRule.squad_code, "编制规则"),
            (AttendanceRecord, AttendanceRecord.squad, AttendanceRecord.squad_code, "考勤记录"),
            (ScanRecord, ScanRecord.squad, ScanRecord.squad_code, "历史扫码记录"),
            (MakeupRequest, MakeupRequest.squad, MakeupRequest.squad_code, "历史补录记录"),
        ],
    }
    if kind in IN_USE:
        refs = []
        for M, name_col, code_col, label in IN_USE[kind]:
            n = db.scalar(
                select(func.count())
                .select_from(M)
                .where(or_(code_col == code, and_(code_col.is_(None), name_col == r.name)))
            )
            if n:
                refs.append(f"{label} {n}")
        if refs:
            raise HTTPException(409, f"该{'地区' if kind == 'region' else '班次'}仍被引用（{'、'.join(refs)}），不可删除")
    if kind == "region":
        # 停用地区前必须先改派仍归属该地区的 HR/统计账号：否则账号的有效地区
        # 会静默失效，触发重新选区/越权边界问题（见 POST /me/region 的确认闸门）。
        bound_accounts = db.scalar(
            select(func.count())
            .select_from(EmployeeFavorite)
            .join(Employee, Employee.emp_id == EmployeeFavorite.emp_id)
            .where(
                Employee.deleted.is_(False),
                Employee.role.in_(["hr", "stat"]),
                EmployeeFavorite.kind == "region",
                EmployeeFavorite.value == r.name,
            )
        )
        if bound_accounts:
            raise HTTPException(
                409, f"该地区仍被 {bound_accounts} 个 HR/统计账号归属，请先修改账号所在地区再停用"
            )
    r.deleted = True
    r.updated_by = user["userid"]
    db.add(AuditLog(actor=user["userid"], action="dict_delete",
                    payload=json.dumps({"kind": kind, "code": code}, ensure_ascii=False)))
    db.commit()
    return {"ok": True}


class FavoritesBody(BaseModel):
    kind: str
    values: list[str] = []


@router.put("/admin/employees/{emp_id}/favorites")
def admin_employee_favorites(
    emp_id: str,
    body: FavoritesBody,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_HR)),
):
    if body.kind not in FAVORITE_KINDS:
        raise HTTPException(400, f"invalid kind: {body.kind}")
    employee = db.scalar(select(Employee).where(Employee.emp_id == emp_id))
    if not employee:
        raise HTTPException(404, "employee not found")
    _assert_employee_region_scope(db, user, employee)
    current_role = employee.role or infer_role(employee.dd_user_id or employee.emp_id)
    if user.get("role") == "hr" and not user.get("super") and current_role in HR_PROTECTED_ROLES:
        raise HTTPException(403, "HR 无权修改管理员、人事或统计角色员工")
    if user.get("role") == "hr" and not user.get("super") and body.kind == "region":
        scope = _current_region_scope(db, user)
        if scope and any(region != scope for region in set(body.values)):
            raise HTTPException(403, "HR 只能把员工归属到本人所在地区")
    if (
        user.get("role") == "admin"
        and current_role in ("hr", "stat")
        and body.kind == "region"
        and len(set(body.values)) > 1
    ):
        raise HTTPException(422, "HR/统计只能配置一个所在地区")
    _sync_employee_favorite_kind(db, emp_id, body.kind, body.values)
    if user.get("role") == "admin" and current_role in ("hr", "stat") and body.kind == "region":
        employee.region_confirmed_at = (
            datetime.now(timezone.utc) if body.values else None
        )
    db.commit()
    return {"ok": True, "favorites": _employee_favorites(db, emp_id)}


@router.post("/recon/run")
@report_endpoint(read_only=False)
def recon_run(
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(*ROLE_STAT_HR)),
):
    # 校验日期格式与有效性，非法日期(如 2026-13-99)返回 400 而非静默接受
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"非法日期：{date!r}，应为 YYYY-MM-DD")
    # 地区账号只能重算本地区的员工对账，避免触发全库重算；admin 不收窄。
    scope = _current_region_scope(db, user)
    employee_ids = (
        None if scope is None else set(_employee_codes_for_region(db, scope) or [])
    )
    run_recon_for_date(db, date, employee_ids=employee_ids)
    return {"ok": True, "date": date}


@router.post("/admin/run-escalation")
def admin_run_escalation(
    date: str,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.escalation_service import run_escalation_for_date

    return run_escalation_for_date(db, date)


@router.get("/admin/escalations")
def admin_escalations(
    date: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.models import EscalationRecord
    from app.services.anomaly_service import resolve_demo_date

    natual_date = resolve_demo_date(date) if date else None
    stmt = select(EscalationRecord)
    if natual_date:
        stmt = stmt.where(EscalationRecord.natual_date == natual_date)
    rows = db.scalars(stmt).all()
    return {
        "items": [
            {
                "id": r.id,
                "furnaceId": r.furnace_id,
                "date": r.natual_date,
                "level": r.level,
                "targets": r.targets,
                "status": r.status,
            }
            for r in rows
        ]
    }


# ============================================================================
# 钉钉考勤同步：从外部 MySQL 视图同步原始打卡 + 转为劳勤工作台数据
# ============================================================================
class DingtalkAttendanceRangeBody(BaseModel):
    dateFrom: str
    dateTo: str


@router.post("/admin/dingtalk-attendance/pull")
def dingtalk_attendance_pull(
    body: DingtalkAttendanceRangeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """非阻塞启动「按员工拉取+转劳勤」后台任务：按现存员工工号拉取原始考勤→转换→对账，立即返回，
    前端轮询 transform-status 看进度。管理页「拉取并转劳勤」按钮的入口。"""
    from app.services.dingtalk_attendance_service import start_pull

    try:
        scope = _current_region_scope(db, user)
        base_codes = None if not scope else _employee_codes_for_region(db, scope)
        codes, locked_ids = _lock_narrowed_codes(db, user, body.dateFrom, body.dateTo, base_codes)
        # 收窄后若没有任何可同步员工（全在封账工作点），拒绝并说明，避免前端空转轮询。
        if locked_ids and codes is not None and len(codes) == 0:
            raise ConflictError("范围内员工全部属于已封账工作点，无可同步数据；如需更新请先由 HR/Admin 解锁对应月份")
        if not scope:
            return start_pull(db, body.dateFrom, body.dateTo, actor=user["userid"], codes=codes)
        return start_pull(
            db,
            body.dateFrom,
            body.dateTo,
            actor=user["userid"],
            codes=codes,
            region=scope,
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/admin/dingtalk-attendance/sync")
def dingtalk_attendance_sync(
    body: DingtalkAttendanceRangeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """按现存员工工号同步指定日期范围的钉钉原始考勤打卡到 dingtalk_attendance_raw（幂等）。

    同步等待：仅适合小范围/脚本兼容调用；管理页已改走 /pull（后台任务）。"""
    from app.services.dingtalk_attendance_service import _log_sync, sync_range

    scope = _current_region_scope(db, user)
    try:
        base_codes = None if not scope else _employee_codes_for_region(db, scope)
        codes, locked_ids = _lock_narrowed_codes(db, user, body.dateFrom, body.dateTo, base_codes)
        if locked_ids and codes is not None and len(codes) == 0:
            raise ConflictError("范围内员工全部属于已封账工作点，无可同步数据；如需更新请先由 HR/Admin 解锁对应月份")
        from app.services.sync_log_service import task
        with task(db, "attendance_raw", user["userid"], region=scope,
                  date_from=body.dateFrom, date_to=body.dateTo):
            res = sync_range(db, body.dateFrom, body.dateTo, codes=codes)
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        _log_sync(db, actor=user["userid"], trigger="manual", result={
            "dateFrom": body.dateFrom, "dateTo": body.dateTo, "region": scope,
            "synced": None, "error": str(e)})
        raise HTTPException(503, str(e))
    _log_sync(db, actor=user["userid"], trigger="manual", result={
        "dateFrom": body.dateFrom, "dateTo": body.dateTo, "region": scope,
        "synced": res, "error": None})
    return res


class ResyncOneBody(BaseModel):
    empId: str
    date: str
    window: int = 1  # 兼容旧客户端；服务端按固定 D±1 证据窗口执行


@router.post("/admin/dingtalk-attendance/resync-one")
def dingtalk_attendance_resync_one(
    body: ResyncOneBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "stat", "admin")),
):
    """单人定点重拉：同步该工号目标日对应的 D±1 打卡证据并重跑对账。

    供劳勤工作台每行「重拉劳勤」按钮使用：定点修正某天异常（含迟到补卡），
    D±1 覆盖夜班跨自然日；同时拉取当天出勤工时。仅拉这一个工号、范围小，可同步执行后即时刷新行。
    """
    from app.services.dingtalk_attendance_service import (
        _log_sync, _validate_date,
    )

    emp = (body.empId or "").strip()
    if not emp:
        raise HTTPException(400, "缺少 empId")
    try:
        _validate_date(body.date)
    except ValueError as e:
        raise HTTPException(400, str(e))
    d = datetime.strptime(body.date, "%Y-%m-%d").date()
    date_from = (d - timedelta(days=1)).isoformat()
    date_to = (d + timedelta(days=1)).isoformat()

    eligible = db.scalar(
        select(AttendanceRecord.id)
        .join(Employee, Employee.emp_id == AttendanceRecord.emp_id)
        .where(
            AttendanceRecord.emp_id == emp,
            AttendanceRecord.natual_date == body.date,
            AttendanceRecord.status.in_(["active", "待审"]),
            Employee.deleted.is_(False),
        )
    )
    if not eligible:
        raise HTTPException(409, "该员工当天没有有效或待审工作点记录，无需拉取劳勤")

    from app.services.anomaly_service import (
        _furnace_id_for_emp,
        furnace_ids_for_emp_on_date,
    )

    furnace_id = _furnace_id_for_emp(db, emp, body.date)
    lock_fids = furnace_ids_for_emp_on_date(db, emp, body.date)
    if furnace_id is None and not lock_fids:
        raise HTTPException(409, "该员工当天工作点归属不明确，无法安全重拉劳勤")
    scope_fid = furnace_id or lock_fids[0]
    _assert_employee_day_region_scope(db, user, emp, scope_fid)
    furnace = db.get(Furnace, scope_fid)
    if furnace is None:
        raise HTTPException(409, "该员工当天工作点不存在，无法安全重拉劳勤")
    scope = _current_region_scope(db, user)

    try:
        assert_month_range_unlocked(db, date_from, date_to, lock_fids or [scope_fid])
        from app.services.attendance_resync_service import task_lease, refresh_employee_day, BusyError
        try:
            from app.services.sync_log_service import task
            with task(db, "attendance_one", user["userid"], region=scope,
                      date_from=body.date, date_to=body.date):
                with task_lease(db) as token:
                    synced, attendance_result, transformed = refresh_employee_day(db, emp, body.date, token)
        except BusyError as e:
            raise HTTPException(409, str(e))
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        _log_sync(db, actor=user["userid"], trigger="manual", result={
            "dateFrom": date_from, "dateTo": date_to, "region": scope,
            "synced": None, "error": str(e)})
        raise HTTPException(503, str(e))
    _log_sync(db, actor=user["userid"], trigger="manual", result={
        "dateFrom": date_from, "dateTo": date_to, "region": scope,
        "synced": synced, "attendanceResult": attendance_result.get("synced", {}), "transformed": transformed, "error": None})
    return {
        "empId": emp, "dateFrom": date_from, "dateTo": date_to,
        "synced": synced, "attendanceResult": attendance_result, "transformed": transformed,
    }


class BulkResyncBody(BaseModel):
    dateFrom: str
    dateTo: str
    region: str | None = None


@router.post("/admin/dingtalk-attendance/resync-bulk")
def attendance_resync_bulk(body: BulkResyncBody, db: Session = Depends(get_db),
                           user=Depends(require_role("hr", "stat", "admin"))):
    from app.services.attendance_resync_service import start, BusyError
    scope = _current_region_scope(db, user)
    if scope and body.region and body.region != scope:
        raise HTTPException(403, "无权重拉其他地区")
    try:
        return start(db, body.dateFrom, body.dateTo, user["userid"], scope or body.region)
    except BusyError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/admin/dingtalk-attendance/resync-bulk-status")
def attendance_resync_bulk_status(jobId: str | None = None, region: str | None = None,
                                  db: Session = Depends(get_db),
                                  user=Depends(require_role("hr", "stat", "admin"))):
    from app.models import AttendanceResyncJob, AttendanceResyncItem
    from app.services.attendance_resync_service import status
    scope = _current_region_scope(db, user)
    stmt = select(AttendanceResyncJob)
    if jobId:
        stmt = stmt.where(AttendanceResyncJob.id == jobId)
    if scope:
        stmt = stmt.where(AttendanceResyncJob.region == scope)
    elif region:
        stmt = stmt.where(AttendanceResyncJob.region == region)
    job = db.scalar(stmt.order_by(AttendanceResyncJob.created_at.desc()).limit(1))
    if job is None:
        if jobId:
            raise HTTPException(404, "批量任务不存在或无权查看")
        return {"status": "idle"}
    payload = status(db, job)
    payload["items"] = [{"empId": r.emp_id, "date": r.date, "status": r.status,
        "attempts": r.attempts, "before": json.loads(r.before_value) if r.before_value else None,
        "after": json.loads(r.after_value) if r.after_value else None, "error": r.error}
        for r in db.scalars(select(AttendanceResyncItem).where(AttendanceResyncItem.job_id == job.id)
            .order_by(AttendanceResyncItem.id.desc()).limit(100))]
    return payload


@router.post("/admin/dingtalk-attendance/transform")
def dingtalk_attendance_transform(
    body: DingtalkAttendanceRangeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """非阻塞启动后台劳勤转换（按天推进）：立即返回，前端轮询 transform-status 看进度。

    避免整月转换阻塞 worker 把服务拖死（旧同步实现的故障点）。
    """
    from app.services.dingtalk_attendance_service import start_transform

    try:
        scope = _current_region_scope(db, user)
        base_codes = None if not scope else _employee_codes_for_region(db, scope)
        codes, locked_ids = _lock_narrowed_codes(db, user, body.dateFrom, body.dateTo, base_codes)
        if locked_ids and codes is not None and len(codes) == 0:
            raise ConflictError("范围内员工全部属于已封账工作点，无可转换数据；如需更新请先由 HR/Admin 解锁对应月份")
        if not scope:
            return start_transform(db, body.dateFrom, body.dateTo, codes=codes, actor=user["userid"])
        return start_transform(
            db,
            body.dateFrom,
            body.dateTo,
            codes=codes,
            region=scope,
            actor=user["userid"],
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/admin/dingtalk-attendance/transform-status")
def dingtalk_attendance_transform_status(
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """劳勤转换后台任务进度（前端 2 秒轮询）。"""
    from app.services.dingtalk_attendance_service import get_transform_status

    scope = _current_region_scope(db, user)
    return get_transform_status(db, region=scope)


@router.get("/admin/dingtalk-attendance/sync-logs")
def dingtalk_attendance_sync_logs(
    limit: int = 50,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """同步日志：定时/手动考勤同步的执行记录（含日期范围、拉取/入库/转换条数、成败）。"""
    scope = _current_region_scope(db, user)
    stmt = select(AuditLog).where(AuditLog.action == "attendance_auto_sync")
    if scope:
        # AuditLog.payload 是历史 Text 列。生产 PostgreSQL 与密闭测试 SQLite 分别
        # 使用各自 JSON 提取函数，把地区权限过滤放在 LIMIT 之前。
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            log_region = func.jsonb_extract_path_text(cast(AuditLog.payload, JSONB), "region")
        else:
            log_region = func.json_extract(AuditLog.payload, "$.region")
        # region 为空的是覆盖所有地区的定时/管理员全局同步，地区 HR 也必须可见。
        stmt = stmt.where(or_(log_region == scope, log_region.is_(None)))
    rows = db.scalars(
        stmt.order_by(AuditLog.ts.desc()).limit(max(1, min(limit, 200)))
    ).all()
    items = []
    for r in rows:
        try:
            p = json.loads(r.payload) if r.payload else {}
        except (ValueError, TypeError):
            p = {}
        items.append({
            "id": r.id,
            "ts": r.ts.isoformat() if r.ts else None,
            "actor": r.actor,
            "trigger": p.get("trigger"),
            "dateFrom": p.get("dateFrom"),
            "dateTo": p.get("dateTo"),
            "fetched": p.get("fetched"),
            "upserted": p.get("upserted"),
            "attendanceResultFetched": p.get("attendanceResultFetched"),
            "attendanceResultUpserted": p.get("attendanceResultUpserted"),
            "transformInserted": p.get("transformInserted"),
            "matched": p.get("matched"),
            "ok": p.get("ok"),
            "error": p.get("error"),
        })
    return {"items": items}


@router.get("/admin/dingtalk-attendance/raw")
def dingtalk_attendance_raw_list(
    dateFrom: str,
    dateTo: str,
    limit: int = 500,
    employeeId: str | None = None,
    employeeName: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """列出范围内已同步的原始考勤打卡（供管理页表格展示）。"""
    from app.services.dingtalk_attendance_service import list_raw

    try:
        scope = _current_region_scope(db, user)
        return list_raw(
            db,
            dateFrom,
            dateTo,
            limit,
            codes=_employee_codes_for_region(db, scope),
            employee_id=employeeId,
            employee_name=employeeName,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/admin/dingtalk-attendance/result/pull")
def dingtalk_attendance_result_pull(
    body: DingtalkAttendanceRangeBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """非阻塞启动「按员工拉取劳勤考勤结果」后台任务：按现存员工工号同步 view_external_att_result。"""
    from app.services.dingtalk_attendance_service import start_attendance_result_pull

    try:
        scope = _current_region_scope(db, user)
        base_codes = None if not scope else _employee_codes_for_region(db, scope)
        codes, locked_ids = _lock_narrowed_codes(db, user, body.dateFrom, body.dateTo, base_codes)
        if locked_ids and codes is not None and len(codes) == 0:
            raise ConflictError("范围内员工全部属于已封账工作点，无可拉取数据；如需更新请先由 HR/Admin 解锁对应月份")
        if not scope:
            return start_attendance_result_pull(db, body.dateFrom, body.dateTo, codes=codes, actor=user["userid"])
        return start_attendance_result_pull(
            db,
            body.dateFrom,
            body.dateTo,
            codes=codes,
            region=scope,
            actor=user["userid"],
        )
    except DomainError as e:
        raise HTTPException(e.status_code, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/admin/dingtalk-attendance/result/status")
def dingtalk_attendance_result_status(
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """劳勤考勤结果拉取后台任务进度。"""
    from app.services.dingtalk_attendance_service import get_attendance_result_status

    scope = _current_region_scope(db, user)
    return get_attendance_result_status(db, region=scope)


@router.get("/admin/dingtalk-attendance/result")
def dingtalk_attendance_result_list(
    dateFrom: str,
    dateTo: str,
    limit: int = 500,
    userCode: str | None = None,
    userName: str | None = None,
    deptName: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "admin")),
):
    """列出范围内已同步的劳勤考勤结果（view_external_att_result 镜像）。"""
    from app.services.dingtalk_attendance_service import list_attendance_result

    try:
        scope = _current_region_scope(db, user)
        return list_attendance_result(
            db,
            dateFrom,
            dateTo,
            limit,
            user_code=userCode,
            user_name=userName,
            dept_name=deptName,
            codes=_employee_codes_for_region(db, scope),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


# ============================================================================
# 钉钉通讯录同步：后台同步部门树+员工到原始表 + 转为 employee 花名册
# ============================================================================
@router.post("/admin/dingtalk-employee/sync")
def dingtalk_employee_sync(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """非阻塞启动后台同步（部门树+员工）；前端轮询 sync-status 看进度。"""
    from app.services.dingtalk_employee_service import start_sync

    return start_sync(db, actor=user["userid"])


@router.get("/admin/dingtalk-employee/sync-status")
def dingtalk_employee_sync_status(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.dingtalk_employee_service import get_state

    return get_state(db)


class TransformBody(BaseModel):
    overwrite: bool = False


@router.post("/admin/dingtalk-employee/transform")
def dingtalk_employee_transform(
    body: TransformBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """把原始员工转为 employee 花名册：按工号 upsert + 写角色/常用职位/常用地区/值班长绑定。
    overwrite=False 仅填空、True 以映射表覆盖。"""
    from app.services.dingtalk_employee_service import transform

    from app.services.sync_log_service import task, add_counts
    with task(db, "employee_transform", user["userid"]):
        result = transform(db, overwrite=bool(body and body.overwrite))
        add_counts(employeeInserted=result["inserted"], employeeUpdated=result["updated"])
        return result


class ImportOneBody(BaseModel):
    userid: str
    overwrite: bool = False


@router.post("/admin/dingtalk-employee/import-one")
def dingtalk_employee_import_one(
    body: ImportOneBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """HR 单人导入：用钉钉直接查询某个 userid 并落地花名册（与批量同源：
    角色/常用岗位、根部门常用地区映射、值班长按部门绑定工作点）。无需先跑整体通讯录同步。"""
    from app.services.dingtalk_employee_service import import_single_user

    try:
        from app.services.sync_log_service import task, add_counts
        with task(db, "employee_import", user["userid"]):
            res = import_single_user(db, body.userid, overwrite=body.overwrite, actor=user["userid"])
            add_counts(memberFetched=1, **{"employeeInserted" if res.get("action") == "inserted" else "employeeUpdated": 1})
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    db.add(AuditLog(actor=user["userid"], action="dingtalk-import-one",
                    payload=f"userid={body.userid} emp={res.get('empId')} role={res.get('role')}"))
    db.commit()
    return res


@router.get("/admin/dingtalk-employee/raw")
def dingtalk_employee_raw_list(
    q: str | None = None,
    limit: int = 500,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.dingtalk_employee_service import list_raw as emp_list_raw

    return emp_list_raw(db, q, limit)


@router.get("/admin/dingtalk-employee/differences")
def dingtalk_employee_differences(
    q: str | None = None,
    field: str | None = None,
    differentOnly: bool = True,
    page: int = 1,
    pageSize: int = 100,
    updateMode: str = "append",
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.dingtalk_employee_service import list_employee_differences

    try:
        return list_employee_differences(
            db,
            q=q,
            field=field,
            different_only=differentOnly,
            page=page,
            page_size=pageSize,
            update_mode=updateMode,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


class ApplyEmployeeDifferencesBody(BaseModel):
    empIds: list[str] = Field(min_length=1, max_length=500)
    field: str
    confirmProtected: bool = False
    updateMode: str | None = None
    rule: dict | None = None


@router.post("/admin/dingtalk-employee/apply-differences")
def dingtalk_employee_apply_differences(
    body: ApplyEmployeeDifferencesBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.dingtalk_employee_service import apply_employee_differences

    try:
        return apply_employee_differences(
            db,
            emp_ids=body.empIds,
            field=body.field,
            actor=user["userid"],
            confirm_protected=body.confirmProtected,
            update_mode=body.updateMode,
            rule_payload=body.rule,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


class EmployeeSyncRuleBody(BaseModel):
    field: str
    enabled: bool = True
    scope: str = "different"
    updateMode: str
    q: str = ""
    localRegions: list[str] = Field(default_factory=list)
    currentRoles: list[str] = Field(default_factory=list)
    targetRoles: list[str] = Field(default_factory=list)
    remoteTitles: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    furnaces: list[str] = Field(default_factory=list)
    differenceTypes: list[str] = Field(default_factory=list)
    skipMulti: bool = True


class EmployeeAutoSyncConfigBody(BaseModel):
    enabled: bool = False
    rules: dict[str, EmployeeSyncRuleBody]


@router.get("/admin/dingtalk-employee/auto-sync-config")
def dingtalk_employee_auto_sync_config_get(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import get_config

    return get_config(db)


@router.put("/admin/dingtalk-employee/auto-sync-config")
def dingtalk_employee_auto_sync_config_put(
    body: EmployeeAutoSyncConfigBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import put_config

    try:
        return put_config(db, body.model_dump(), user["userid"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class EmployeeSyncWhitelistBody(BaseModel):
    empIds: list[str] = Field(min_length=1, max_length=500)


@router.get("/admin/dingtalk-employee/sync-whitelist")
def dingtalk_employee_sync_whitelist_get(
    q: str = "",
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import list_whitelist

    return list_whitelist(db, q)


@router.post("/admin/dingtalk-employee/sync-whitelist")
def dingtalk_employee_sync_whitelist_post(
    body: EmployeeSyncWhitelistBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import add_whitelist

    return add_whitelist(db, body.empIds, user["userid"])


@router.delete("/admin/dingtalk-employee/sync-whitelist/{emp_id}")
def dingtalk_employee_sync_whitelist_delete(
    emp_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import remove_whitelist

    return remove_whitelist(db, emp_id, user["userid"])


@router.post("/admin/dingtalk-employee/differences/preview")
def dingtalk_employee_differences_preview(
    body: EmployeeSyncRuleBody,
    page: int = 1,
    pageSize: int = 100,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import preview

    try:
        return preview(db, body.model_dump(), page=page, page_size=pageSize)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class EmployeeManualSyncBody(BaseModel):
    rule: EmployeeSyncRuleBody


@router.post("/admin/dingtalk-employee/manual-sync")
def dingtalk_employee_manual_sync(
    body: EmployeeManualSyncBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import start_run

    try:
        rule = body.rule.model_dump()
        rule["enabled"] = True
        return start_run(db, [rule], user["userid"], "manual")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@router.get("/admin/dingtalk-employee/manual-sync/status")
def dingtalk_employee_manual_sync_status(
    runId: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    from app.services.employee_difference_sync_service import run_status

    return run_status(db, runId)


class RosterRootItem(BaseModel):
    label: str
    deptId: int
    regionCode: str


class RosterRootsBody(BaseModel):
    items: list[RosterRootItem]


@router.get("/admin/dingtalk-employee/roster-roots")
def dingtalk_roster_roots_get(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """同步范围（根部门）列表：描述、钉钉部门 ID、基础地区。"""
    from app.services.dingtalk_employee_service import list_roots

    return list_roots(db)


@router.put("/admin/dingtalk-employee/roster-roots")
def dingtalk_roster_roots_put(
    body: RosterRootsBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """覆盖式保存同步范围（按顺序写入；每个根部门必须选择有效基础地区）。"""
    from app.services.dingtalk_employee_service import save_roots

    try:
        return save_roots(db, [it.model_dump() for it in body.items])
    except ValueError as e:
        raise HTTPException(400, str(e))


class TitleMapItem(BaseModel):
    title: str
    role: str | None = None
    post: str | None = None


class TitleMapBody(BaseModel):
    items: list[TitleMapItem]


@router.get("/admin/dingtalk-employee/title-map")
def dingtalk_title_map_get(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """钉钉职位 → 角色/常用职位 映射列表。"""
    from app.services.dingtalk_employee_service import list_title_map

    return list_title_map(db)


@router.put("/admin/dingtalk-employee/title-map")
def dingtalk_title_map_put(
    body: TitleMapBody,
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """覆盖式保存职位映射（title 唯一；role/post 可空）。"""
    from app.services.dingtalk_employee_service import save_title_map

    try:
        return save_title_map(db, [it.model_dump() for it in body.items])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/admin/dingtalk-employee/unmapped-titles")
def dingtalk_unmapped_titles(
    db: Session = Depends(get_db),
    user=Depends(require_role("admin")),
):
    """已同步原始员工里尚未映射的去重职位（含人数），供页面一键导入。"""
    from app.services.dingtalk_employee_service import list_unmapped_titles

    return list_unmapped_titles(db)


@router.get("/admin/sync-execution-logs")
def sync_execution_logs(
    group: str = "attendance",
    kind: str | None = None,
    trigger: str | None = None,
    status: str | None = None,
    dateFrom: str | None = None,
    dateTo: str | None = None,
    page: int = Query(1, ge=1),
    pageSize: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user=Depends(require_role("hr", "stat", "admin")),
):
    """同步页面的执行历史；所有筛选与权限限制在分页之前执行。"""
    from app.models import SyncExecutionLog
    from app.services.sync_log_service import KINDS, COUNT_LABELS
    if group not in {"attendance", "employee"}:
        raise HTTPException(422, "非法同步分类")
    if group == "employee" and user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可查看员工同步日志")
    allowed_kinds = {k: v for k, v in KINDS.items()
                     if k.startswith("attendance_") == (group == "attendance")}
    if kind and kind not in allowed_kinds:
        raise HTTPException(422, "非法同步类型")
    if trigger and trigger not in {"auto", "manual"}:
        raise HTTPException(422, "非法触发方式")
    if status and status not in {"running", "success", "partial", "failed", "skipped"}:
        raise HTTPException(422, "非法执行状态")
    stmt = select(SyncExecutionLog).where(SyncExecutionLog.kind.in_(allowed_kinds))
    scope = _current_region_scope(db, user)
    if scope:
        stmt = stmt.where(or_(SyncExecutionLog.region == scope, SyncExecutionLog.region.is_(None)))
    if kind:
        stmt = stmt.where(SyncExecutionLog.kind == kind)
    if trigger:
        stmt = stmt.where(SyncExecutionLog.trigger == trigger)
    if status:
        stmt = stmt.where(SyncExecutionLog.status == status)
    # 按执行开始时间筛选，日期按中国时区解释。
    from zoneinfo import ZoneInfo
    try:
        lower = datetime.strptime(dateFrom, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) if dateFrom else None
        upper = datetime.strptime(dateTo, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai")) + timedelta(days=1) if dateTo else None
    except ValueError:
        raise HTTPException(422, "日志日期格式应为 YYYY-MM-DD")
    if lower and upper and lower >= upper:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    if lower:
        stmt = stmt.where(SyncExecutionLog.started_at >= lower.astimezone(timezone.utc))
    if upper:
        stmt = stmt.where(SyncExecutionLog.started_at < upper.astimezone(timezone.utc))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(SyncExecutionLog.started_at.desc(), SyncExecutionLog.id.desc())
                      .offset((page - 1) * pageSize).limit(pageSize)).all()
    def timestamp(value):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat() if value else None
    items = []
    for row in rows:
        counts = json.loads(row.counts_json)
        items.append({
            "id": row.id, "kind": row.kind, "kindLabel": KINDS[row.kind],
            "trigger": row.trigger, "actor": row.actor, "region": row.region,
            "dateFrom": row.date_from, "dateTo": row.date_to,
            "status": row.status, "startedAt": timestamp(row.started_at),
            "finishedAt": timestamp(row.finished_at), "counts": counts,
            "fetched": sum(counts.get(k, 0) for k in ("rawFetched", "resultFetched", "memberFetched", "departmentFetched")),
            "upserted": sum(counts.get(k, 0) for k in ("rawUpserted", "resultUpserted", "memberUpserted", "departmentUpserted")),
            "message": row.message,
        })
    return {"items": items, "total": total, "page": page, "pageSize": pageSize,
            "kinds": allowed_kinds, "countLabels": COUNT_LABELS}
