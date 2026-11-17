"""持久化逐员工日补拉队列；与原始同步、转换及单人重拉共用租约。"""
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from app.db import SessionLocal
from app.services.sync_log_service import logged_start, logged_sync
from app.models import AttendanceResyncJob as Job, AttendanceResyncItem as Item, ReconResult, AuditLog, Furnace, Employee
from app.services import dingtalk_attendance_service as sync
from app.services.anomaly_service import get_anomalies, furnace_ids_for_emp_on_date
from app.services.monthly_export_lock_service import assert_month_range_unlocked
from app.services.recon_service import run_recon_for_date
from app.services.report_jobs import background_report_reads

logger = logging.getLogger(__name__)


class BusyError(RuntimeError):
    pass


@contextmanager
def task_lease(db):
    if not sync._transform_lock.acquire(blocking=False):
        raise BusyError("已有考勤拉取或转换任务进行中，请稍后重试")
    token = None
    try:
        token = sync._claim_task_lease(db)
        if token is None:
            raise BusyError("已有考勤拉取或转换任务进行中，请稍后重试")
        yield token
    finally:
        try:
            db.rollback()
            if token:
                sync._release_task_lease(db, token)
        finally:
            sync._transform_lock.release()


def refresh_employee_day(db, emp, date, token, guard=None):
    """页面单人、批量、自动补拉统一入口。调用方负责身份/日期/归属校验。"""
    def check():
        sync._heartbeat_task_lease(db, token)
        if guard:
            guard()
        d = datetime.strptime(date, "%Y-%m-%d").date()
        fids = furnace_ids_for_emp_on_date(db, emp, date)
        if not fids:
            raise ValueError("员工当天已无工作点记录")
        assert_month_range_unlocked(db, (d-timedelta(days=1)).isoformat(),
                                    (d+timedelta(days=1)).isoformat(), fids)
    check()
    raw = sync.sync_range(db, date, date, codes=[emp])
    check()
    result = sync.pull_attendance_result_range(db, date, date, codes=[emp])
    check()
    transformed = sync.transform_range(db, date, date, codes=[emp])
    return raw, result, transformed


def _snapshot(db, emp, date, region):
    # 补同步是显式写入流程。只物化本员工日并立即提交，不依赖列表查询的副作用。
    run_recon_for_date(db, date, employee_ids={emp})
    # 只计算这一天的工作台语义，包含加班/补录问题；取相同员工日的所有行。
    with background_report_reads():
        rows = get_anomalies(db, date, filters={"region": region} if region else {},
                             paginate=False, employee_id=emp)["items"]
    rows = [r for r in rows if r["empId"] == emp]
    rr = db.scalar(select(ReconResult).where(ReconResult.emp_id == emp, ReconResult.date == date))
    protected = rr is None or rr.hr_by is not None or rr.chief_by is not None
    employee = db.scalar(select(Employee).where(Employee.emp_id == emp))
    protected = protected or employee is None or employee.deleted
    if region:
        fids = furnace_ids_for_emp_on_date(db, emp, date)
        protected = protected or not fids or any(
            f.region != region for f in db.scalars(select(Furnace).where(Furnace.id.in_(fids))))
    return {"eligible": bool(rows) and not protected,
            "pending": any(r["validKind"] == "自动无效" for r in rows),
            "rows": [{k: r.get(k) for k in ("status", "validKind", "attendanceResultWorkHours", "rowKind")}
                     for r in rows]}


