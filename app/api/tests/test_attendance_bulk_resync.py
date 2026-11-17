"""批量补拉：真实 SQLite 对账与队列，劳勤外部调用全部替身。"""
import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models import AttendanceResyncJob as Job, AttendanceResyncItem as Item
from app.models import ReconResult, ExternalAttendanceResultRaw, Furnace, AuditLog
from app.services import attendance_resync_service as bulk
from app.services import dingtalk_attendance_service as sync
from app.services.monthly_export_lock_service import set_month_locks
from test_attendance_resync_one import _attendance, _patches
from test_anomaly_sm import _seed_pair

DAY = "2026-05-10"
ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}


def test_real_reconciliation_recovers_missing_result_and_keeps_other_anomaly(db_session):
    _seed_pair(db_session, DAY)
    source = db_session.scalar(select(ExternalAttendanceResultRaw).where(
        ExternalAttendanceResultRaw.user_code == "T0001"))
    source.cqgs = None
    db_session.commit()
    job = bulk.create_job(db_session, DAY, DAY, "admin")
    calls = []

    def result(db, start, end, codes=None):
        calls.append(codes)
        if codes == ["T0001"]:
            source.cqgs = "8.00000"
            db.commit()
        return {}

    with patch.object(sync, "sync_range", return_value={}), \
         patch.object(sync, "pull_attendance_result_range", result), \
         patch.object(sync, "transform_range", return_value={}):
        with bulk.task_lease(db_session) as token:
            bulk.run_job(db_session, job, token)
    assert calls == [["T0001"], ["T0003"]]
    assert bulk.status(db_session, job)["counts"] == {"recovered": 1, "unchanged": 1}
    item = db_session.scalar(select(Item).where(Item.emp_id == "T0001"))
    assert json.loads(item.after_value)["rows"][0]["attendanceResultWorkHours"] == "8.00000"
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == "attendance_bulk_resync"))


def test_selection_deduplicates_employee_day_and_excludes_manual(db_session):
    _attendance(db_session, "A", DAY)
    _attendance(db_session, "B", DAY)
    bulk._snapshot(db_session, "B", DAY, None)
    rr = db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "B"))
    rr.hr_by = "hr"
    db_session.commit()
    rows = [{"empId": e, "date": DAY} for e in ["A", "A", "B"]]
    with patch.object(bulk, "get_anomalies", return_value={"items": rows}):
        job = bulk.create_job(db_session, DAY, DAY, "admin")
    assert [(i.emp_id, i.date) for i in db_session.scalars(select(Item))] == [("A", DAY)]


@pytest.mark.parametrize("protection", ["manual", "locked", "other_region"])
def test_rechecks_protection_before_external_calls(db_session, protection):
    _attendance(db_session, "A", DAY)
    job = bulk.create_job(db_session, DAY, DAY, "admin", region="示例厂区A")
    furnace = db_session.scalar(select(Furnace).where(Furnace.furnace_no == "35号工作点"))
    if protection == "manual":
        rr = db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "A"))
        rr.chief_by = "chief"
    elif protection == "locked":
        set_month_locks(db_session, month="2026-05", furnace_ids=[furnace.id], locked=True, actor="hr")
    else:
        furnace.region = "其他地区"
    db_session.commit()
    with patch.object(sync, "sync_range") as external:
        with bulk.task_lease(db_session) as token:
            bulk.run_job(db_session, job, token)
    external.assert_not_called()
    assert bulk.status(db_session, job)["counts"] == {"skipped": 1}


def test_transient_retry_is_bounded_and_next_employee_continues(db_session):
    for emp in ["A", "B"]:
        _attendance(db_session, emp, DAY)
    job = bulk.create_job(db_session, DAY, DAY, "admin")
    calls = []

    def raw(db, start, end, codes=None):
        calls.append(codes[0])
        if codes == ["A"]:
            raise TimeoutError("source timeout")
        return {}

    with patch.object(sync, "sync_range", raw), \
         patch.object(sync, "pull_attendance_result_range", return_value={}), \
         patch.object(sync, "transform_range", return_value={}), patch.object(bulk.time, "sleep"):
        with bulk.task_lease(db_session) as token:
            bulk.run_job(db_session, job, token)
    assert calls == ["A", "A", "A", "B"]
    assert bulk.status(db_session, job)["counts"] == {"failed": 1, "unchanged": 1}


def test_resume_skips_finished_items_and_respects_persisted_attempts(db_session):
    for emp in ["A", "B", "C"]:
        _attendance(db_session, emp, DAY)
    job = bulk.create_job(db_session, DAY, DAY, "admin")
    items = list(db_session.scalars(select(Item).order_by(Item.emp_id)))
    items[0].status = "unchanged"
    items[1].status, items[1].attempts = "running", 3
    job.status = "interrupted"
    db_session.commit()
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3, bulk.task_lease(db_session) as token:
        bulk.run_job(db_session, job, token)
    assert calls["sync"][2] == ["C"]
    assert items[1].attempts == 3
    assert bulk.status(db_session, job)["counts"] == {"unchanged": 2, "failed": 1}


