"""Bound report work before loading data; duplicate previews share only in-flight results."""

import hashlib
import inspect
import json
import logging
import os
import time
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import event, select, or_
from sqlalchemy.exc import DBAPIError

from app.config import settings

logger = logging.getLogger(__name__)
PAUSE_FILE = Path("/app/api/.heavy_reports_paused")


class ReportLimitExceeded(Exception):
    pass


class ReportTimedOut(Exception):
    pass


@dataclass
class ReportBudget:
    deadline: float
    max_rows: int
    rows: int = 0
    read_only: bool = True
    output_cells: int = 0


_budget: ContextVar[ReportBudget | None] = ContextVar("report_budget", default=None)
_background_reads: ContextVar[bool] = ContextVar("background_report_reads", default=False)


@contextmanager
def background_report_reads():
    """Explicit opt-out for bounded background batches, never for HTTP reports."""
    budget_token = _budget.set(None)
    token = _background_reads.set(True)
    try:
        yield
    finally:
        _background_reads.reset(token)
        _budget.reset(budget_token)


def check_report_deadline():
    budget = _budget.get()
    if budget and time.monotonic() >= budget.deadline:
        raise ReportTimedOut("报表计算超时，请缩小工作点或日期范围后重试")


def bounded_rows(db, statement, *, scalars=True):
    check_report_deadline()
    budget = _budget.get()
    if budget is None and _background_reads.get():
        result = db.execute(statement)
        return list(result.scalars() if scalars else result)
    remaining = budget.max_rows - budget.rows if budget else settings.fdc_report_max_source_rows
    # LIMIT applies in SQL, so a too-large result is not materialized before rejection.
    result = db.execute(statement.limit(remaining + 1))
    rows = list(result.scalars() if scalars else result)
    if len(rows) > remaining:
        raise ReportLimitExceeded("本次报表数据量过大，请缩小工作点或日期范围后重试")
    if budget:
        budget.rows += len(rows)
    check_report_deadline()
    return rows


def prepare_report_audit(db):
    """End the read-only transaction before writing the download audit record."""
    check_report_deadline()
    db.rollback()
    budget = _budget.get()
    if budget:
        budget.read_only = False


def append_report_row(worksheet, row):
    check_report_deadline()
    budget = _budget.get()
    if budget:
        budget.output_cells += len(row)
        if budget.output_cells > settings.fdc_report_max_output_cells:
            raise ReportLimitExceeded("导出单元格过多，请缩小范围或分工作表下载")
    worksheet.append(row)


def _busy():
    return HTTPException(429, "报表正在计算，请稍后重试", headers={"Retry-After": "2"})


def _resident_memory_mb():
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return round(pages * os.sysconf("SC_PAGE_SIZE") / 1024**2, 1)
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class _Running:
    future: Future
    name: str
    started: float
    followers: int = 0
    requested_at: str = ""
    actor: dict = field(default_factory=dict)
    task: str = ""


