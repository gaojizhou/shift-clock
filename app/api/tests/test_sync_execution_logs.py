import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from app.models import SyncExecutionLog, Region, Employee, EmployeeFavorite
from app.services import sync_log_service as logs
from app.services import dingtalk_attendance_service as attendance
from app.services import dingtalk_employee_service as directory
from app.services import employee_difference_sync_service as difference

ADMIN = {"x-test-user": "ADMIN01", "x-test-role": "admin"}
URL = "/api/v1/admin/sync-execution-logs"


def test_failure_keeps_started_record_and_partial_counts(db_session):
    with pytest.raises(RuntimeError):
        with logs.task(db_session, "attendance_pull", "HR01", region="示例厂区A"):
            # 已完成日已入库；后续日期事务回滚不能删除本次历史。
            logs.add_counts(rawFetched=12, rawUpserted=10)
            db_session.rollback()
            raise RuntimeError("第二天源库连接中断")
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.status == "failed"
    assert row.started_at and row.finished_at >= row.started_at
    assert json.loads(row.counts_json) == {"rawFetched": 12, "rawUpserted": 10}
    assert row.message == "第二天源库连接中断"
    assert logs._current.get() is None


def test_nested_steps_are_not_double_counted_and_bulk_is_separate(db_session):
    with logs.task(db_session, "attendance_auto", "scheduler", "auto"):
        logs.add_counts(rawFetched=3)
        with logs.task(db_session, "attendance_result"):
            logs.add_counts(resultFetched=2)
        with logs.task(db_session, "attendance_bulk", "scheduler", "auto", separate=True):
            logs.add_counts(rawFetched=5)
        logs.add_counts(rawFetched=1)
    rows = db_session.scalars(select(SyncExecutionLog).order_by(SyncExecutionLog.id)).all()
    assert len(rows) == 2
    assert json.loads(rows[0].counts_json) == {"rawFetched": 4, "resultFetched": 2}
    assert json.loads(rows[1].counts_json) == {"rawFetched": 5}


def test_auto_job_busy_is_logged_as_skipped(db_session):
    assert attendance._transform_lock.acquire(False)
    try:
        attendance.attendance_sync_job(today="2026-05-10", db=db_session)
    finally:
        attendance._transform_lock.release()
    row = db_session.scalar(select(SyncExecutionLog))
    assert (row.kind, row.trigger, row.actor, row.status) == (
        "attendance_auto", "auto", "scheduler", "skipped")
    assert (row.date_from, row.date_to) == ("2026-04-01", "2026-05-10")


def test_result_pull_worker_failure_is_logged_with_actor(db_session, monkeypatch):
    monkeypatch.setattr(attendance, "SessionLocal", lambda: db_session)
    def fail(*args, **kwargs):
        logs.add_counts(resultFetched=7, resultUpserted=4)
        raise RuntimeError("结果源库中断")
    monkeypatch.setattr(attendance, "pull_attendance_result_range", fail)
    assert attendance._transform_lock.acquire(False)
    attendance._run_attendance_result_pull("2026-05-01", "2026-05-02", actor="HR02")
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.actor == "HR02"
    assert row.status == "failed"
    assert "结果源库中断" in row.message
    assert json.loads(row.counts_json)["resultUpserted"] == 4
    assert not attendance._transform_lock.locked()


def test_directory_partial_result_is_recorded(db_session, monkeypatch):
    monkeypatch.setattr(directory, "SessionLocal", lambda: db_session)
    def pull(db):
        logs.add_counts(memberFetched=6, memberUpserted=4, failedDepartments=1)
        logs.mark("partial", "一个部门失败")
    monkeypatch.setattr(directory, "run_directory_sync_locked", pull)
    assert directory.try_acquire_directory_sync()
    directory._run("ADMIN02")
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.actor == "ADMIN02" and row.status == "partial"
    assert json.loads(row.counts_json)["memberUpserted"] == 4
    assert directory.try_acquire_directory_sync()
    directory.release_directory_sync()


def test_disabled_automatic_employee_sync_is_logged(db_session, monkeypatch):
    monkeypatch.setattr(difference, "SessionLocal", lambda: db_session)
    difference.automatic_sync_job()
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.kind == "employee_difference" and row.trigger == "auto"
    assert row.status == "skipped" and "关闭" in row.message


