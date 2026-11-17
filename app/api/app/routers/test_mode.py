from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Employee
from app.session_store import create_session, infer_role, resolve_identity, session_cookie_max_age
from app.signing import verify_employees_sig, verify_login_sig

router = APIRouter()


def _safe_redirect(target: str | None) -> str | None:
    """只允许站内相对路径跳转，防开放重定向：必须以单个 / 开头，禁止 //（协议相对）与含 :（绝对 URL）。"""
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//") or ":" in target:
        return None
    return target


@router.get("/test/auth")
def test_auth(
    userid: str,
    role: str | None = None,
    redirect: str | None = None,
    exp: str | None = None,
    sig: str | None = None,
    db: Session = Depends(get_db),
):
    # 签名令牌门控：服务端持 SECRET，启动器本地用同值签名。无 SECRET 或签名/过期不符 → 403。
    # 不再依赖 FDC_TEST_MODE，故线上即使 test 模式关着也能安全地签名登录（公网无 SECRET 者伪造不出 sig）。
    secret = settings.fdc_test_login_secret
    if not verify_login_sig(secret, userid, role, redirect, exp, sig):
        raise HTTPException(403, "invalid or missing signature")
    canonical, resolved_role, name = resolve_identity(db, userid, role)
    # 启动器以 admin 身份直登时授予 super（与真实信息中心一致），否则后台超管入口(推送调试/
    # 基础字典/员工管理等)会因缺 super 标记而隐藏。启动器本身已由 SECRET 门控。
    session_id = create_session(canonical, resolved_role, is_super=(resolved_role == "admin"), db=db)
    db.commit()
    from fastapi.responses import JSONResponse, RedirectResponse

    # 本地身份启动器用：带合法 redirect 时登录后 302 直达对应 App（相对跳转保留请求 host）。
    safe = _safe_redirect(redirect)
    resp = (
        RedirectResponse(safe, status_code=302)
        if safe
        else JSONResponse(
            {
                "userid": canonical,
                "name": name or canonical,
                "role": resolved_role,
            }
        )
    )
    resp.set_cookie("fdc_session", session_id, path="/", httponly=True, max_age=session_cookie_max_age())
    return resp


@router.get("/test/employees")
def test_employees(
    exp: str | None = None,
    sig: str | None = None,
    db: Session = Depends(get_db),
):
    """签名授权拉取在册员工，供启动器动态列出真实身份（不再写死工号）。
    与 /test/auth 同一 SECRET 门控；parts=["employees"]。返回全量在册，前端自行搜索/分组。"""
    secret = settings.fdc_test_login_secret
    if not verify_employees_sig(secret, exp, sig):
        raise HTTPException(403, "invalid or missing signature")
    rows = db.scalars(
        select(Employee).where(Employee.deleted == False).order_by(Employee.emp_id)  # noqa: E712
    ).all()
    return {
        "items": [
            {
                "empId": e.emp_id,
                "name": e.name,
                "role": e.role or infer_role(e.dd_user_id or e.emp_id),
            }
            for e in rows
        ]
    }