class ReportJobs:
    def __init__(self):
        self._lock = Lock()
        self._running: dict[str, _Running] = {}
        self._durations = deque(maxlen=256)
        self.completed = self.failed = self.rejected = self.coalesced = 0

    def run(self, key, name, compute, *, coalesce=False, release_follower=None, actor=None, task=None):
        with self._lock:
            running = self._running.get(key) if coalesce else None
            if running:
                if running.followers >= settings.fdc_report_max_followers:
                    self.rejected += 1
                    raise _busy()
                running.followers += 1
                self.coalesced += 1
                leader = False
            else:
                if len(self._running) >= settings.fdc_report_concurrency:
                    self.rejected += 1
                    raise _busy()
                # Non-coalesced downloads each retain their own audit event.
                if not coalesce:
                    key = f"{key}:{time.monotonic_ns()}"
                running = _Running(Future(), name, time.monotonic(),
                    requested_at=datetime.now(timezone.utc).isoformat(),
                    actor=dict(actor or {}), task=task or name)
                self._running[key] = running
                leader = True
        if not leader:
            try:
                if release_follower:
                    release_follower()  # Never hold an authentication DB connection while waiting.
                return running.future.result(timeout=settings.fdc_report_follower_timeout_seconds)
            except FutureTimeout:
                if running.future.done():
                    return running.future.result()
                with self._lock:
                    self.rejected += 1
                raise _busy() from None
            finally:
                with self._lock:
                    running.followers -= 1
        try:
            result = compute()
            running.future.set_result(result)
            with self._lock:
                self.completed += 1
            return result
        except BaseException as exc:
            running.future.set_exception(exc)
            with self._lock:
                self.failed += 1
            raise
        finally:
            elapsed = time.monotonic() - running.started
            with self._lock:
                self._durations.append(elapsed)
                del self._running[key]
            logger.info("report_finished endpoint=%s duration_ms=%d", name, elapsed * 1000)

    def activity(self):
        """页面联络信息；不暴露查询参数、员工报表内容或会话凭据。"""
        with self._lock:
            return {
                "capacity": settings.fdc_report_concurrency,
                "paused": settings.fdc_reports_paused or PAUSE_FILE.exists(),
                "items": [{"userId": r.actor.get("userId", ""),
                           "userName": r.actor.get("userName", "") or r.actor.get("userId", "未知用户"),
                           "task": r.task, "requestedAt": r.requested_at,
                           "elapsedSeconds": round(time.monotonic() - r.started, 1)}
                          for r in self._running.values()],
            }

    def diagnostics(self):
        with self._lock:
            durations = sorted(self._durations)
            return {
                "scope": "current_process",
                "paused": settings.fdc_reports_paused or PAUSE_FILE.exists(),
                "capacity": settings.fdc_report_concurrency,
                "active": len(self._running),
                "inFlight": [
                    {
                        "endpoint": r.name,
                        "seconds": round(time.monotonic() - r.started, 2),
                        "followers": r.followers,
                    }
                    for r in self._running.values()
                ],
                "completed": self.completed,
                "failed": self.failed,
                "rejected": self.rejected,
                "coalesced": self.coalesced,
                "recentCount": len(durations),
                "recentP95Ms": round(
                    durations[max(0, (len(durations) * 95 + 99) // 100 - 1)] * 1000
                )
                if durations
                else None,
                "timeoutSeconds": settings.fdc_report_timeout_seconds,
                "maxSourceRows": settings.fdc_report_max_source_rows,
                "maxOutputCells": settings.fdc_report_max_output_cells,
                "residentMemoryMb": _resident_memory_mb(),
            }


report_jobs = ReportJobs()


def _json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def report_endpoint(function=None, *, read_only=True, scope_key=None):
    """Use after FastAPI authentication/validation; same-user previews may share work."""
    if function is None:
        return lambda target: report_endpoint(target, read_only=read_only, scope_key=scope_key)
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        params = bound.arguments
        db = params["db"]
        if settings.fdc_reports_paused or PAUSE_FILE.exists():
            raise HTTPException(
                503, "报表与劳勤列表维护中，请稍后重试", headers={"Retry-After": "300"}
            )
        # Include full authorization identity and every supplied parameter. No results
        # are cached after completion, across users, or across authorization changes.
        identity = {k: _json_value(v) for k, v in params.items() if k != "db"}
        if scope_key:
            identity["_authorization_scope"] = scope_key(db, params["user"])
        key = hashlib.sha256(
            json.dumps(
                [function.__name__, identity], sort_keys=True, ensure_ascii=False, default=str
            ).encode()
        ).hexdigest()
        body = params.get("body")
        preview = (
            function.__name__ in {"export_sheet_preview", "anomalies", "export_absence_overrides_get"}
            or params.get("format") == "meta"
            or getattr(body, "format", None) == "meta"
        )

        def compute():
            db.rollback()  # Authentication reads must not keep a transaction open.
            budget = ReportBudget(
                time.monotonic() + settings.fdc_report_timeout_seconds,
                settings.fdc_report_max_source_rows,
                read_only=read_only,
            )
            token = _budget.set(budget)

            def configure(_session, _transaction, connection):
                if connection.dialect.name != "postgresql":
                    return
                if budget.read_only:
                    connection.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                connection.exec_driver_sql(
                    f"SET LOCAL lock_timeout = '{settings.fdc_report_lock_timeout_ms}ms'"
                )
                remaining_ms = max(1, int((budget.deadline - time.monotonic()) * 1000))
                timeout_ms = min(settings.fdc_report_statement_timeout_ms, remaining_ms)
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
                # Pure Python calculation is idle from PostgreSQL's perspective.
                # Keep the session alive through the remaining compute budget.
                idle_ms = max(settings.fdc_report_idle_transaction_timeout_ms, remaining_ms + 1000)
                connection.exec_driver_sql(
                    f"SET LOCAL idle_in_transaction_session_timeout = '{idle_ms}ms'"
                )

            event.listen(db, "after_begin", configure)
            try:
                result = function(*args, **kwargs)
                check_report_deadline()
                return result
            except ReportLimitExceeded as exc:
                raise HTTPException(422, str(exc)) from exc
            except ReportTimedOut as exc:
                raise HTTPException(504, str(exc)) from exc
            except DBAPIError as exc:
                if getattr(exc.orig, "pgcode", None) in {"57014", "55P03", "25P03"}:
                    raise HTTPException(504, "报表查询超时，请缩小范围后重试") from exc
                if exc.connection_invalidated:
                    raise HTTPException(503, "报表数据库连接已中断，请稍后重试") from exc
                raise
            finally:
                try:
                    db.rollback()
                finally:
                    event.remove(db, "after_begin", configure)
                    _budget.reset(token)

        from app.models import Employee
        userid = str(params["user"].get("userid") or "")
        employee_name = db.scalar(select(Employee.name).where(
            or_(Employee.emp_id == userid, Employee.dd_user_id == userid)
        ).limit(1)) if userid else None
        labels = {
            "export_absence_overrides_get": "读取手动旷工及可填上限",
            "export_absence_overrides_put": "保存手动旷工",
            "export_data": "生成合并报表", "export_sheet": "下载工作表",
            "export_sheet_preview": "生成工作表预览",
            "export_monthly_hours": "计算班次有效工时",
            "export_overtime_detail": "计算加班明细", "anomalies": "查询劳勤工作台",
        }
        task = labels.get(function.__name__, "报表计算")
        sheet = getattr(body, "sheet", None)
        if sheet:
            task += " · " + str(sheet)[:64]
        return report_jobs.run(
            key, function.__name__, compute, coalesce=preview, release_follower=db.rollback,
            actor={"userId": userid, "userName": employee_name or userid}, task=task
        )

    return wrapped