def test_log_api_filters_counts_pagination_and_region(client, db_session):
    db_session.add_all([
        Region(code="LOG_LOCAL", name="示例厂区A", sort=91),
        Employee(emp_id="LOG_HR", name="日志人事", role="hr"),
        EmployeeFavorite(emp_id="LOG_HR", kind="region", value="示例厂区A"),
    ])
    for i, region in enumerate(["示例厂区A", None, "示例厂区B"]):
        db_session.add(SyncExecutionLog(kind="attendance_pull", trigger="manual", actor="HR01",
            region=region, status="success", started_at=datetime(2026, 5, 1, 1, i, tzinfo=timezone.utc),
            counts_json='{"rawFetched":12,"rawUpserted":10,"resultFetched":2,"resultUpserted":2}'))
    db_session.add(SyncExecutionLog(kind="directory", trigger="manual", actor="ADMIN01", status="success"))
    db_session.commit()
    headers = {"x-test-user": "LOG_HR", "x-test-role": "hr"}
    result = client.get(URL, params={"group": "attendance", "pageSize": 1, "dateFrom": "2026-05-01", "dateTo": "2026-05-01"}, headers=headers)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["total"] == 2 and len(data["items"]) == 1
    assert data["items"][0]["region"] is None
    assert data["items"][0]["fetched"] == 14 and data["items"][0]["upserted"] == 12
    assert data["items"][0]["startedAt"].endswith("+00:00")
    assert client.get(URL, params={"group": "employee"}, headers=headers).status_code == 403
    assert client.get(URL, params={"group": "employee"}, headers=ADMIN).json()["total"] == 1
    assert client.get(URL, params={"group": "attendance", "trigger": "auto"}, headers=ADMIN).json()["total"] == 0
    assert client.get(URL, params={"status": "failed"}, headers=ADMIN).json()["total"] == 0
    assert client.get(URL, params={"kind": "directory"}, headers=ADMIN).status_code == 422
    assert client.get(URL, params={"dateFrom": "bad"}, headers=ADMIN).status_code == 422
    assert client.get(URL, params={"pageSize": 100000}, headers=ADMIN).status_code == 422
    assert client.get(URL, headers={"x-test-user": "T0001", "x-test-role": "worker"}).status_code == 403


def test_real_source_upserts_count_fetched_and_committed_rows_separately(db_session, monkeypatch):
    from sqlalchemy.dialects.sqlite import insert
    from app.models import DingtalkAttendanceRaw, ExternalAttendanceResultRaw
    monkeypatch.setattr(attendance, "pg_insert", insert)
    raw = {"code": "T0001", "att_time": datetime(2026, 5, 1, 8), "direct": "进", "src": "钉钉"}
    result = {"user_code": "T0001", "kq_date": "2026-05-01", "cqgs": "8"}
    with logs.task(db_session, "attendance_pull", "HR01"):
        attendance._upsert_raw_rows(db_session, [tuple(raw.get(k) for k in attendance._VIEW_COLS)] * 2)
        attendance._upsert_result_rows(db_session, [tuple(result.get(k) for k in attendance._ATT_RESULT_COLS)] * 3)
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.status == "success"
    assert json.loads(row.counts_json) == {
        "rawFetched": 2, "rawUpserted": 1, "resultFetched": 3, "resultUpserted": 1,
    }
    assert len(db_session.scalars(select(DingtalkAttendanceRaw)).all()) == 1
    assert len(db_session.scalars(select(ExternalAttendanceResultRaw)).all()) == 1


def test_background_thread_start_failure_is_logged_and_unlocks(db_session, monkeypatch):
    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            raise RuntimeError("无法启动线程")
    monkeypatch.setattr(directory.threading, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="无法启动线程"):
        directory.start_sync(db_session, actor="ADMIN03")
    row = db_session.scalar(select(SyncExecutionLog))
    assert row.actor == "ADMIN03" and row.status == "failed"
    assert directory.try_acquire_directory_sync()
    directory.release_directory_sync()
