import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal, get_db
from app.models import AuditLog
from app.routers import api, test_mode
from app.services import code_resolver
from app.services.request_audit_queue import RequestAuditEvent, request_audit_buffer
from app.session_store import get_from_headers, get_session

code_resolver.register()

logger = logging.getLogger(__name__)
_scheduler = None


def _resolve_job(job_id: str):
    """惰性导入单个 job 模块：某个 job 模块导入期出错只降级该 job，不拖垮启动。"""
    if job_id == "fdc_escalation_daily":
        from app.services.escalation_service import escalation_job

        return escalation_job
    if job_id == "fdc_shift_push_minutely":
        from app.services.shift_push_service import shift_push_job

        return shift_push_job
    if job_id == "fdc_attendance_sync_daily":
        from app.services.dingtalk_attendance_service import attendance_sync_job

        return attendance_sync_job
    if job_id == "fdc_employee_difference_sync_daily":
        from app.services.employee_difference_sync_service import automatic_sync_job

        return automatic_sync_job
    raise KeyError(job_id)


_CRON_SPECS: tuple[tuple[str, dict], ...] = (
    ("fdc_escalation_daily", {"hour": 22, "minute": 0}),
    # 班次结束推送：每分钟检查当前时刻是否命中超管配置的推送时间点（动态生效，无需重排）
    ("fdc_shift_push_minutely", {"minute": "*"}),
    # 每天 0 点同步上月全月 + 本月至今：原始打卡和考勤结果两张源表
    ("fdc_attendance_sync_daily", {"hour": 0, "minute": 0}),
    # 员工差异自动同步：配置默认关闭；开启后先拉通讯录，再按三个字段规则执行。
    ("fdc_employee_difference_sync_daily", {"hour": 0, "minute": 0}),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    app.state.scheduler_status = {
        "started": False,
        "allHealthy": False,
        "jobIds": [],
        "error": None,
        "failedJobs": [],
    }
    await request_audit_buffer.start()
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:
        # APScheduler 是生产硬依赖。导入失败（包括传递依赖不兼容）必须阻止应用启动，
        # 否则探针仍会报健康、但所有日常任务都会静默停摆。
        app.state.scheduler_status["error"] = f"APScheduler 导入失败：{exc}"
        logger.exception("APScheduler 导入失败，拒绝以调度器失效状态启动")
        _scheduler = None
        raise

    # 除 APScheduler 导入失败外，其余初始化问题（时区配置、单个 job 模块导入、
    # 单个 add_job）一律按 job 粒度降级，让扫码打卡等核心接口继续服务，
    # 避免一个配置笔误把整站打成反复重启。失败明细落在 scheduler_status 供 /health 暴露。
    failed_jobs: list[str] = []
    tz = settings.fdc_escalation_tz
    tz_error: Exception | None = None
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001 - 时区配置来自环境变量，任何异常都按配置错误降级
        tz_error = exc

    if tz_error is not None:
        app.state.scheduler_status = {
            "started": False,
            "allHealthy": False,
            "jobIds": [],
            "error": (
                f"FDC_ESCALATION_TZ 配置了无效时区 {tz!r}：{tz_error}。"
                "定时任务已停用，接口服务正常；请修正配置后重启"
            ),
            "failedJobs": [],
        }
        logger.error("无效时区配置 FDC_ESCALATION_TZ=%r，定时任务降级停用", tz)
        _scheduler = None
    else:
        _scheduler = BackgroundScheduler(timezone=tz)
        for job_id, trigger_kwargs in _CRON_SPECS:
            if job_id == "fdc_attendance_sync_daily" and (
                not settings.fdc_attendance_sync_scheduler_enabled
                or not settings.attendance_mysql_host
            ):
                # 维护开关关闭，或外部考勤源未配置：不注册任务，避免每日空跑/连接报错。
                logger.info("考勤自动同步未启用（维护开关关闭或外部考勤源未配置），跳过注册")
                continue
            try:
                job = _resolve_job(job_id)
                _scheduler.add_job(
                    job,
                    CronTrigger(timezone=tz, **trigger_kwargs),
                    id=job_id,
                    replace_existing=True,
                )
            except Exception as exc:  # noqa: BLE001 - 单个 job 失败不影响其他 job
                failed_jobs.append(f"{job_id}: {exc}")
                logger.exception("定时任务 %s 初始化失败，跳过该任务", job_id)
        try:
            _scheduler.start()
        except Exception as exc:  # noqa: BLE001
            failed_jobs.append(f"scheduler.start: {exc}")
            logger.exception("调度器启动失败，定时任务降级停用")
            _scheduler = None
        else:
            job_ids = sorted(job.id for job in _scheduler.get_jobs())
            all_healthy = bool(job_ids) and not failed_jobs
            app.state.scheduler_status = {
                "started": all_healthy,
                "allHealthy": all_healthy,
                "jobIds": job_ids,
                "error": (
                    f"部分定时任务初始化失败：{'；'.join(failed_jobs)}"
                    if failed_jobs
                    else None
                ),
                "failedJobs": failed_jobs,
            }
            if job_ids:
                logger.info("定时任务已启动：%s", ", ".join(job_ids))

    # 收敛上一次进程遗留的 running 员工同步记录（重启/OOM 会留下永久空洞）。
    # 启动期尽力而为：数据库不可达等问题只记一条警告，不影响应用提供服务。
    try:
        from app.services.employee_difference_sync_service import (
            reconcile_stale_running_runs,
        )

        reconcile_stale_running_runs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时收敛员工差异同步 running 状态失败（忽略）：%s", exc)

    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)
    app.state.scheduler_status = {
        "started": False,
        "allHealthy": False,
        "jobIds": [],
        "error": "应用正在关闭",
        "failedJobs": [],
    }
    await request_audit_buffer.close()


