"""统一同步历史。计数在实际读取/提交处采集，嵌套步骤不重复生成任务记录。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import logging

from app.models import SyncExecutionLog, DingtalkEmployeeSyncRun

logger = logging.getLogger(__name__)


def _rollback(db):
    try:
        db.rollback()
    except Exception:
        logger.exception("同步日志会话回滚失败")

_current = ContextVar("sync_execution", default=None)
KINDS = {
    "attendance_auto": "考勤自动同步",
    "attendance_pull": "原始打卡拉取并转劳勤",
    "attendance_raw": "原始打卡拉取",
    "attendance_result": "考勤结果拉取",
    "attendance_transform": "原始打卡转劳勤",
    "attendance_one": "单人重拉",
    "attendance_bulk": "异常批量补拉",
    "directory": "通讯录拉取",
    "employee_difference": "员工差异同步",
    "employee_transform": "通讯录转员工",
    "employee_import": "单人导入员工",
}
COUNT_LABELS = {
    "resultInserted": "考勤结果新增条数", "resultUpdated": "考勤结果更新条数",
    "resultWithdrawn": "源端撤回条数", "resultRestored": "源端恢复条数",
    "rawFetched": "原始打卡拉取条数", "rawUpserted": "原始打卡入库条数",
    "resultFetched": "考勤结果拉取条数", "resultUpserted": "考勤结果入库条数",
    "punchInserted": "劳勤新增条数", "punchSkipped": "劳勤重复跳过条数",
    "departmentFetched": "部门拉取条数", "departmentUpserted": "部门入库条数",
    "memberFetched": "人员拉取条数（含跨部门重复）", "memberUpserted": "人员入库人数（去重）",
    "employeeChanged": "员工字段变更次数", "employeeInserted": "新增员工数",
    "employeeUpdated": "更新员工数", "failedDepartments": "失败部门数",
    "bulkRecovered": "补拉恢复员工日数", "bulkUnchanged": "补拉仍异常员工日数",
    "bulkFailed": "补拉失败员工日数", "bulkSkipped": "补拉跳过员工日数",
}


def add_counts(**counts):
    run = _current.get()
    if run:
        for key, value in counts.items():
            run.counts[key] = run.counts.get(key, 0) + int(value or 0)


def mark(status, message):
    run = _current.get()
    if run:
        run.status, run.message = status, str(message or "")[:2000]


class Execution:
    def __init__(self, db, kind, actor, trigger, region=None, date_from=None, date_to=None):
        self.db, self.id = db, None
        self.status, self.message, self.counts = "success", None, {}
        try:
            row = SyncExecutionLog(kind=kind, actor=actor, trigger=trigger, region=region,
                                   date_from=date_from, date_to=date_to)
            db.add(row)
            db.flush()
            self.id = row.id
            db.commit()
        except Exception:
            self.id = None
            _rollback(db)
            logger.exception("创建同步历史失败")

    def finish(self):
        if self.id is None:
            return
        try:
            # 失败的业务事务不得抹去此前已提交的开始记录。
            _rollback(self.db)
            row = self.db.get(SyncExecutionLog, self.id)
            row.status = self.status
            row.message = self.message
            row.counts_json = json.dumps(self.counts, ensure_ascii=False)
            row.finished_at = datetime.now(timezone.utc)
            self.db.commit()
        except Exception:
            _rollback(self.db)
            logger.exception("同步历史收尾失败 log_id=%s", self.id)


@contextmanager
def task(db, kind, actor="admin", trigger="manual", region=None, date_from=None, date_to=None, *, separate=False):
    if _current.get() is not None and not separate:
        yield _current.get()
        return
    run = Execution(db, kind, actor, trigger, region, date_from, date_to)
    token = _current.set(run)
    try:
        yield run
    except BaseException as exc:
        run.status = "failed"
        run.message = str(getattr(exc, "detail", None) or exc)[:2000]
        raise
    finally:
        run.finish()
        _current.reset(token)


def logged_sync(kind, *, employee_run=False, bulk=False, trigger="manual"):
    """包住实际执行函数；worker 的独立会话使用所在模块的 SessionLocal（可密闭测试）。"""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            args_map = signature.bind(*args, **kwargs)
            args_map.apply_defaults()
            values = args_map.arguments
            if _current.get() is not None and not bulk:
                return fn(*args, **kwargs)
            db = values.get("db")
            own = db is None
            try:
                if own:
                    db = fn.__globals__["SessionLocal"]()
            except Exception:
                # 原函数仍须执行自己的异常处理和锁释放。
                logger.exception("无法创建同步日志会话")
                return fn(*args, **kwargs)
            try:
                actor = values.get("actor") or ("scheduler" if trigger == "auto" else "admin")
                region = values.get("region")
                dfrom, dto = values.get("date_from"), values.get("date_to")
                source = trigger
                if employee_run:
                    try:
                        run = db.get(DingtalkEmployeeSyncRun, values["run_id"])
                        actor, source = run.actor, run.trigger
                    except Exception:
                        _rollback(db)
                        logger.exception("读取员工同步日志元数据失败")
                        return fn(*args, **kwargs)
                if bulk:
                    job = values["job"]
                    actor, region = job.actor, job.region
                    source = "auto" if job.schedule_key else "manual"
                    dfrom, dto = job.date_from, job.date_to
                    if job.status in {"done", "failed"}:
                        return fn(*args, **kwargs)
                if kind == "attendance_auto":
                    from app.services.dingtalk_attendance_service import _attendance_month_ranges
                    from zoneinfo import ZoneInfo
                    today = values.get("today") or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                    ranges = _attendance_month_ranges(today)
                    dfrom, dto = ranges[0][0], ranges[-1][1]
                with task(db, kind, actor, source, region, dfrom, dto, separate=bulk) as log:
                    result = fn(*args, **kwargs)
                    if isinstance(result, dict):
                        if result.get("error"):
                            mark("skipped" if "跳过" in result["error"] and "进行中" in result["error"] else "failed", result["error"])
                        elif result.get("skippedLockedRanges") or result.get("narrowedLockedRanges"):
                            mark("partial", "存在封账范围跳过或收窄，详见原有考勤同步日志")
                    try:
                        # 员工差异和补拉具有独立运行 ID，可安全读取这一次执行的结果。
                        db.expire_all()
                        if employee_run:
                            run = db.get(DingtalkEmployeeSyncRun, values["run_id"])
                            if run.status == "error":
                                mark("partial" if log.counts else "failed", run.message)
                            elif run.status == "running":
                                mark("failed", "任务未正常结束，请检查后重试")
                        if bulk:
                            from app.services.attendance_resync_service import status
                            counts = status(db, values["job"])["counts"]
                            add_counts(**{"bulk" + k.title(): counts.get(k, 0) for k in ("recovered", "unchanged", "failed", "skipped")})
                            if counts.get("failed"):
                                mark("partial", f"{counts['failed']} 个员工日补拉失败")
                    except Exception:
                        _rollback(db)
                        logger.exception("读取同步最终状态失败")
                        mark("partial", "任务已返回，但最终状态读取失败，请核对同步页面的进度")
                    return result
            finally:
                if own:
                    try:
                        db.close()
                    except Exception:
                        logger.exception("关闭同步日志会话失败")
        return wrapped
    return decorate


def logged_start(kind):
    """线程未启动的失败/忙碌也留痕；真正开始的任务由执行函数记录。"""
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            def record(status, message):
                # 自动员工同步入口统一处理启动异常，避免同一次失败记两条。
                if values.get("trigger") == "auto":
                    return
                with task(values["db"], kind, values.get("actor") or "admin",
                          region=values.get("region"), date_from=values.get("date_from"),
                          date_to=values.get("date_to")):
                    mark(status, message)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                record("failed", str(exc))
                raise
            if isinstance(result, dict) and result.get("alreadyRunning"):
                record("skipped", "已有同步任务执行中，本次未启动")
            return result
        return wrapped
    return decorate
