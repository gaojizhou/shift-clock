"""每日 0 点考勤同步定时任务：同步上月全月 + 本月至今，
原始打卡和考勤结果两张源表都同步，源库不可达时降级不抛、与手动拉取锁互斥时跳过。"""
import json
from unittest.mock import patch

from app.models import AuditLog
from app.services import dingtalk_attendance_service as svc


def _fake_pull(calls):
    def fn(db, dfrom, dto, progress_cb=None, codes=None):
        calls.setdefault("pull", []).append((dfrom, dto, codes))
        return {
            "dateFrom": dfrom, "dateTo": dto, "dates": [],
            "synced": {"fetched": 5, "deduped": 5, "upserted": 5},
            "transformed": {"inserted": 3, "matched": 2},
        }
    return fn


def _fake_attendance_result_pull(calls):
    def fn(db, dfrom, dto, progress_cb=None, codes=None):
        calls.setdefault("result", []).append((dfrom, dto))
        return {
            "dateFrom": dfrom, "dateTo": dto, "dates": [],
            "synced": {"fetched": 7, "deduped": 7, "upserted": 6},
        }
    return fn


def test_job_pulls_previous_month_and_current_month_for_both_tables(db_session):
    calls = {}
    with patch.object(svc, "pull_range", _fake_pull(calls)), \
         patch.object(svc, "pull_attendance_result_range", _fake_attendance_result_pull(calls)):
        res = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    assert res["dateFrom"] == "2026-04-01"
    assert res["dateTo"] == "2026-05-10"
    assert calls["pull"] == [
        ("2026-04-01", "2026-04-30", None),
        ("2026-05-01", "2026-05-10", None),
    ]
    assert calls["result"] == [("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-10")]
    assert res["error"] is None
    assert res["synced"]["upserted"] == 10
    assert res["attendanceResult"]["upserted"] == 12
    assert res["transformed"]["inserted"] == 6


def test_job_passes_daily_progress_callbacks_and_releases_database_lease(
    db_session,
):
    callbacks = []

    def raw(db, dfrom, dto, progress_cb=None, codes=None):
        callbacks.append(("raw", progress_cb is not None))
        progress_cb(1, 1, dfrom)
        return {
            "synced": {"fetched": 1, "deduped": 1, "upserted": 1},
            "transformed": {"inserted": 1, "matched": 1},
        }

    def result(db, dfrom, dto, progress_cb=None, codes=None):
        callbacks.append(("result", progress_cb is not None))
        progress_cb(1, 1, dto)
        return {"synced": {"fetched": 1, "deduped": 1, "upserted": 1}}

    with patch.object(svc, "pull_range", raw), patch.object(
        svc, "pull_attendance_result_range", result
    ):
        response = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    assert response["error"] is None
    assert callbacks == [
        ("raw", True),
        ("result", True),
        ("raw", True),
        ("result", True),
    ]
    lease = svc._task_state(db_session, svc.TASK_LEASE_KIND)
    assert lease.owner_token is None
    assert lease.status == "idle"


def test_job_degrades_on_source_error(db_session):
    def boom(db, dfrom, dto, progress_cb=None, codes=None):
        raise RuntimeError("未配置钉钉考勤源库密码")

    with patch.object(svc, "pull_range", boom):
        res = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    # 源库不可达：不抛异常，error 记录原因，不崩调度器；锁已释放（可再次启动）
    assert res["error"] is not None
    assert "密码" in res["error"]
    assert res["synced"] is None
    assert svc._transform_lock.acquire(blocking=False)
    svc._transform_lock.release()


def test_job_writes_sync_log(db_session):
    calls = {}
    with patch.object(svc, "pull_range", _fake_pull(calls)), \
         patch.object(svc, "pull_attendance_result_range", _fake_attendance_result_pull(calls)):
        svc.attendance_sync_job(today="2026-05-10", db=db_session)

    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    p = json.loads(log.payload)
    assert p["trigger"] == "auto" and p["ok"] is True
    assert p["dateFrom"] == "2026-04-01" and p["dateTo"] == "2026-05-10"
    assert p["upserted"] == 10 and p["matched"] == 4
    assert p["attendanceResultUpserted"] == 12
    assert len(p["ranges"]) == 2


def test_job_skips_when_task_running(db_session):
    calls = {}
    assert svc._transform_lock.acquire(blocking=False)  # 模拟手动任务占锁
    try:
        with patch.object(svc, "pull_range", _fake_pull(calls)):
            res = svc.attendance_sync_job(today="2026-05-10", db=db_session)
    finally:
        svc._transform_lock.release()

    # 不并发写：跳过本次并记录原因；且 job 未误释放别人的锁（否则上面 release 会抛）
    assert "pull" not in calls
    assert res["error"] is not None and "跳过" in res["error"]
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    assert json.loads(log.payload)["ok"] is False