app = FastAPI(title="FDC API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_SENSITIVE_KEY_PARTS = ("authorization", "cookie", "token", "secret", "password", "code", "sig", "session")
_MAX_AUDIT_BODY = 4096
# 高频只读监控不能反过来制造数据库写入；该接口的权限仍由路由 admin 依赖校验。
_REQUEST_AUDIT_EXCLUDED_PATHS = frozenset({
    "/api/v1/health",
    "/api/v1/admin/connection-pool",
})


def _redact_value(key: str, value: Any) -> Any:
    lk = key.lower()
    if any(part in lk for part in _SENSITIVE_KEY_PARTS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "...[truncated]"
    return value


def _safe_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if any(part in lk for part in _SENSITIVE_KEY_PARTS):
            continue
        out[k] = v[:500]
    return out


def _safe_query(request: Request) -> dict[str, Any]:
    return {k: _redact_value(k, v) for k, v in request.query_params.multi_items()}


def _safe_body(raw: bytes) -> Any:
    if not raw:
        return None
    trimmed = raw[:_MAX_AUDIT_BODY]
    text = trimmed.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    if isinstance(data, dict):
        redacted = {str(k): _redact_value(str(k), v) for k, v in data.items()}
    else:
        redacted = data
    if len(raw) > _MAX_AUDIT_BODY:
        return {"value": redacted, "truncated": True}
    return redacted


def _audit_identity(request: Request, db: Session) -> dict[str, Any]:
    if settings.fdc_test_mode:
        hdr = get_from_headers(
            {
                "x-test-user": request.headers.get("x-test-user"),
                "x-test-role": request.headers.get("x-test-role"),
            }
        )
        if hdr:
            return {"userid": hdr.get("userid") or "anonymous", "role": hdr.get("role") or "", "loggedIn": True}
    sid = request.cookies.get("fdc_session")
    sess = get_session(sid, db=db, touch=True) if sid else None
    if sess:
        return {
            "userid": sess.get("userid") or "anonymous",
            "role": sess.get("role") or "",
            "loggedIn": True,
        }
    return {"userid": "anonymous", "role": "", "loggedIn": False}


def _request_action(status: int, logged_in: bool) -> tuple[str, str]:
    if status < 400:
        return "request_ok", "ok"
    if status == 401 and not logged_in:
        return "request_unauthenticated", "unauthenticated"
    if status == 403 and logged_in:
        return "request_forbidden", "forbidden"
    if logged_in:
        return "request_error", "logged_in_error"
    return "request_rejected", "rejected"


def _request_audit_payload(
    request: Request,
    body: bytes,
    status: int,
    elapsed_ms: int,
    category: str,
    role: str,
    error: str | None,
) -> str:
    payload = {
        "category": category,
        "status": status,
        "durationMs": elapsed_ms,
        "actorRole": role,
        "request": {
            "method": request.method,
            "path": request.url.path,
            "query": _safe_query(request),
            "headers": _safe_headers(request),
            "body": _safe_body(body),
            "client": request.client.host if request.client else None,
        },
    }
    if error:
        payload["error"] = error[:500]
    return json.dumps(payload, ensure_ascii=False)


def _queued_audit_identity(request: Request, status: int) -> tuple[dict[str, Any], str | None]:
    """Resolve an already-authenticated identity without opening another DB session."""
    ident = getattr(request.state, "audit_identity", None)
    session_id = getattr(request.state, "audit_session_id", None)
    if isinstance(ident, dict):
        return ident, session_id
    if settings.fdc_test_mode:
        hdr = get_from_headers(
            {
                "x-test-user": request.headers.get("x-test-user"),
                "x-test-role": request.headers.get("x-test-role"),
            }
        )
        if hdr:
            return {**hdr, "loggedIn": True}, None
    session_id = request.cookies.get("fdc_session")
    # create_session/current_user keep this cache warm. A cold cache after restart is
    # populated by the first authenticated route; unknown routes stay anonymous rather
    # than reintroducing one database checkout solely for generic request logging.
    sess = get_session(session_id, touch=False) if session_id else None
    if sess:
        return {**sess, "loggedIn": True}, session_id
    if session_id and status >= 400:
        # 冷缓存下的 4xx/5xx 很少见，允许一次 DB 回查以保留错误审计归属；
        # 正常 2xx 请求绝不为通用日志额外借数据库连接。
        db, gen = _audit_db_session()
        try:
            try:
                sess = get_session(session_id, db=db, touch=False)
            except Exception as exc:  # noqa: BLE001 - audit lookup must not replace the response
                logger.warning("错误审计身份回查失败，降级为 anonymous：%s", exc)
            else:
                if sess:
                    return {**sess, "loggedIn": True}, session_id
        finally:
            if gen is not None:
                try:
                    next(gen)
                except StopIteration:
                    pass
            else:
                db.close()
    return {"userid": "anonymous", "role": "", "loggedIn": False}, None


def _audit_db_session():
    override = app.dependency_overrides.get(get_db)
    if override:
        gen = override()
        db = next(gen)
        return db, gen
    return SessionLocal(), None


def _write_request_audit_sync(
    request: Request,
    body: bytes,
    status: int,
    elapsed_ms: int,
    error: str | None = None,
    *,
    event: RequestAuditEvent | None = None,
) -> None:
    if not request.url.path.startswith("/api/"):
        return
    db, gen = _audit_db_session()
    try:
        if event is None:
            ident = _audit_identity(request, db)
            action, category = _request_action(status, bool(ident.get("loggedIn")))
            actor = str(ident.get("userid") or "anonymous")[:64]
            payload = _request_audit_payload(
                request,
                body,
                status,
                elapsed_ms,
                category,
                str(ident.get("role") or ""),
                error,
            )
            event_id = None
            occurred_at = None
        else:
            actor = event.actor
            action = event.action
            payload = event.payload
            event_id = event.event_id
            occurred_at = event.occurred_at
            if event.session_id:
                # Redis 故障时，仅少量错误/拒绝日志走同步降级；顺带保持原滑动续期语义。
                get_session(event.session_id, db=db, touch=True)
        db.add(
            AuditLog(
                event_id=event_id,
                actor=actor,
                action=action,
                payload=payload,
                **(
                    {"ts": datetime.fromisoformat(occurred_at)}
                    if occurred_at
                    else {}
                ),
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - audit must never replace business response
        db.rollback()
        logger.warning("请求审计同步兜底写入失败，已静默降级：%s", exc)
    finally:
        if gen is not None:
            try:
                next(gen)
            except StopIteration:
                pass
        else:
            db.close()


async def _write_request_audit(
    request: Request,
    body: bytes,
    status: int,
    elapsed_ms: int,
    error: str | None = None,
) -> None:
    if not request.url.path.startswith("/api/"):
        return
    if not request_audit_buffer.enabled:
        _write_request_audit_sync(request, body, status, elapsed_ms, error)
        return

    ident, session_id = _queued_audit_identity(request, status)
    action, category = _request_action(status, bool(ident.get("loggedIn")))
    event = RequestAuditEvent.create(
        actor=str(ident.get("userid") or "anonymous"),
        action=action,
        payload=_request_audit_payload(
            request,
            body,
            status,
            elapsed_ms,
            category,
            str(ident.get("role") or ""),
            error,
        ),
        session_id=session_id,
    )
    if await request_audit_buffer.enqueue(event):
        return
    # Ordinary 2xx/3xx telemetry is intentionally shed during a Redis incident. Errors,
    # rejected requests and permission failures are rare and retain a synchronous fallback.
    if action != "request_ok":
        _write_request_audit_sync(
            request,
            body,
            status,
            elapsed_ms,
            error,
            event=event,
        )


@app.middleware("http")
async def request_audit_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path in _REQUEST_AUDIT_EXCLUDED_PATHS:
        return await call_next(request)
    started = time.perf_counter()
    body = await request.body()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    audited_request = Request(request.scope, receive)
    try:
        response = await call_next(audited_request)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        await _write_request_audit(
            audited_request,
            body,
            500,
            elapsed_ms,
            error=exc.__class__.__name__,
        )
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    await _write_request_audit(audited_request, body, response.status_code, elapsed_ms)
    return response


app.include_router(api.router)
# 无条件挂载：/h5/test/auth 由签名令牌（FDC_TEST_LOGIN_SECRET）自门控，无密钥即 403，生产挂着无风险，
# 且线上 FDC_TEST_MODE=0 时仍可签名登录（本地启动器替代应用内身份切换）。
app.include_router(test_mode.router, prefix="/h5")

# 静态站点为可选组件：未配置路径时使用不存在的占位目录，相关路由返回“前端未构建”，
# 不影响纯 API 部署。Docker/Compose 会显式设置 STATIC_ADMIN / STATIC_H5。
admin_dist = Path(settings.static_admin) if settings.static_admin else Path("/nonexistent/admin-dist")
h5_dist = Path(settings.static_h5) if settings.static_h5 else Path("/nonexistent/h5-dist")

# index.html 不缓存：每次都向服务器校验，确保发布新版后浏览器立即拿到新 bundle（无需手动硬刷新）。
# 带 hash 的 /assets/* 仍可被长期缓存（内容哈希变了文件名就变，天然安全）。
_INDEX_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def _index_response(index: Path) -> FileResponse:
    return FileResponse(index, headers=_INDEX_HEADERS)


if admin_dist.exists():
    app.mount("/assets", StaticFiles(directory=admin_dist / "assets"), name="admin-assets")
if h5_dist.exists():
    app.mount("/h5/assets", StaticFiles(directory=h5_dist / "assets"), name="h5-assets")


@app.get("/")
async def admin_spa():
    index = admin_dist / "index.html"
    if index.exists():
        return _index_response(index)
    return JSONResponse({"message": "admin not built"})


@app.get("/h5")
@app.get("/h5/")
async def h5_spa():
    index = h5_dist / "index.html"
    if index.exists():
        return _index_response(index)
    return JSONResponse({"message": "h5 not built"})


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str, request: Request):
    if full_path.startswith("api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    if full_path.startswith("h5/"):
        index = h5_dist / "index.html"
        if index.exists():
            return _index_response(index)
    index = admin_dist / "index.html"
    if index.exists() and not full_path.startswith("h5"):
        return _index_response(index)
    return JSONResponse({"error": "not found"}, status_code=404)
