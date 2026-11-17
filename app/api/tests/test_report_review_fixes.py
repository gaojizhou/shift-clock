"""Regression cases from PR #57: background jobs, freshness and transaction lifetime."""

import time
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select
from test_anomaly_sm import _seed_pair
from test_attendance_resync_one import _attendance, _patches

from app.config import settings
from app.models import AttendanceResyncItem as Item
from app.models import AttendanceResyncJob as Job
from app.models import AuditLog, Employee, ExternalAttendanceResultRaw, ReconResult
from app.services import attendance_resync_service as bulk
from app.services import dingtalk_attendance_service as sync
from app.services import report_jobs as jobs
from app.services.recon_service import run_recon_for_date

DAY = "2026-05-10"
ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}


def test_background_read_budget_is_explicit_and_restores_interactive_budget(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "fdc_report_max_source_rows", 1)
    token = jobs._budget.set(jobs.ReportBudget(time.monotonic() + 30, 1))
    outer = jobs._budget.get()
    try:
        with pytest.raises(RuntimeError), jobs.background_report_reads():
            assert len(jobs.bounded_rows(db_session, select(Employee))) > 1
            raise RuntimeError("background failure")
        assert jobs._budget.get() is outer
        with pytest.raises(jobs.ReportLimitExceeded):
            jobs.bounded_rows(db_session, select(Employee))
    finally:
        jobs._budget.reset(token)
    with pytest.raises(jobs.ReportLimitExceeded):
        jobs.bounded_rows(db_session, select(Employee))


def test_daily_resync_ignores_interactive_limit_and_prepares_one_day_at_a_time(
    db_session, monkeypatch
):
    for day in ["2026-05-09", DAY]:
        _attendance(db_session, "A", day)
        _attendance(db_session, "B", day)
    monkeypatch.setattr(settings, "fdc_report_max_source_rows", 1)
    days = []
    real = bulk.get_anomalies

    def scan(db, day, **kwargs):
        assert day is not None and "date_from" not in kwargs
        days.append(day)
        return real(db, day, **kwargs)

    calls = {}
    p1, p2, p3 = _patches(calls)
    with (
        p1,
        p2,
        p3,
        patch.object(bulk, "get_anomalies", scan),
        bulk.task_lease(db_session) as token,
    ):
        result = bulk.run_daily(db_session, "2026-05-09", DAY, DAY, token)
    assert result["status"] == "done"
    assert result["total"] == 4 and result["counts"] == {"unchanged": 4}
    assert set(days) == {"2026-05-09", DAY}
    assert jobs._budget.get() is None and not jobs._background_reads.get()


def test_failed_preparation_does_not_poison_daily_queue_or_schedule_key(db_session):
    bad = bulk.create_job(
        db_session, "2026-05-09", "2026-05-09", "scheduler", schedule_key=DAY, defer=True
    )
    original_id = bad.id
    real = bulk.get_anomalies

    def scan(db, day, **kwargs):
        if day == "2026-05-09":
            raise RuntimeError("broken old batch")
        return real(db, day, **kwargs)

    with patch.object(bulk, "get_anomalies", scan), bulk.task_lease(db_session) as token:
        result = bulk.run_daily(db_session, DAY, DAY, DAY, token)
    db_session.refresh(bad)
    assert bad.status == "failed" and bad.error == "broken old batch"
    assert bad.finished_at and bad.schedule_key is None
    assert result["id"] != original_id and result["status"] == "done"
    today = db_session.scalar(select(Job).where(Job.schedule_key == DAY))
    assert today.id == result["id"]
    with (
        patch.object(bulk.threading, "Thread"),
        patch.object(sync, "_claim_task_lease", return_value="token"),
    ):
        # A failed manual range must create a fresh job, not reuse the failed one.
        try:
            payload = bulk.start(db_session, bad.date_from, bad.date_to, "admin")
            assert payload["id"] != original_id
        finally:
            sync._transform_lock.release()


