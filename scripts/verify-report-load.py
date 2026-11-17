#!/usr/bin/env python3
"""Isolated PostgreSQL smoke/load check. Requires an EMPTY fdc_report_verify database.

app/api/.venv/bin/python scripts/verify-report-load.py \
  --database-url postgresql://user@127.0.0.1:55439/fdc_report_verify

Uses real API routing and service calculations via ASGI; omits production middleware,
Redis, schedulers, network and external attendance integrations. Never target production.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/api"))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database-url", required=True)
parser.add_argument("--employees", type=int, default=300)
parser.add_argument("--seconds", type=int, default=60)
parser.add_argument("--rps", type=int, default=14)
args = parser.parse_args()
if min(args.employees, args.seconds, args.rps) <= 0:
    parser.error("employees, seconds and rps must be positive")
from sqlalchemy.engine import make_url
url = make_url(args.database_url)
if (url.drivername not in {"postgresql", "postgresql+psycopg2"}
        or url.host not in {"127.0.0.1", "localhost"}
        or url.database != "fdc_report_verify" or url.query):
    parser.error("Only a local, disposable fdc_report_verify database is accepted")
os.environ.update(DATABASE_URL=args.database_url, FDC_TEST_MODE="true",
                  FDC_AUTH_MODE="mock", FDC_REPORTS_PAUSED="false",
                  FDC_REQUEST_AUDIT_REDIS_URL="", FDC_ATTENDANCE_SYNC_SCHEDULER_ENABLED="false")

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from app.config import settings
from app.db import Base, engine, SessionLocal
from app.models import (AttendanceRecord, Employee, Furnace, FurnaceType,
                        ExternalAttendanceResultRaw, LaoqinPunch, QuotaRule)
from app.routers.api import router
from app.services import report_jobs as jobs

with engine.connect() as connection:
    if str(connection.execute(text("SELECT inet_server_addr()")).scalar()) not in {"127.0.0.1", "::1"}:
        raise SystemExit("PostgreSQL server must be on a loopback address")
if inspect(engine).get_table_names():
    raise SystemExit("Database is not empty; create a fresh disposable database before each run")
Base.metadata.create_all(engine)
# ORM create_all omits indexes installed by SQL migrations in deployed databases.
with engine.begin() as connection:
    for filename in ("0041_attendance_record.sql", "0049_laoqin_punch_index.sql", "0063_external_attendance_result_raw.sql"):
        sql = (ROOT / "app/api/migrations" / filename).read_text()
        for statement in re.findall(r"CREATE INDEX[^;]+;", sql, flags=re.I):
            connection.exec_driver_sql(statement)
with SessionLocal() as db:
    db.add(FurnaceType(code="VERIFY", name="verify", sort=1))
    db.flush()
    f = Furnace(region="验证地区", furnace_no="验证工作点", furnace_type_code="VERIFY", name="verify")
    db.add(f)
    db.flush()
    fid = f.id
    db.add(QuotaRule(furnace_id=fid, squad="1班", post="出料工", capacity=args.employees+10))
    for i in range(args.employees):
        emp = f"VERIFY{i:04d}"
        db.add(Employee(emp_id=emp, name=emp))
        db.flush()
        for day in range(1, 31):
            date = f"2026-08-{day:02d}"
            db.add(AttendanceRecord(emp_id=emp, furnace_id=fid, natual_date=date,
                squad="1班", post="出料工", employ_type="正式", source="扫码", work_hours=8,
                scan_time=datetime(2026, 8, day, 10, tzinfo=ZoneInfo("Asia/Shanghai"))))
            db.add(ExternalAttendanceResultRaw(user_code=emp, user_name=emp, kq_date=date, cqgs="8.00000"))
            for hour, direction in [(9, "进"), (19, "出")]:
                db.add(LaoqinPunch(emp_id=emp, natual_date=date, inout_type=direction,
                    punch_time=datetime(2026, 8, day, hour, tzinfo=ZoneInfo("Asia/Shanghai"))))
    db.commit()
with engine.begin() as connection:
    connection.exec_driver_sql("ANALYZE")

# Validate actual PostgreSQL transaction settings and timeout/error cleanup.
@jobs.report_endpoint
def probe(db, user, sql):
    return db.execute(text(sql)).scalar()
with SessionLocal() as db:
    assert probe(db, {}, "SHOW transaction_read_only") == "on"
    assert probe(db, {}, "SHOW transaction_isolation") == "repeatable read"
    try:
        probe(db, {}, "UPDATE employee SET name=name WHERE emp_id='VERIFY0000'")
        raise AssertionError("read-only transaction accepted an UPDATE")
    except DBAPIError as exc:
        assert exc.orig.pgcode == "25006"
    settings.fdc_report_statement_timeout_ms = 100
    try:
        probe(db, {}, "SELECT pg_sleep(1)")
        raise AssertionError("statement timeout did not fire")
    except HTTPException as exc:
        assert exc.status_code == 504
    settings.fdc_report_statement_timeout_ms = 10000
    settings.fdc_report_lock_timeout_ms = 100
    with engine.connect() as blocker:
        blocker.execute(text("LOCK TABLE employee IN ACCESS EXCLUSIVE MODE"))
        try:
            probe(db, {}, "SELECT count(*) FROM employee")
            raise AssertionError("lock timeout did not fire")
        except HTTPException as exc:
            assert exc.status_code == 504
        finally:
            blocker.rollback()
    settings.fdc_report_lock_timeout_ms = 2000
    assert probe(db, {}, "SELECT count(*) FROM employee") == args.employees
    assert not db.in_transaction()
    assert db.execute(text("SHOW transaction_read_only")).scalar() == "off"
    assert db.execute(text("SHOW statement_timeout")).scalar() == "0"
    db.rollback()
@jobs.report_endpoint
def idle_probe(db, user):
    db.execute(text("SELECT 1"))
    time.sleep(1.2)
    return db.execute(text("SELECT 1")).scalar()
settings.fdc_report_idle_transaction_timeout_ms = 1000
with SessionLocal() as db:
    # CPU work longer than the configured idle timeout is allowed within the
    # overall report budget; the server must not terminate a valid calculation.
    assert idle_probe(db, {}) == 1
    assert not db.in_transaction()
    assert db.execute(text("SELECT 1")).scalar() == 1
settings.fdc_report_idle_transaction_timeout_ms = 15000
assert jobs.report_jobs.diagnostics()["active"] == 0
jobs.report_jobs = jobs.ReportJobs()
# Router imports a reference for the diagnostics endpoint; keep it in sync for this process.
import app.routers.api as api
api.report_jobs = jobs.report_jobs

app = FastAPI()
app.include_router(router)
headers = {"x-test-user": "VERIFY0000", "x-test-role": "admin"}
ordinary = [
    ("/api/v1/quota", {"region": "验证地区", "furnace": "验证工作点", "post": "出料工", "squad": "1班", "date": "2026-08-15"}),
    ("/api/v1/attendance", {"userid": "VERIFY0000", "month": "2026-08"}),
    ("/api/v1/demo-context", {}),
    ("/api/v1/furnaces", {}),
]

async def run():
    timings = {"ordinary": [], "report": []}
    statuses = {"ordinary": Counter(), "report": Counter()}
    samples = []
    by_path = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://verify", timeout=70) as client:
        async def request(kind, index):
            started = time.perf_counter()
            if kind == "ordinary":
                path, params = ordinary[index % len(ordinary)]
                response = await client.get(path, params=params, headers=headers)
                by_path.setdefault(path, []).append((time.perf_counter()-started)*1000)
            else:
                response = await client.post("/api/v1/export/sheet-preview", headers={
                    **headers, "x-test-user": f"VERIFY{index % 2:04d}"}, json={
                    "month": "2026-08", "furnaceIds": [fid], "furnaces": ["验证工作点"], "sheet": "输出给薪资"})
                if response.status_code == 200:
                    assert response.json()["payrollSheet"]["rowCount"] == args.employees
            statuses[kind][response.status_code] += 1
            timings[kind].append((time.perf_counter()-started)*1000)
            if response.status_code not in {200, 429}:
                raise AssertionError((kind, response.status_code, response.text[:300]))
        download = await client.post("/api/v1/export/sheet", headers=headers, json={
            "month": "2026-08", "furnaceIds": [fid], "furnaces": ["验证工作点"], "sheet": "输出给薪资"})
        assert download.status_code == 200, download.text[:200]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM audit_log WHERE action='export_sheet'")).scalar() == 1
        started = time.perf_counter()
        tasks = []
        for index in range(args.seconds * args.rps):
            due = started + index / args.rps
            await asyncio.sleep(max(0, due-time.perf_counter()))
            tasks.append(asyncio.create_task(request("ordinary", index)))
            if index % args.rps == 0:
                tasks.append(asyncio.create_task(request("report", index//args.rps)))
                samples.append(jobs.report_jobs.diagnostics())
        await asyncio.gather(*tasks)
    def stats(values):
        values = sorted(values)
        return {"count": len(values), "p50Ms": round(values[len(values)//2], 1),
                "p95Ms": round(values[min(len(values)-1, int(len(values)*.95))], 1),
                "maxMs": round(max(values), 1)}
    with engine.connect() as conn:
        recon_rows = conn.execute(text("SELECT count(*) FROM recon_result")).scalar()
        version = conn.execute(text("SHOW server_version")).scalar()
    output = {"host": platform.node(), "postgres": version, "cpuCount": os.cpu_count(),
              "employees": args.employees, "days": 30, "targetOrdinaryRps": args.rps,
              "durationSeconds": args.seconds, "postgresReadOnlyAndTimeoutChecks": "passed",
              "ordinary": {**stats(timings["ordinary"]), "status": dict(statuses["ordinary"])},
              "ordinaryByPath": {path: stats(values) for path, values in by_path.items()},
              "report": {**stats(timings["report"]), "status": dict(statuses["report"])},
              "peakActive": max(s["active"] for s in samples),
              "peakRssMb": max((s["residentMemoryMb"] for s in samples
                                if s["residentMemoryMb"] is not None), default=None),
              "reconciliationRowsWritten": recon_rows, "diagnostics": jobs.report_jobs.diagnostics()}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    assert statuses["ordinary"] == {200: args.seconds * args.rps}
    assert statuses["report"][200] > 0
    assert output["peakActive"] <= 1 and recon_rows == 0
    assert output["diagnostics"]["active"] == 0

asyncio.run(run())
engine.dispose()
