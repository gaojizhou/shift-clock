"""按员工「拉取+转劳勤」pull_range / start_pull / POST /admin/dingtalk-attendance/pull：

覆盖：
- 逐员工拉取：按本地现存员工工号单独查源库，进度回调每个工号一次。
- 中途失败：已完成员工的原始数据保留，错误信息带失败工号。
- start_pull 锁互斥（与劳勤转换共用一把锁）；后台线程完成后落状态 + 同步日志。
- 端点：admin 可启动，worker 403。
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models import (
    AttendanceRecord,
    AuditLog,
    DingtalkAttendanceRaw,
    Employee,
    Furnace,
    ExternalAttendanceResultRaw,
    LaoqinPunch,
    MonthlyExportLock,
    ReconResult,
)
from app.services import dingtalk_attendance_service as svc


class _DummyConn:
    def close(self):
        pass


def test_mysql_naive_datetime_is_interpreted_as_shanghai_local_time():
    source = datetime(2026, 8, 22, 1, 48, 15)

    normalized = svc._source_datetime(source)

    assert normalized is not None
    assert normalized.isoformat() == "2026-08-22T01:48:15+08:00"
    assert normalized.utcoffset() == timedelta(hours=8)
    assert normalized.astimezone(timezone.utc).isoformat() == "2026-08-21T17:48:15+00:00"


def test_mysql_aware_datetime_is_normalized_to_shanghai():
    source = datetime(2026, 8, 21, 17, 48, 15, tzinfo=timezone.utc)

    normalized = svc._source_datetime(source)

    assert normalized is not None
    assert normalized.isoformat() == "2026-08-22T01:48:15+08:00"


def test_attendance_result_datetime_string_is_normalized_to_shanghai():
    source = datetime(2026, 8, 21, 17, 48, 15, tzinfo=timezone.utc)

    assert svc._raw_str(source) == "2026-08-22 01:48:15"


def _keep_employees(db, emp_ids: set[str]) -> None:
    for e in db.query(Employee).all():
        e.deleted = e.emp_id not in emp_ids
    db.commit()


def _write_raw(db, emp_id: str, date: str):
    """模拟拉取入库：写一条花名册内员工的原始打卡。"""
    db.add(DingtalkAttendanceRaw(
        code=emp_id, name=emp_id, org="35号工作点", layer="x",
        att_time=datetime(2026, 5, int(date[-2:]), 8, 0, 0),
        direct="上班", src="钉钉打卡", natual_date=date,
    ))
    db.commit()


def _attendance(db, emp_id: str, date: str, *, status: str = "active", source: str = "扫码"):
    furnace = db.query(Furnace).filter_by(furnace_no="35号工作点").first()
    db.add(
        AttendanceRecord(
            emp_id=emp_id,
            furnace_id=furnace.id,
            squad="早班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.fromisoformat(f"{date}T10:00:00"),
            source=source,
            status=status,
            natual_date=date,
            work_hours=8,
        )
    )
    db.commit()


def test_pull_range_only_for_furnace_employee_days(db_session):
    _keep_employees(db_session, {"T0001", "T0003"})
    _attendance(db_session, "T0001", "2026-05-01")
    _attendance(db_session, "T0003", "2026-05-02", status="待审", source="补录")
    pulled_days, progress = [], []

    def fake_sync_one_day(conn, db, date, codes):
        pulled_days.append((date, tuple(codes)))
        for code in codes:
            _write_raw(db, code, date)
        return {"fetched": len(codes), "deduped": len(codes), "upserted": len(codes)}

    with patch.object(svc, "_mysql_connect", _DummyConn), \
         patch.object(svc, "_sync_one_day", fake_sync_one_day):
        res = svc.pull_range(
            db_session, "2026-05-01", "2026-05-03",
            progress_cb=lambda done, total, code: progress.append((done, total, code)),
        )

    assert pulled_days == [
        ("2026-04-30", ("T0001",)),
        ("2026-05-01", ("T0001", "T0003")),
        ("2026-05-02", ("T0001", "T0003")),
        ("2026-05-03", ("T0003",)),
    ]
    # 拉取阶段逐日上报（total=证据日 5 天）；转劳勤阶段分两段续报：
    # 打卡转换按源自然日（5 天）逐日心跳 + 逐日对账（3 天），进度在拉取完成数 5
    # 之上继续累加（total=5+5+3=13），保证长任务任何一段卡顿都有心跳续约。
    assert progress == [
        (1, 5, "2026-04-30"),
        (2, 5, "2026-05-01"),
        (3, 5, "2026-05-02"),
        (4, 5, "2026-05-03"),
        (5, 5, "2026-05-04"),
        (6, 13, "2026-04-30"),
        (7, 13, "2026-05-01"),
        (8, 13, "2026-05-02"),
        (9, 13, "2026-05-03"),
        (10, 13, "2026-05-04"),
        (11, 13, "2026-05-01"),
        (12, 13, "2026-05-02"),
        (13, 13, "2026-05-03"),
    ]
    assert db_session.query(LaoqinPunch).filter(LaoqinPunch.emp_id.in_(["T0001", "T0003"])).count() == 6
    assert res["synced"]["employeeCount"] == 2
    assert res["synced"]["eligibleEmployeeDays"] == 2
    assert res["synced"]["upserted"] == 6
    assert res["transformed"]["inserted"] == 6


def test_pull_range_partial_failure_keeps_done_employees(db_session):
    _keep_employees(db_session, {"T0001", "T0003"})
    _attendance(db_session, "T0001", "2026-05-01")
    _attendance(db_session, "T0003", "2026-05-02")

    def flaky(conn, db, date, codes):
        if date == "2026-05-01":
            raise RuntimeError("源库中断")
        for code in codes:
            _write_raw(db, code, date)
        return {"fetched": len(codes), "deduped": len(codes), "upserted": len(codes)}

    with patch.object(svc, "_mysql_connect", _DummyConn), \
         patch.object(svc, "_sync_one_day", flaky):
        with pytest.raises(RuntimeError) as ei:
            svc.pull_range(db_session, "2026-05-01", "2026-05-03")

    assert "2026-05-01" in str(ei.value)
    assert db_session.query(DingtalkAttendanceRaw).filter(DingtalkAttendanceRaw.code == "T0001").count() == 1
    assert db_session.query(DingtalkAttendanceRaw).filter(DingtalkAttendanceRaw.code == "T0003").count() == 0
    assert db_session.query(LaoqinPunch).count() == 0


def test_start_pull_already_running(db_session):
    assert svc._transform_lock.acquire(blocking=False)
    try:
        r = svc.start_pull(db_session, "2026-05-01", "2026-05-02")
    finally:
        svc._transform_lock.release()
    assert r == {"started": False, "alreadyRunning": True}


def test_start_pull_thread_completes_and_logs(db_session):
    """后台线程跑完：状态 done + message 带汇总，同步日志 trigger=manual。"""
    TestSession = sessionmaker(bind=db_session.get_bind())

    def fake_pull(db, dfrom, dto, progress_cb=None):
        if progress_cb:
            progress_cb(1, 2, dfrom)
            progress_cb(2, 2, dto)
        return {
            "dateFrom": dfrom, "dateTo": dto, "dates": [dfrom, dto],
            "synced": {"fetched": 9, "deduped": 9, "upserted": 9, "employeeCount": 2},
            "transformed": {"inserted": 6, "matched": 5},
        }

    with patch.object(svc, "SessionLocal", TestSession), \
         patch.object(svc, "pull_range", fake_pull):
        r = svc.start_pull(db_session, "2026-05-01", "2026-05-02", actor="A0001")
        assert r["started"] is True
        assert svc._transform_lock.acquire(timeout=5)  # 等后台线程结束并释放锁
        svc._transform_lock.release()

    st = svc.get_transform_status(db_session)
    assert st["status"] == "done"
    assert "拉取完成" in (st["message"] or "")
    assert st["done"] == 4 and st["total"] == 4
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    p = json.loads(log.payload)
    assert p["trigger"] == "manual" and p["ok"] is True
    assert p["upserted"] == 9 and p["matched"] == 5


def test_start_pull_thread_error_sets_status_and_logs(db_session):
    TestSession = sessionmaker(bind=db_session.get_bind())

    def boom(db, dfrom, dto, progress_cb=None):
        raise RuntimeError("2026-05-02 拉取/转换失败：源库中断")

    with patch.object(svc, "SessionLocal", TestSession), \
         patch.object(svc, "pull_range", boom):
        r = svc.start_pull(db_session, "2026-05-01", "2026-05-03", actor="A0001")
        assert r["started"] is True
        assert svc._transform_lock.acquire(timeout=5)
        svc._transform_lock.release()

    st = svc.get_transform_status(db_session)
    assert st["status"] == "error"
    assert "源库中断" in (st["message"] or "")
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    p = json.loads(log.payload)
    assert p["trigger"] == "manual" and p["ok"] is False


def test_pull_endpoint_admin_starts(client, db_session):
    calls = {}

    def fake_start(db, dfrom, dto, actor="admin", codes=None):
        calls["start"] = (dfrom, dto, actor, codes)
        return {"started": True, "alreadyRunning": False}

    with patch.object(svc, "start_pull", fake_start):
        r = client.post(
            "/api/v1/admin/dingtalk-attendance/pull",
            json={"dateFrom": "2026-05-01", "dateTo": "2026-05-31"},
            headers={"x-test-user": "A0001", "x-test-role": "admin"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "alreadyRunning": False}
    assert calls["start"] == ("2026-05-01", "2026-05-31", "A0001", None)


def test_pull_endpoint_worker_forbidden(client, db_session):
    r = client.post(
        "/api/v1/admin/dingtalk-attendance/pull",
        json={"dateFrom": "2026-05-01", "dateTo": "2026-05-02"},
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r.status_code == 403


def test_attendance_result_pull_range_by_furnace_employee_month(db_session):
    _keep_employees(db_session, {"T0001", "T0003"})
    _attendance(db_session, "T0001", "2026-06-01")
    _attendance(db_session, "T0003", "2026-06-03", status="待审", source="补录")
    pulled_days, progress = [], []

    def fake_fetch(conn, dfrom, dto, codes):
        pulled_days.append((dfrom, tuple(codes)))
        return [tuple({"user_code":code,"kq_date":dfrom,"cqgs":"8.00000"}.get(k) for k in svc._ATT_RESULT_COLS) for code in codes]

    with patch.object(svc, "_mysql_connect", _DummyConn), \
         patch.object(svc, "_fetch_result_rows", fake_fetch):
        res = svc.pull_attendance_result_range(
            db_session, "2026-06-01", "2026-06-03",
            progress_cb=lambda done, total, code: progress.append((done, total, code)),
        )

    assert pulled_days == [
        ("2026-06-01", ("T0001", "T0003")),
        ("2026-06-02", ("T0001", "T0003")),
        ("2026-06-03", ("T0001", "T0003")),
    ]
    assert progress == [
        (1, 4, "2026-06-01"),
        (2, 4, "2026-06-02"),
        (3, 4, "2026-06-03"),
        (4, 4, "2026-06-03"),
    ]
    assert db_session.query(ExternalAttendanceResultRaw).count() == 6
    assert res["employeeCount"] == 2
    assert res["eligibleEmployeeDays"] == 6
    assert res["synced"] == {"fetched": 6, "deduped": 6, "upserted": 6, "removed": 0, "inserted": 6, "updated": 0, "withdrawn": 0, "restored": 0}


def test_attendance_result_month_scope_is_independent_across_months(db_session):
    _keep_employees(db_session, {"T0001", "T0003"})
    # 两条资格记录都不在请求日期内，仍应按各自完整自然月决定白名单。
    _attendance(db_session, "T0001", "2026-06-15")
    _attendance(db_session, "T0003", "2026-07-20", status="待审", source="补录")
    pulled_days = []

    def fake_fetch(conn, dfrom, dto, codes):
        pulled_days.append((dfrom, tuple(codes)))
        return ()

    with patch.object(svc, "_mysql_connect", _DummyConn), \
         patch.object(svc, "_fetch_result_rows", fake_fetch):
        res = svc.pull_attendance_result_range(
            db_session, "2026-06-30", "2026-07-02"
        )

    assert pulled_days == [
        ("2026-06-30", ("T0001",)),
        ("2026-07-01", ("T0003",)),
        ("2026-07-02", ("T0003",)),
    ]
    assert res["employeeCount"] == 2
    assert res["eligibleEmployeeDays"] == 3


def test_start_attendance_result_pull_thread_completes(db_session):
    TestSession = sessionmaker(bind=db_session.get_bind())

    def fake_pull(db, dfrom, dto, progress_cb=None):
        if progress_cb:
            progress_cb(1, 2, dfrom)
            progress_cb(2, 2, dto)
        return {
            "dateFrom": dfrom, "dateTo": dto, "dates": [dfrom, dto],
            "employeeCount": 2,
            "synced": {"fetched": 8, "deduped": 8, "upserted": 7},
        }

    with patch.object(svc, "SessionLocal", TestSession), \
         patch.object(svc, "pull_attendance_result_range", fake_pull):
        r = svc.start_attendance_result_pull(db_session, "2026-06-01", "2026-06-02")
        assert r["started"] is True
        assert svc._transform_lock.acquire(timeout=5)
        svc._transform_lock.release()

    st = svc.get_attendance_result_status(db_session)
    assert st["status"] == "done"
    assert st["done"] == 2 and st["total"] == 2
    assert "考勤结果拉取完成" in (st["message"] or "")
    assert "入库 7 条" in (st["message"] or "")


def test_attendance_result_endpoint_admin_starts(client, db_session):
    calls = {}

    def fake_start(db, dfrom, dto, codes=None, actor=None):
        calls["start"] = (dfrom, dto, codes)
        calls["actor"] = actor
        return {"started": True, "alreadyRunning": False}

    with patch.object(svc, "start_attendance_result_pull", fake_start):
        r = client.post(
            "/api/v1/admin/dingtalk-attendance/result/pull",
            json={"dateFrom": "2026-06-01", "dateTo": "2026-06-30"},
            headers={"x-test-user": "A0001", "x-test-role": "admin"},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"started": True, "alreadyRunning": False}
    assert calls["start"] == ("2026-06-01", "2026-06-30", None)
    assert calls["actor"] == "A0001"


def test_attendance_result_list_endpoint(client, db_session):
    db_session.add(ExternalAttendanceResultRaw(
        tenant_code="HSGY",
        user_code="0523052403",
        user_name="何嘉嘉",
        dept_name="人力/OA协同组",
        layer_name="示例集团/数智化中心",
        kq_date="2026-06-01",
        status_name="正常",
        actual_on="2026-06-01 08:15:00",
        actual_off="2026-06-01 17:32:00",
        quota="8.00000",
        cqgs="8.00000",
    ))
    db_session.add(ExternalAttendanceResultRaw(
        tenant_code="HSGY",
        user_code="0523052404",
        user_name="王测试",
        dept_name="生产一部",
        layer_name="示例集团/生产中心",
        kq_date="2026-06-01",
        status_name="正常",
        actual_on="2026-06-01 08:00:00",
        actual_off="2026-06-01 17:00:00",
        quota="8.00000",
        cqgs="8.00000",
    ))
    db_session.commit()

    r = client.get(
        "/api/v1/admin/dingtalk-attendance/result",
        params={"dateFrom": "2026-06-01", "dateTo": "2026-06-01"},
        headers={"x-test-user": "A0001", "x-test-role": "admin"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["items"][0]["userCode"] == "0523052403"
    assert body["items"][0]["statusName"] == "正常"
    assert body["items"][0]["actualOn"] == "2026-06-01 08:15:00"

    r = client.get(
        "/api/v1/admin/dingtalk-attendance/result",
        params={
            "dateFrom": "2026-06-01",
            "dateTo": "2026-06-01",
            "userCode": "0523052404",
        },
        headers={"x-test-user": "A0001", "x-test-role": "admin"},
    )
    assert r.status_code == 200, r.text
    assert [x["userCode"] for x in r.json()["items"]] == ["0523052404"]

    r = client.get(
        "/api/v1/admin/dingtalk-attendance/result",
        params={"dateFrom": "2026-06-01", "dateTo": "2026-06-01", "userName": "嘉"},
        headers={"x-test-user": "A0001", "x-test-role": "admin"},
    )
    assert r.status_code == 200, r.text
    assert [x["userName"] for x in r.json()["items"]] == ["何嘉嘉"]

    r = client.get(
        "/api/v1/admin/dingtalk-attendance/result",
        params={"dateFrom": "2026-06-01", "dateTo": "2026-06-01", "deptName": "生产"},
        headers={"x-test-user": "A0001", "x-test-role": "admin"},
    )
    assert r.status_code == 200, r.text
    assert [x["deptName"] for x in r.json()["items"]] == ["生产一部"]


def test_attendance_result_endpoint_worker_forbidden(client, db_session):
    r = client.post(
        "/api/v1/admin/dingtalk-attendance/result/pull",
        json={"dateFrom": "2026-06-01", "dateTo": "2026-06-02"},
        headers={"x-test-user": "T0001", "x-test-role": "worker"},
    )
    assert r.status_code == 403


def test_empty_eligible_scope_prunes_without_connecting_source(db_session):
    _keep_employees(db_session, {"T0001"})
    _write_raw(db_session, "T0001", "2026-05-01")
    db_session.add(ExternalAttendanceResultRaw(
        user_code="T0001", kq_date="2026-05-01", cqgs="8.00000"
    ))
    db_session.commit()

    with patch.object(svc, "_mysql_connect", side_effect=AssertionError("不应连接源库")):
        raw = svc.sync_range(db_session, "2026-05-01", "2026-05-01")
        result = svc.pull_attendance_result_range(
            db_session, "2026-05-01", "2026-05-01"
        )

    assert raw["fetched"] == 0 and raw["removed"] == 1
    assert result["synced"]["fetched"] == 0
    assert result["synced"]["removed"] == 1
    assert db_session.query(DingtalkAttendanceRaw).count() == 0
    assert db_session.query(ExternalAttendanceResultRaw).count() == 0


def test_reset_all_laoqin_data_preserves_relevant_confirmations_and_unlocks(db_session):
    _keep_employees(db_session, {"T0001", "T0003"})
    _attendance(db_session, "T0001", "2026-05-01")
    _write_raw(db_session, "T0001", "2026-05-01")
    db_session.add_all([
        LaoqinPunch(
            emp_id="T0001",
            punch_time=datetime(2026, 5, 1, 8),
            inout_type="上班",
            natual_date="2026-05-01",
        ),
        ExternalAttendanceResultRaw(
            user_code="T0001", kq_date="2026-05-01", cqgs="8.00000"
        ),
        ReconResult(
            emp_id="T0001",
            date="2026-05-01",
            status="一致",
            hr_status="通过",
            hr_by="H0001",
            chief_status="不通过",
            chief_by="C0001",
        ),
        ReconResult(
            emp_id="T0003",
            date="2026-05-01",
            status="劳勤有工作点无",
            hr_status="通过",
        ),
        MonthlyExportLock(
            month="2026-05",
            furnace_id=db_session.scalar(select(Furnace.id).where(Furnace.furnace_no == "35号工作点")),
            locked=True,
            locked_by="H0001",
            locked_at=datetime.now(timezone.utc),
        ),
    ])
    db_session.commit()

    result = svc.reset_all_laoqin_data(db_session, actor="maintenance-test")

    assert result["before"] == {
        "raw": 1,
        "punches": 1,
        "attendanceResults": 1,
        "recon": 2,
    }
    assert result["removedRecon"] == 1
    assert result["remainingRecon"] == 1
    assert result["unlockedMonths"] == ["2026-05"]
    assert db_session.query(DingtalkAttendanceRaw).count() == 0
    assert db_session.query(LaoqinPunch).count() == 0
    assert db_session.query(ExternalAttendanceResultRaw).count() == 0
    recon = db_session.query(ReconResult).one()
    assert (recon.emp_id, recon.hr_status, recon.chief_status) == (
        "T0001", "通过", "不通过"
    )
    lock = db_session.query(MonthlyExportLock).one()
    assert lock.locked is False
    assert lock.unlocked_by == "maintenance-test"
    log = db_session.query(AuditLog).filter_by(action="attendance_filtered_reset").one()
    assert json.loads(log.payload)["removedRecon"] == 1


def test_timezone_cleanup_refuses_to_delete_locked_month_data(db_session):
    _keep_employees(db_session, {"T0001"})
    _attendance(db_session, "T0001", "2026-05-01")
    furnace_id = db_session.scalar(
        select(Furnace.id).where(Furnace.furnace_no == "35号工作点")
    )
    db_session.add_all([
        ReconResult(emp_id="T0001", date="2026-05-01", status="一致"),
        MonthlyExportLock(
            month="2026-05",
            furnace_id=furnace_id,
            locked=True,
            locked_by="H0001",
            locked_at=datetime.now(timezone.utc),
        ),
    ])
    db_session.commit()

    with pytest.raises(RuntimeError, match="已封账工作点"):
        svc.reset_all_laoqin_data(
            db_session,
            actor="timezone-hotfix",
            fail_if_locked=True,
        )

    assert db_session.query(ReconResult).count() == 1
    lock = db_session.query(MonthlyExportLock).one()
    assert lock.locked is True
    assert lock.locked_by == "H0001"
    assert db_session.query(AuditLog).filter_by(action="attendance_filtered_reset").count() == 0