def test_preparation_resume_deduplicates_already_committed_days(db_session):
    _attendance(db_session, "A", DAY)
    job = bulk.create_job(db_session, DAY, DAY, "admin", defer=True)
    db_session.add(Item(job_id=job.id, emp_id="A", date=DAY))
    db_session.commit()
    bulk._prepare_job(db_session, job)
    assert job.status == "pending"
    assert len(list(db_session.scalars(select(Item).where(Item.job_id == job.id)))) == 1


def test_daily_lease_loss_stops_instead_of_starting_new_job(db_session):
    old = bulk.create_job(db_session, DAY, DAY, "admin", defer=True)
    with (
        bulk.task_lease(db_session) as token,
        patch.object(bulk, "run_job", side_effect=sync.TaskLeaseLost("lost")),
        pytest.raises(sync.TaskLeaseLost),
    ):
        bulk.run_daily(db_session, DAY, DAY, DAY, token)
    assert list(db_session.scalars(select(Job.id))) == [old.id]
    assert old.status == "preparing"


def test_calendar_source_limit_is_a_422_including_scan_detail(client, db_session, monkeypatch):
    _seed_pair(db_session, DAY)
    monkeypatch.setattr(settings, "fdc_report_max_source_rows", 1)
    response = client.get(f"/api/v1/attendance?userid=T0001&month={DAY[:7]}", headers=ADMIN)
    assert response.status_code == 422
    assert "缩小" in response.json()["detail"]
    with (
        patch("app.services.attendance_service.ReportSnapshot.for_month"),
        patch(
            "app.services.attendance_service.ScanDetailContext",
            side_effect=jobs.ReportLimitExceeded("scan detail limit"),
        ),
    ):
        response = client.get(f"/api/v1/attendance?userid=T0001&month={DAY[:7]}", headers=ADMIN)
    assert response.status_code == 422 and response.json()["detail"] == "scan detail limit"


@pytest.mark.parametrize("cqgs,expected", [("8.00000", "自动一致"), ("2.00000", "时间错位")])
def test_result_pull_refreshes_persisted_status_before_completion(db_session, cqgs, expected):
    _seed_pair(db_session, DAY)
    source = db_session.scalar(
        select(ExternalAttendanceResultRaw).where(ExternalAttendanceResultRaw.user_code == "T0001")
    )
    source.cqgs = "2.00000" if cqgs == "8.00000" else "8.00000"
    db_session.commit()
    run_recon_for_date(db_session, DAY)
    old_other = db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "T0003"))
    old_other.status = "do not touch"
    db_session.commit()

    observed = []

    def progress(*args):
        row = db_session.scalar(
            select(ReconResult).where(ReconResult.emp_id == "T0001", ReconResult.date == DAY)
        )
        observed.append((row.status, row.hr_status))

    with (
        patch.object(sync, "_mysql_connect", return_value=Mock()),
        patch.object(sync, "_fetch_result_rows", return_value=[tuple({"user_code":"T0001","kq_date":DAY,"cqgs":cqgs}.get(k) for k in sync._ATT_RESULT_COLS)]),
    ):
        sync.pull_attendance_result_range(
            db_session, DAY, DAY, codes=["T0001"], progress_cb=progress
        )
    assert observed[-1] == (expected, "未确认" if expected == "自动一致" else "待值班长确认")
    assert old_other.status == "do not touch"
    from app.models import Furnace
    from app.services.anomaly_service import (
        get_anomalies,
        get_chief_review_queue,
        get_m2_review_queue,
    )

    current = next(r for r in get_anomalies(db_session, DAY)["items"] if r["empId"] == "T0001")
    assert current["status"] == expected
    fid = db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点"))
    for queue in (
        get_chief_review_queue(db_session, chief_emp_id="L0001", date=DAY)["delegated"],
        get_m2_review_queue(db_session, furnace_id=fid, date=DAY),
    ):
        rows = [r for r in queue if r["empId"] == "T0001"]
        assert bool(rows) == (expected != "自动一致")
        if rows:
            assert rows[0]["status"] == expected