def create_job(db, date_from, date_to, actor, region=None, schedule_key=None, *, defer=False):
    for d in (date_from, date_to):
        sync._validate_date(d)
    if date_to < date_from or (datetime.fromisoformat(date_to)-datetime.fromisoformat(date_from)).days > 62:
        raise ValueError("请选择不超过 63 天的有效日期范围")
    if schedule_key:
        existing = db.scalar(select(Job).where(Job.schedule_key == schedule_key))
        if existing and existing.status != "failed":
            return existing
        if existing:
            # Keep failed history, but let this schedule create a fresh attempt.
            existing.schedule_key = None
            db.flush()
    job = Job(id=str(uuid.uuid4()), actor=actor, region=region, date_from=date_from,
              date_to=date_to, schedule_key=schedule_key, status="preparing")
    db.add(job)
    db.commit()
    if not defer:
        _prepare_job(db, job)
    return job


def _prepare_job(db, job, token=None):
    try:
        # Bound memory by day, independent of the interactive report row budget.
        # Commit each day so lease loss can resume without duplicate queue items.
        for day in sync._date_list(job.date_from, job.date_to):
            if token:
                sync._heartbeat_task_lease(db, token)
            with background_report_reads():
                rows = get_anomalies(db, day,
                    filters={"region": job.region, "validKind": "自动无效"}, paginate=False)["items"]
            employee_ids = {r["empId"] for r in rows}
            if employee_ids:
                run_recon_for_date(db, day, employee_ids=employee_ids)
            protected = {r.emp_id for r in db.scalars(select(ReconResult).where(
                ReconResult.date == day, ReconResult.emp_id.in_(employee_ids)))
                if r.hr_by is not None or r.chief_by is not None}
            existing = set(db.scalars(select(Item.emp_id).where(Item.job_id == job.id, Item.date == day)))
            for emp in sorted(employee_ids - protected - existing):
                db.add(Item(job_id=job.id, emp_id=emp, date=day))
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
        job.status = "pending"
        db.commit()
    except sync.TaskLeaseLost:
        db.rollback()
        raise
    except Exception as exc:
        _record_job_error(db, job, exc, preparing=True)
        raise


def _record_job_error(db, job, exc, *, preparing=False):
    db.rollback()
    if preparing or job.status in {"preparing", "failed"}:
        job.status = "failed"
        job.finished_at = datetime.now(timezone.utc)
    else:
        job.status = "interrupted"
    job.error = str(exc)
    job.updated_at = datetime.now(timezone.utc)
    db.commit()


def _transient(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if type(exc).__module__.startswith("pymysql") and getattr(exc, "args", (None,))[0] in (2002,2003,2006,2013):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


@logged_sync("attendance_bulk", bulk=True)
def run_job(db, job, token, processed=None):
    if job.status in {"done", "failed"}:
        return
    sync._heartbeat_task_lease(db, token)
    if job.status == "preparing":
        _prepare_job(db, job, token)
    job.status, job.error = "running", None
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    ids = list(db.scalars(select(Item.id).where(Item.job_id == job.id,
        Item.status.in_(["pending", "running"])).order_by(Item.date, Item.emp_id)))
    for item_id in ids:
        sync._heartbeat_task_lease(db, token)
        item = db.get(Item, item_id)
        item.status = "running"
        db.commit()
        try:
            before = _snapshot(db, item.emp_id, item.date, job.region)
            if item.before_value is None:
                item.before_value = json.dumps(before, ensure_ascii=False)
            db.commit()
            def guard():
                current = _snapshot(db, item.emp_id, item.date, job.region)
                if not current["eligible"]:
                    raise ValueError("员工日已人工处理或已不在当前管理范围，跳过")
            key = (item.emp_id, item.date)
            if processed is not None and key in processed:
                item.status, item.error = "skipped", "本轮定时任务已重拉该员工日"
            elif not before["eligible"] or not before["pending"]:
                item.status, item.error = "skipped", "已处理或已恢复正常"
            else:
                if processed is not None:
                    processed.add(key)
                while True:
                    if item.attempts >= 3:
                        raise RuntimeError("已达到最多 3 次尝试，请检查错误后重新发起任务")
                    item.attempts += 1
                    db.commit()
                    try:
                        refresh_employee_day(db, item.emp_id, item.date, token, guard)
                        break
                    except sync.TaskLeaseLost:
                        raise
                    except Exception as exc:
                        db.rollback()
                        if item.attempts >= 3 or not _transient(exc):
                            raise
                        time.sleep(min(item.attempts, 2))
                after = _snapshot(db, item.emp_id, item.date, job.region)
                item.after_value = json.dumps(after, ensure_ascii=False)
                item.status = "unchanged" if after["pending"] else "recovered"
        except sync.TaskLeaseLost:
            db.rollback()
            raise
        except Exception as exc:
            from app.errors import DomainError
            db.rollback()
            item.status = "skipped" if isinstance(exc, (ValueError, DomainError)) else "failed"
            item.error = str(exc)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
    job.status = "done"
    job.finished_at = job.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(actor=job.actor, action="attendance_bulk_resync", payload=json.dumps(
        {"jobId": job.id, "region": job.region, "dateFrom": job.date_from, "dateTo": job.date_to,
         "trigger": "auto" if job.schedule_key else "manual", "counts": status(db, job)["counts"]}, ensure_ascii=False)))
    db.commit()