def test_job_narrows_codes_when_some_furnaces_are_locked(db_session):
    """部分工作点封账：只排除封账工作点员工，其余工作点照常同步，不再整月跳过。"""
    from datetime import datetime

    from app.models import AttendanceRecord, Employee, EmployeeFavorite, Furnace, Region
    from app.services.monthly_export_lock_service import set_month_locks

    db_session.add_all([
        Region(code="SYNC_NARROW", name="收窄地区", sort=120),
        Furnace(id=901, region="收窄地区", furnace_no="收窄锁定工作点"),
        Furnace(id=902, region="收窄地区", furnace_no="收窄正常工作点"),
        Employee(emp_id="NARROW_LOCKED", name="封账工作点员工", role="worker"),
        Employee(emp_id="NARROW_FREE", name="正常工作点员工", role="worker"),
        EmployeeFavorite(emp_id="NARROW_LOCKED", kind="region", value="收窄地区"),
        EmployeeFavorite(emp_id="NARROW_FREE", kind="region", value="收窄地区"),
    ])
    db_session.flush()
    db_session.add_all([
        AttendanceRecord(
            emp_id="NARROW_LOCKED", furnace_id=901, squad="早班", post="出料工",
            employ_type="正式", scan_time=datetime(2026, 4, 5, 10), source="扫码",
            natual_date="2026-04-05", status="active", work_hours=8,
        ),
        AttendanceRecord(
            emp_id="NARROW_FREE", furnace_id=902, squad="早班", post="出料工",
            employ_type="正式", scan_time=datetime(2026, 4, 5, 10), source="扫码",
            natual_date="2026-04-05", status="active", work_hours=8,
        ),
    ])
    db_session.commit()
    set_month_locks(
        db_session, month="2026-04", furnace_ids=[901], locked=True, actor="HR01"
    )

    calls = {}
    with patch.object(svc, "pull_range", _fake_pull(calls)), \
         patch.object(svc, "pull_attendance_result_range", _fake_attendance_result_pull(calls)):
        res = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    # 封账月（2026-04）段按收窄工号执行：剔除封账工作点员工，其余照常拉取；
    # 未封账月（2026-05 至今）段不收窄。
    assert res["error"] is None
    assert len(calls["pull"]) == 2
    april_from, april_to, april_codes = calls["pull"][0]
    assert (april_from, april_to) == ("2026-04-01", "2026-04-30")
    assert april_codes is not None
    assert "NARROW_LOCKED" not in april_codes
    assert "NARROW_FREE" in april_codes
    may_from, may_to, may_codes = calls["pull"][1]
    assert (may_from, may_to, may_codes) == ("2026-05-01", "2026-05-10", None)
    assert res.get("narrowedLockedRanges") == 1
    assert res.get("skippedLockedRanges") == 0
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    p = json.loads(log.payload)
    assert p["ok"] is True
    assert p["narrowedLockedRanges"] == 1
    # 收窄信息进入逐段结果，避免「部分没同步」被读成「全部完成」
    assert p["ranges"][0].get("narrowedForLockedFurnaces")
    assert "收窄地区 收窄锁定工作点" in p["ranges"][0]["narrowedForLockedFurnaces"]


def test_job_marks_range_skipped_when_all_employees_locked(db_session):
    """范围内员工全部属于封账工作点：该段标记 skippedLocked 且不调源库。"""
    from datetime import datetime

    from app.models import AttendanceRecord, Employee, Furnace
    from app.services.monthly_export_lock_service import set_month_locks

    db_session.add(Furnace(id=911, region="示例厂区A", furnace_no="全锁跳过工作点"))
    db_session.flush()
    db_session.add(AttendanceRecord(
        emp_id="T0001", furnace_id=911, squad="早班", post="出料工",
        employ_type="正式", scan_time=datetime(2026, 4, 5, 10), source="扫码",
        natual_date="2026-04-05", status="active", work_hours=8,
    ))
    db_session.commit()
    set_month_locks(
        db_session, month="2026-04", furnace_ids=[911], locked=True, actor="HR01"
    )
    # 其余在册员工当月没有任何工作点记录 → 同步范围里只剩封账工作点员工
    for emp_id in ("T0003", "T0005", "L0001"):
        employee = db_session.query(Employee).filter_by(emp_id=emp_id).one()
        employee.deleted = True
    db_session.commit()

    def guard_pull(db, dfrom, dto, progress_cb=None, codes=None):
        assert codes != [], "全锁段不应以空工号集合调用 pull"
        return {
            "dateFrom": dfrom, "dateTo": dto, "dates": [],
            "synced": {"fetched": 1, "deduped": 1, "upserted": 1, "employeeCount": 1},
            "transformed": {"inserted": 1, "matched": 1},
        }

    calls = {}
    with patch.object(svc, "pull_range", guard_pull), \
         patch.object(svc, "pull_attendance_result_range", _fake_attendance_result_pull(calls)):
        res = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    assert res["error"] is None
    april = res["ranges"][0]
    assert april["skippedLocked"] is True
    assert "锁定" in april["error"]
    assert april["lockedFurnaces"] == "示例厂区A 全锁跳过工作点"
    may = res["ranges"][1]
    assert not may.get("skippedLocked")
    assert res.get("skippedLockedRanges") == 1
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    p = json.loads(log.payload)
    assert p["skippedLockedRanges"] == 1
    assert p["ranges"][0]["skippedLocked"] is True


def test_job_state_finalize_failure_is_logged_and_does_not_escape(db_session):
    calls = {}
    real_update = svc._transform_update
    update_count = 0

    def fail_terminal_update(db, state, **fields):
        nonlocal update_count
        update_count += 1
        if fields.get("finished_at") is not None:
            raise RuntimeError("状态库暂时不可写")
        return real_update(db, state, **fields)

    with patch.object(svc, "pull_range", _fake_pull(calls)), \
         patch.object(svc, "pull_attendance_result_range", _fake_attendance_result_pull(calls)), \
         patch.object(svc, "_transform_update", fail_terminal_update):
        result = svc.attendance_sync_job(today="2026-05-10", db=db_session)

    assert update_count >= 4
    assert "状态收尾失败" in result["error"]
    log = db_session.query(AuditLog).filter(AuditLog.action == "attendance_auto_sync").one()
    assert json.loads(log.payload)["ok"] is False
    assert svc._transform_lock.acquire(blocking=False)
    svc._transform_lock.release()