@pytest.mark.parametrize(
    "path,body,builder,action",
    [
        (
            "/export",
            {"month": DAY[:7], "furnaces": ["35号工作点"], "format": "xlsx"},
            "build_export_xlsx",
            "export",
        ),
        (
            "/export/sheet",
            {"month": DAY[:7], "furnaces": ["35号工作点"], "sheet": "输出给薪资"},
            "build_export_sheet_xlsx",
            "export_sheet",
        ),
        (
            f"/export/monthly-hours?month={DAY[:7]}&format=flat",
            None,
            "build_monthly_hours_xlsx",
            "export_monthly_hours",
        ),
        (
            f"/export/monthly-hours?month={DAY[:7]}",
            None,
            "build_monthly_calendar_xlsx",
            "export_monthly_hours",
        ),
        (
            f"/export/overtime-detail?month={DAY[:7]}",
            None,
            "build_overtime_detail_xlsx",
            "export_overtime_detail",
        ),
    ],
)
def test_all_download_builders_run_without_db_transaction(
    client, db_session, path, body, builder, action
):
    _seed_pair(db_session, DAY)

    def build(*args):
        assert not db_session.in_transaction()
        assert jobs._budget.get() is not None  # Compute/cell guards remain active.
        assert not jobs._budget.get().read_only
        return b"verified xlsx boundary"

    module = "app.routers.api" if builder == "build_export_xlsx" else "app.services.export_service"
    with patch(f"{module}.{builder}", build):
        response = (
            client.get("/api/v1" + path, headers=ADMIN)
            if body is None
            else client.post("/api/v1" + path, json=body, headers=ADMIN)
        )
    assert response.status_code == 200, response.text
    assert db_session.scalar(select(AuditLog).where(AuditLog.action == action)) is not None


def test_idle_timeout_covers_remaining_compute_budget(db_session, monkeypatch):
    listeners = {}
    monkeypatch.setattr(jobs.event, "listen", lambda db, event, fn: listeners.update(configure=fn))
    monkeypatch.setattr(jobs.event, "remove", lambda *args: None)
    monkeypatch.setattr(settings, "fdc_report_idle_transaction_timeout_ms", 15_000)
    monkeypatch.setattr(settings, "fdc_report_timeout_seconds", 60)
    connection = Mock()
    connection.dialect.name = "postgresql"

    @jobs.report_endpoint
    def compute(db, user):
        listeners["configure"](db, None, connection)

    compute(db_session, {})
    statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
    idle = next(s for s in statements if "idle_in_transaction_session_timeout" in s)
    assert int(idle.split("'")[1][:-2]) >= 60_000


@pytest.mark.parametrize("protection", ["manual", "locked"])
def test_result_pull_keeps_manual_decisions_and_month_locks(db_session, protection):
    from app.models import Furnace
    from app.services.monthly_export_lock_service import set_month_locks

    _seed_pair(db_session, DAY)
    run_recon_for_date(db_session, DAY)
    rr = db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "T0001"))
    if protection == "manual":
        rr.hr_by, rr.hr_status = "HR01", "通过"
        rr.chief_by, rr.chief_status = "L0001", "不通过"
        db_session.commit()
    else:
        fid = db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点"))
        set_month_locks(db_session, month=DAY[:7], furnace_ids=[fid], locked=True, actor="admin")
    before = (rr.status, rr.hr_status, rr.hr_by, rr.chief_status, rr.chief_by)

    with (
        patch.object(sync, "_mysql_connect", return_value=Mock()),
        patch.object(sync, "_fetch_result_rows", return_value=[tuple({"user_code":"T0001","kq_date":DAY,"cqgs":"2.00000"}.get(k) for k in sync._ATT_RESULT_COLS)]),
    ):
        sync.pull_attendance_result_range(db_session, DAY, DAY, codes=["T0001"])
    db_session.refresh(rr)
    after = (rr.status, rr.hr_status, rr.hr_by, rr.chief_status, rr.chief_by)
    assert after == before if protection == "locked" else after[1:] == before[1:]


