"""SQL read-only and resource admission tests; no external dependencies."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_export_preview_meta import _seed

from app.config import settings
from app.models import AttendanceRecord, Employee, Furnace, ReconResult
from app.services import report_jobs as jobs
from app.services.anomaly_service import get_anomalies
from app.services.export_service import build_export_meta
from app.services.recon_service import run_recon_for_date
from app.services.report_snapshot import ReportSnapshot

ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}
MONTH, DAY = "2026-05", "2026-05-06"


@contextmanager
def capture_sql(db):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            assert ReconResult.__tablename__ not in statement, statement

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/export", {"month": MONTH, "furnaces": ["35号工作点"], "format": "meta"}),
        ("/export", {"month": MONTH, "furnaces": ["35号工作点"]}),
        ("/export/sheet-preview", {"month": MONTH, "furnaces": ["35号工作点"], "sheet": "输出给薪资"}),
        ("/export/sheet", {"month": MONTH, "furnaces": ["35号工作点"], "sheet": "输出给薪资"}),
        (f"/export/monthly-hours?month={MONTH}&format=meta", None),
        (f"/export/monthly-hours?month={MONTH}&format=flat", None),
        (f"/export/overtime-detail?month={MONTH}&format=meta", None),
        (f"/export/overtime-detail?month={MONTH}", None),
        (f"/export/health-check?month={MONTH}&furnaces=35号工作点", None),
        (f"/anomalies?date={DAY}", None),
    ],
)
def test_report_endpoints_never_issue_reconciliation_dml(client, db_session, path, body):
    _seed(db_session, "T0001", DAY, "出料工", work_hours=12)
    db_session.add(ReconResult(emp_id="T0003", date=DAY, status="时间错位"))
    db_session.commit()
    with capture_sql(db_session):
        response = (
            client.get("/api/v1" + path, headers=ADMIN)
            if body is None
            else client.post("/api/v1" + path, json=body, headers=ADMIN)
        )
    assert response.status_code == 200, response.text
    assert db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "T0001")) is None
    assert (
        db_session.scalar(select(ReconResult).where(ReconResult.emp_id == "T0003")).status
        == "时间错位"
    )


@pytest.mark.parametrize("case", ["normal", "manual", "pending", "deleted", "locked", "overtime"])
def test_virtual_recon_matches_explicit_reconciliation(db_session, case):
    from app.models import MonthlyExportLock

    _seed(db_session, "T0001", DAY, "出料工")
    scan = db_session.scalar(select(AttendanceRecord))
    old = ReconResult(emp_id="T0001", date=DAY, status="时间错位", hr_status="不通过")
    db_session.add_all([old, ReconResult(emp_id="T0003", date=DAY, status="时间错位")])
    if case == "manual":
        old.hr_by, old.chief_by, old.chief_status = "HR01", "L0001", "通过"
    elif case == "pending":
        scan.status, scan.source = "待审", "补录"
    elif case == "deleted":
        db_session.scalar(select(Employee).where(Employee.emp_id == "T0001")).deleted = True
    elif case == "locked":
        db_session.add(MonthlyExportLock(month=MONTH, furnace_id=scan.furnace_id, locked=True))
    elif case == "overtime":
        scan.record_kind, scan.shift_slot = "加班", "中班"
    db_session.commit()
    virtual = ReportSnapshot.for_month(db_session, MONTH, {"T0001", "T0003"}).recons
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    run_recon_for_date(db_session, DAY)
    actual = {(r.emp_id, r.date): r for r in db_session.scalars(select(ReconResult))}
    assert set(virtual) == set(actual)
    for key, row in actual.items():
        for field in (
            "status",
            "hr_status",
            "chief_status",
            "hr_by",
            "chief_by",
            "delegate_furnace_id",
        ):
            assert getattr(virtual[key], field) == getattr(row, field), (case, field)


def test_snapshot_scope_and_narrow_fields(db_session):
    from sqlalchemy.exc import InvalidRequestError

    _seed(db_session, "T0001", DAY, "出料工")
    _seed(db_session, "T0003", DAY, "出料工")
    db_session.expunge_all()
    with capture_sql(db_session) as statements:
        context = ReportSnapshot.for_month(db_session, MONTH, {"T0001"})
    assert {r.emp_id for r in context.records} == {"T0001"}
    assert {key[0] for key in context.results} == {"T0001"}
    assert len(statements) <= 8
    with pytest.raises(InvalidRequestError):
        _ = context.results[("T0001", DAY)].user_name


def test_query_count_does_not_grow_per_employee_or_day(db_session):
    _seed(db_session, "T0001", DAY, "出料工")
    with capture_sql(db_session) as small:
        build_export_meta(db_session, month=MONTH, furnaces=["35号工作点"], emp_id=None, persist=False)
    fid = db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点"))
    for i in range(20):
        emp = f"PERF{i}"
        db_session.add(Employee(emp_id=emp, name=emp))
        for day in range(1, 21):
            db_session.add(
                AttendanceRecord(
                    emp_id=emp,
                    furnace_id=fid,
                    natual_date=f"{MONTH}-{day:02d}",
                    squad="1班",
                    post="出料工",
                    employ_type="正式",
                    source="扫码",
                )
            )
    db_session.commit()
    with capture_sql(db_session) as large:
        meta = build_export_meta(
            db_session, month=MONTH, furnaces=["35号工作点"], emp_id=None, persist=False
        )
    assert len(large) <= len(small) + 2, (len(small), len(large))
    assert len(large) < 35
    with capture_sql(db_session) as anomalies:
        get_anomalies(db_session, None, date_from=MONTH + "-01", date_to=MONTH + "-31")
    assert len(anomalies) < 20
    assert meta["payrollSheet"]["rows"]


def test_source_and_excel_limits(db_session, monkeypatch):
    from openpyxl import Workbook

    token = jobs._budget.set(jobs.ReportBudget(time.monotonic() + 30, max_rows=1))
    try:
        with pytest.raises(jobs.ReportLimitExceeded):
            jobs.bounded_rows(db_session, select(Employee))
        monkeypatch.setattr(settings, "fdc_report_max_output_cells", 2)
        ws = Workbook().active
        with pytest.raises(jobs.ReportLimitExceeded):
            jobs.append_report_row(ws, [1, 2, 3])
        assert ws.cell(1, 1).value is None
    finally:
        jobs._budget.reset(token)


@pytest.mark.parametrize("name", ["魏鑫", "魏"])
def test_anomaly_name_filter_limits_source_rows_before_snapshot(db_session, name):
    furnace_id = db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点"))
    db_session.scalar(select(Employee).where(Employee.emp_id == "T0001")).name = "魏鑫"
    for emp_id in ["T0001", *(f"X{i:04d}" for i in range(50))]:
        db_session.add(AttendanceRecord(
            emp_id=emp_id,
            furnace_id=furnace_id,
            natual_date=DAY,
            squad="1班",
            post="出料工",
            employ_type="正式",
            source="扫码",
        ))
    db_session.commit()

    token = jobs._budget.set(jobs.ReportBudget(time.monotonic() + 30, max_rows=30))
    try:
        with pytest.raises(jobs.ReportLimitExceeded):
            get_anomalies(db_session, DAY)
    finally:
        jobs._budget.reset(token)

    token = jobs._budget.set(jobs.ReportBudget(time.monotonic() + 30, max_rows=30))
    try:
        result = get_anomalies(db_session, DAY, filters={"name": name})
    finally:
        jobs._budget.reset(token)
    assert [row["empId"] for row in result["items"]] == ["T0001"]


def test_capacity_coalescing_bounded_followers_and_cleanup(monkeypatch):
    manager = jobs.ReportJobs()
    monkeypatch.setattr(settings, "fdc_report_concurrency", 1)
    monkeypatch.setattr(settings, "fdc_report_max_followers", 1)
    started, release, following = Event(), Event(), Event()
    calls = []

    def compute():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(manager.run, "same", "preview", compute, coalesce=True)
        assert started.wait(1)
        follower = pool.submit(
            manager.run, "same", "preview", compute, coalesce=True, release_follower=following.set
        )
        try:
            assert following.wait(1)
            for key in ("same", "other"):
                with pytest.raises(HTTPException) as exc:
                    manager.run(key, "preview", compute, coalesce=True)
                assert exc.value.status_code == 429
            assert manager.diagnostics()["active"] == 1
        finally:
            release.set()
        assert leader.result() == follower.result() == {"ok": True}
    assert calls == [1]
    assert manager.diagnostics()["active"] == 0
    assert manager.run("same", "preview", lambda: "fresh", coalesce=True) == "fresh"
    with pytest.raises(ValueError):
        manager.run("bad", "preview", lambda: (_ for _ in ()).throw(ValueError("failure")))
    assert manager.diagnostics()["active"] == 0
    assert manager.diagnostics()["failed"] == 1


def test_follower_timeout_keeps_leader_capacity(monkeypatch):
    manager = jobs.ReportJobs()
    monkeypatch.setattr(settings, "fdc_report_follower_timeout_seconds", 0.01)
    started, release = Event(), Event()

    def compute():
        started.set()
        assert release.wait(2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(manager.run, "same", "preview", compute, coalesce=True)
        assert started.wait(1)
        try:
            with pytest.raises(HTTPException) as exc:
                manager.run("same", "preview", compute, coalesce=True)
            assert exc.value.status_code == 429
            assert manager.diagnostics()["active"] == 1
            assert manager.diagnostics()["inFlight"][0]["followers"] == 0
        finally:
            release.set()
        leader.result()


def test_coalescing_key_includes_user_role_and_filters(db_session, monkeypatch):
    monkeypatch.setattr(jobs, "report_jobs", jobs.ReportJobs())
    started, release = Event(), Event()

    scope = ["地区 A"]

    @jobs.report_endpoint(scope_key=lambda db, user: scope[0])
    def export_sheet_preview(db, user, month):
        started.set()
        assert release.wait(2)
        return {"month": month}

    def run(user, month):
        with Session(bind=db_session.get_bind()) as db:
            return export_sheet_preview(db=db, user=user, month=month)

    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(run, {"userid": "A", "role": "hr"}, MONTH)
        assert started.wait(1)
        try:
            for user, month in [
                ({"userid": "B", "role": "hr"}, MONTH),
                ({"userid": "A", "role": "stat"}, MONTH),
                ({"userid": "A", "role": "hr"}, "2026-04"),
            ]:
                with pytest.raises(HTTPException) as exc:
                    run(user, month)
                assert exc.value.status_code == 429
            scope[0] = "地区 B"
            with pytest.raises(HTTPException) as exc:
                run({"userid": "A", "role": "hr"}, MONTH)
            assert exc.value.status_code == 429
        finally:
            release.set()
        leader.result()


def test_endpoint_timeout_cleans_session_context_capacity(db_session, monkeypatch):
    manager = jobs.ReportJobs()
    monkeypatch.setattr(jobs, "report_jobs", manager)

    @jobs.report_endpoint
    def report(db, user):
        jobs._budget.get().deadline = 0
        jobs.check_report_deadline()

    with pytest.raises(HTTPException) as exc:
        report(db=db_session, user={"userid": "A"})
    assert exc.value.status_code == 504
    assert jobs._budget.get() is None
    assert not db_session.in_transaction()
    assert manager.diagnostics()["active"] == 0
    assert db_session.scalar(select(Employee.emp_id))


def test_pause_keeps_health_available_and_recon_paused(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_reports_paused", True)
    assert client.get("/api/v1/health").status_code == 200
    for path in (
        f"/api/v1/export/monthly-hours?month={MONTH}&format=meta",
        f"/api/v1/anomalies?date={DAY}",
    ):
        assert client.get(path, headers=ADMIN).status_code == 503
    assert client.post(f"/api/v1/recon/run?date={DAY}", headers=ADMIN).status_code == 503
    response = client.get("/api/v1/admin/connection-pool", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["reports"]["paused"] is True


def test_busy_report_does_not_block_ordinary_routes(client, monkeypatch):
    manager = jobs.ReportJobs()
    monkeypatch.setattr(jobs, "report_jobs", manager)
    started, release = Event(), Event()

    def compute():
        started.set()
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(manager.run, "held", "report", compute)
        assert started.wait(1)
        try:
            assert client.get("/api/v1/furnaces", headers=ADMIN).status_code == 200
            response = client.get(
                f"/api/v1/export/monthly-hours?month={MONTH}&format=meta", headers=ADMIN
            )
            assert response.status_code == 429
            assert response.headers["retry-after"] == "2"
        finally:
            release.set()
        leader.result()


def test_resource_limit_returns_actionable_error(client, db_session, monkeypatch):
    _seed(db_session, "T0001", DAY, "出料工")
    monkeypatch.setattr(settings, "fdc_report_max_source_rows", 1)
    response = client.get(f"/api/v1/export/monthly-hours?month={MONTH}&format=meta", headers=ADMIN)
    assert response.status_code == 422
    assert "缩小" in response.json()["detail"]


def test_monthly_calendar_matches_legacy_day_queries_with_fixed_query_count(db_session):
    from app.models import ChiefConfirm
    from app.services.attendance_service import day_status, get_attendance
    from app.services.scan_detail import day_scan_detail

    _seed(db_session, "T0001", DAY, "出料工")
    fid = db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点"))
    for day, source, status, kind in [
        (7, "补录", "待审", "出勤"),
        (8, "代签", "active", "出勤"),
        (9, "扫码", "active", "加班"),
        (10, "扫码", "已取消", "出勤"),
    ]:
        db_session.add(
            AttendanceRecord(
                emp_id="T0001",
                furnace_id=fid,
                natual_date=f"{MONTH}-{day:02d}",
                post="出料工",
                squad="1班",
                employ_type="正式",
                source=source,
                status=status,
                record_kind=kind,
                shift_slot="早班",
            )
        )
    db_session.add(ChiefConfirm(furnace_id=fid, natual_date=DAY, locked=True))
    db_session.commit()
    expected = {}
    for day in range(1, 32):
        date = f"{MONTH}-{day:02d}"
        expected[date] = (
            day_status(db_session, emp_id="T0001", natual_date=date),
            day_scan_detail(db_session, emp_id="T0001", natual_date=date),
        )
    with capture_sql(db_session) as statements:
        result = get_attendance(db_session, userid="T0001", month=MONTH)
    for day in result["days"]:
        assert (day["status"], day["detail"]) == expected[day["date"]]
    assert len(statements) <= 18