def test_async_start_prepares_in_worker_and_releases_on_launch_failure(db_session):
    with patch.object(bulk.threading, "Thread") as thread, patch.object(bulk, "get_anomalies") as scan:
        thread.return_value.start.side_effect = RuntimeError("cannot start")
        with pytest.raises(RuntimeError, match="cannot start"):
            bulk.start(db_session, DAY, DAY, "admin")
        scan.assert_not_called()
    assert not sync._task_lease_active(db_session)
    assert not sync._transform_lock.locked()
    job = db_session.scalar(select(Job))
    assert job.status == "preparing"
    with bulk.task_lease(db_session) as token:
        bulk.run_job(db_session, job, token)
    assert job.status == "done"


def test_bulk_and_single_share_mutex(client, db_session):
    _attendance(db_session, "A", DAY)
    with bulk.task_lease(db_session):
        for path, body in [("resync-bulk", {"dateFrom": DAY, "dateTo": DAY}),
                           ("resync-one", {"empId": "A", "date": DAY})]:
            response = client.post("/api/v1/admin/dingtalk-attendance/" + path, json=body, headers=ADMIN)
            assert response.status_code == 409, response.text


def test_other_process_lease_blocks_batch_and_preserves_owner(db_session):
    owner = sync._claim_task_lease(db_session)
    try:
        with pytest.raises(bulk.BusyError):
            bulk.start(db_session, DAY, DAY, "admin")
        sync._heartbeat_task_lease(db_session, owner)
        assert not sync._transform_lock.locked()
        assert db_session.scalar(select(Job)) is None
    finally:
        sync._release_task_lease(db_session, owner)


@pytest.mark.parametrize("start,end", [("bad", DAY), (DAY, "2026-05-01"), ("2026-01-01", DAY)])
def test_bad_range_rejected_without_job(client, db_session, start, end):
    r = client.post("/api/v1/admin/dingtalk-attendance/resync-bulk",
                    json={"dateFrom": start, "dateTo": end}, headers=ADMIN)
    assert r.status_code == 400
    assert db_session.scalar(select(Job)) is None
    assert not sync._task_lease_active(db_session)


def test_daily_schedule_idempotent_and_resumed_day_not_pulled_twice(db_session):
    _attendance(db_session, "A", DAY)
    bulk.create_job(db_session, DAY, DAY, "admin", defer=True)
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3, patch.object(sync, "pull_range", return_value={"synced": {}, "transformed": {}}):
        first = sync.attendance_sync_job(today=DAY, db=db_session)
        second = sync.attendance_sync_job(today=DAY, db=db_session)
    assert first["error"] is None
    assert first["bulkResync"]["counts"] == {"skipped": 1}
    assert first["bulkResync"]["id"] == second["bulkResync"]["id"]
    assert len(list(db_session.scalars(select(Job).where(Job.schedule_key == DAY)))) == 1


def test_bulk_runs_even_when_whole_range_sync_fails(db_session):
    _attendance(db_session, "A", DAY)
    calls = {}
    p1, p2, p3 = _patches(calls)
    with p1, p2, p3, patch.object(sync, "pull_range", side_effect=RuntimeError("whole range failed")):
        result = sync.attendance_sync_job(today=DAY, db=db_session)
    assert result["error"] == "whole range failed"
    assert result["bulkResync"]["counts"] == {"unchanged": 1}
    assert calls["sync"] == (DAY, DAY, ["A"])


def test_scoped_user_cannot_start_or_read_other_regions(client, db_session):
    from app.models import Employee, EmployeeFavorite, Region
    db_session.add_all([
        Region(code="BULK_LOCAL", name="本地", sort=199),
        Employee(emp_id="BULK_HR", name="人事", role="hr"),
        EmployeeFavorite(emp_id="BULK_HR", kind="region", value="本地"),
    ])
    db_session.commit()
    headers = {"x-test-user": "BULK_HR", "x-test-role": "hr"}
    remote = bulk.create_job(db_session, DAY, DAY, "admin", region="外地", defer=True)
    response = client.post("/api/v1/admin/dingtalk-attendance/resync-bulk",
        json={"dateFrom": DAY, "dateTo": DAY, "region": "外地"}, headers=headers)
    assert response.status_code == 403
    response = client.get("/api/v1/admin/dingtalk-attendance/resync-bulk-status",
                          params={"jobId": remote.id}, headers=headers)
    assert response.status_code == 404
    with patch.object(bulk, "start", return_value={"status": "preparing"}) as start:
        response = client.post("/api/v1/admin/dingtalk-attendance/resync-bulk",
                              json={"dateFrom": DAY, "dateTo": DAY}, headers=headers)
    assert response.status_code == 200
    assert start.call_args.args[-1] == "本地"


def test_status_contains_durable_results_and_worker_is_forbidden(client, db_session):
    _attendance(db_session, "A", DAY)
    job = bulk.create_job(db_session, DAY, DAY, "admin")
    response = client.get("/api/v1/admin/dingtalk-attendance/resync-bulk-status",
                          params={"jobId": job.id}, headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["empId"] == "A"
    headers = {"x-test-user": "A", "x-test-role": "worker"}
    assert client.get("/api/v1/admin/dingtalk-attendance/resync-bulk-status", headers=headers).status_code == 403
    assert client.post("/api/v1/admin/dingtalk-attendance/resync-bulk",
        json={"dateFrom": DAY, "dateTo": DAY}, headers=headers).status_code == 403