def test_result_pull_preserves_entire_range_if_next_day_fails(db_session):
    earlier = "2026-05-09"
    for day in (earlier, DAY):
        _seed_pair(db_session, day)
        source = db_session.scalar(
            select(ExternalAttendanceResultRaw).where(
                ExternalAttendanceResultRaw.user_code == "T0001",
                ExternalAttendanceResultRaw.kq_date == day,
            )
        )
        source.cqgs = "2.00000"
        db_session.commit()
        run_recon_for_date(db_session, day)

    def fetch(conn, start, end, codes):
        if start == DAY:
            raise RuntimeError("source failed")
        return [tuple({"user_code":"T0001","kq_date":start,"cqgs":"8.00000"}.get(k) for k in sync._ATT_RESULT_COLS)]

    with (
        patch.object(sync, "_mysql_connect", return_value=Mock()),
        patch.object(sync, "_fetch_result_rows", fetch),
        pytest.raises(RuntimeError, match="source failed"),
    ):
        sync.pull_attendance_result_range(db_session, earlier, DAY, codes=["T0001"])
    result = dict(
        db_session.execute(
            select(ReconResult.date, ReconResult.status).where(ReconResult.emp_id == "T0001")
        ).all()
    )
    assert result == {earlier: "时间错位", DAY: "时间错位"}


@pytest.mark.parametrize(
    "path,extra",
    [
        ("/scan", {"natualDate": DAY}),
        ("/makeup", {}),
        ("/makeup/approve", {}),
        ("/makeup/review", {"action": "approve"}),
        ("/attendance/overtime", {"shiftSlot": "早班", "workHours": 4}),
    ],
)
def test_attendance_writes_materialize_only_affected_employee_day(client, db_session, path, extra):
    if path == "/attendance/overtime":
        _attendance(db_session, "T0001", DAY)
    sentinel = ReconResult(emp_id="T0003", date=DAY, status="unrelated sentinel")
    db_session.add(sentinel)
    db_session.commit()
    body = {
        "empId": "T0001",
        "userid": "T0001",
        "date": DAY,
        "region": "示例厂区A",
        "furnace": "35号工作点",
        "squad": "3班",
        "post": "出料工",
        "employType": "正式",
        **extra,
    }
    with patch("app.services.on_duty_service.notify_on_duty"):
        response = client.post("/api/v1" + path, json=body, headers=ADMIN)
    assert response.status_code in (200, 201), response.text
    rr = db_session.scalar(
        select(ReconResult).where(ReconResult.emp_id == "T0001", ReconResult.date == DAY)
    )
    assert rr is not None and rr.hr_status == "待值班长确认"
    db_session.refresh(sentinel)
    assert sentinel.status == "unrelated sentinel"


@pytest.mark.parametrize("by_record", [False, True])
def test_work_hours_change_refreshes_queue_in_same_write(db_session, by_record):
    from app.models import AttendanceRecord
    from app.services.confirm_service import set_work_hours

    _seed_pair(db_session, DAY)
    run_recon_for_date(db_session, DAY)
    record = db_session.scalar(select(AttendanceRecord).where(AttendanceRecord.emp_id == "T0003"))
    result = db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "T0003"))
    assert result.status == "时间错位"
    set_work_hours(
        db_session,
        furnace_no="35号工作点",
        date=DAY,
        emp_id="T0003",
        work_hours=4,
        record_id=record.id if by_record else None,
        region="示例厂区A",
        actor="L0001",
    )
    db_session.refresh(result)
    assert result.status == "自动一致" and result.hr_status == "未确认"