def status(db, job):
    counts = dict(db.execute(select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)).all())
    total = sum(counts.values())
    return {"id": job.id, "status": job.status, "dateFrom": job.date_from, "dateTo": job.date_to,
            "region": job.region, "error": job.error, "counts": counts, "total": total,
            "done": total-counts.get("pending",0)-counts.get("running",0),
            "startedAt": job.created_at.isoformat(),
            "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
            "busy": sync._task_lease_active(db)}


def run_daily(db, date_from, date_to, today, token):
    """沿用零点任务租约，先续跑中断队列，再补拉今日仍异常的数据。"""
    sync._heartbeat_task_lease(db, token)
    pending = list(db.scalars(select(Job).where(
        Job.status.in_(["preparing", "pending", "running", "interrupted"])
    ).order_by(Job.created_at)))
    processed = set()
    def resume(job):
        try:
            run_job(db, job, token, processed)
        except sync.TaskLeaseLost:
            raise
        except Exception as exc:
            _record_job_error(db, job, exc)
            logger.exception("批量补拉任务失败，继续处理后续队列 job=%s", job.id)
    for job in pending:
        resume(job)
    job = create_job(db, date_from, date_to, "scheduler", schedule_key=today, defer=True)
    resume(job)
    return status(db, job)


def _worker(job_id, token):
    with SessionLocal() as db:
        try:
            run_job(db, db.get(Job, job_id), token)
        except sync.TaskLeaseLost:
            db.rollback()
            logger.exception("批量补拉租约失效，停止旧任务，由新持有者续跑")
        except Exception as exc:
            db.rollback()
            job = db.get(Job, job_id)
            _record_job_error(db, job, exc)
            logger.exception("批量劳勤补拉中断")
        finally:
            try:
                sync._release_task_lease(db, token)
            finally:
                sync._transform_lock.release()


@logged_start("attendance_bulk")
def start(db, date_from, date_to, actor, region=None):
    if not sync._transform_lock.acquire(blocking=False):
        raise BusyError("已有考勤拉取或转换任务进行中，请稍后重试")
    token = None
    try:
        token = sync._claim_task_lease(db)
        if token is None:
            raise BusyError("已有考勤拉取或转换任务进行中，请稍后重试")
        job = db.scalar(select(Job).where(Job.region == region, Job.date_from == date_from,
            Job.date_to == date_to, Job.status.in_(["preparing","pending","running","interrupted"])).order_by(Job.created_at))
        if job is None:
            job = create_job(db, date_from, date_to, actor, region, defer=True)
        payload = status(db, job)
        threading.Thread(target=_worker, args=(job.id,token), daemon=True).start()
        return payload
    except Exception:
        try:
            db.rollback()
            if token:
                sync._release_task_lease(db, token)
        finally:
            sync._transform_lock.release()
        raise
