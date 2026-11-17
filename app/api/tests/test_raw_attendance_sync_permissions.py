import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import (
    AuditLog,
    DingtalkAttendanceRaw,
    DingtalkSyncState,
    Employee,
    EmployeeFavorite,
    Furnace,
    ExternalAttendanceResultRaw,
    Region,
)
from app.services import dingtalk_attendance_service as attendance_service


HR = {"x-test-user": "HR01", "x-test-role": "hr"}
STAT = {"x-test-user": "STAT01", "x-test-role": "stat"}
RANGE = {"dateFrom": "2026-07-01", "dateTo": "2026-07-01"}


def _stub_background_jobs(monkeypatch):
    monkeypatch.setattr(
        attendance_service,
        "start_pull",
        lambda db, date_from, date_to, actor, **kwargs: {"started": True, "alreadyRunning": False},
    )
    monkeypatch.setattr(
        attendance_service,
        "start_transform",
        lambda db, date_from, date_to, **kwargs: {"started": True, "alreadyRunning": False},
    )
    monkeypatch.setattr(
        attendance_service,
        "start_attendance_result_pull",
        lambda db, date_from, date_to, **kwargs: {"started": True, "alreadyRunning": False},
    )


def test_hr_can_access_and_operate_raw_attendance_sync(client, monkeypatch):
    _stub_background_jobs(monkeypatch)

    responses = [
        client.post("/api/v1/admin/dingtalk-attendance/pull", json=RANGE, headers=HR),
        client.post("/api/v1/admin/dingtalk-attendance/transform", json=RANGE, headers=HR),
        client.get("/api/v1/admin/dingtalk-attendance/transform-status", headers=HR),
        client.get("/api/v1/admin/dingtalk-attendance/sync-logs", headers=HR),
        client.get(
            "/api/v1/admin/dingtalk-attendance/raw",
            params=RANGE,
            headers=HR,
        ),
        client.post("/api/v1/admin/dingtalk-attendance/result/pull", json=RANGE, headers=HR),
        client.get("/api/v1/admin/dingtalk-attendance/result/status", headers=HR),
        client.get(
            "/api/v1/admin/dingtalk-attendance/result",
            params=RANGE,
            headers=HR,
        ),
    ]

    assert [response.status_code for response in responses] == [200] * len(responses)


def test_stat_cannot_access_or_operate_raw_attendance_sync(client, monkeypatch):
    _stub_background_jobs(monkeypatch)

    responses = [
        client.post("/api/v1/admin/dingtalk-attendance/pull", json=RANGE, headers=STAT),
        client.post("/api/v1/admin/dingtalk-attendance/transform", json=RANGE, headers=STAT),
        client.get("/api/v1/admin/dingtalk-attendance/transform-status", headers=STAT),
        client.get("/api/v1/admin/dingtalk-attendance/sync-logs", headers=STAT),
        client.get(
            "/api/v1/admin/dingtalk-attendance/raw",
            params=RANGE,
            headers=STAT,
        ),
        client.post("/api/v1/admin/dingtalk-attendance/result/pull", json=RANGE, headers=STAT),
        client.get("/api/v1/admin/dingtalk-attendance/result/status", headers=STAT),
        client.get(
            "/api/v1/admin/dingtalk-attendance/result",
            params=RANGE,
            headers=STAT,
        ),
    ]

    assert [response.status_code for response in responses] == [403] * len(responses)


def test_raw_attendance_fuzzy_searches_employee_id_and_name_before_limit(
    client, db_session
):
    db_session.add_all([
        DingtalkAttendanceRaw(
            code="0321012706",
            name="马一奴斯",
            natual_date="2026-07-01",
            att_time=datetime(2026, 7, 1, 8),
        ),
        DingtalkAttendanceRaw(
            code="0321099999",
            name="马一新",
            natual_date="2026-07-01",
            att_time=datetime(2026, 7, 1, 9),
        ),
        DingtalkAttendanceRaw(
            code="0419000001",
            name="张一奴斯",
            natual_date="2026-07-01",
            att_time=datetime(2026, 7, 1, 10),
        ),
    ])
    db_session.commit()

    by_id = client.get(
        "/api/v1/admin/dingtalk-attendance/raw",
        params={**RANGE, "employeeId": "210127"},
        headers=HR,
    )
    assert by_id.status_code == 200
    assert by_id.json()["total"] == 1
    assert [item["code"] for item in by_id.json()["items"]] == ["0321012706"]

    by_name = client.get(
        "/api/v1/admin/dingtalk-attendance/raw",
        params={**RANGE, "employeeName": "一奴"},
        headers=HR,
    )
    assert by_name.status_code == 200
    assert by_name.json()["total"] == 2
    assert {item["code"] for item in by_name.json()["items"]} == {
        "0321012706",
        "0419000001",
    }

    combined = client.get(
        "/api/v1/admin/dingtalk-attendance/raw",
        params={
            **RANGE,
            "employeeId": "0321",
            "employeeName": "一奴",
            "limit": 1,
        },
        headers=HR,
    )
    assert combined.status_code == 200
    assert combined.json()["total"] == 1
    assert combined.json()["count"] == 1
    assert combined.json()["items"][0]["code"] == "0321012706"


def test_hr_attendance_pull_narrows_around_its_own_region_month_lock(
    client, db_session, monkeypatch
):
    """工作点级月锁不再整批拒绝：封账工作点的员工被剔除，其余照常同步。"""
    from app.models import AttendanceRecord
    from app.services.monthly_export_lock_service import set_month_locks

    furnace_a = Furnace(region="月锁地区A", furnace_no="月锁测试A")
    furnace_b = Furnace(region="月锁地区B", furnace_no="月锁测试B")
    db_session.add_all([
        Region(code="LOCK_A", name="月锁地区A", sort=105),
        Region(code="LOCK_B", name="月锁地区B", sort=106),
        furnace_a,
        furnace_b,
        Employee(emp_id="LOCK_HR_A", name="A人事", role="hr"),
        Employee(emp_id="LOCK_HR_B", name="B人事", role="hr"),
        Employee(emp_id="LOCK_EMP_LOCKED", name="封账工作点员工", role="worker"),
        Employee(emp_id="LOCK_EMP_FREE", name="未锁工作点员工", role="worker"),
        EmployeeFavorite(emp_id="LOCK_HR_A", kind="region", value="月锁地区A"),
        EmployeeFavorite(emp_id="LOCK_HR_B", kind="region", value="月锁地区B"),
        EmployeeFavorite(emp_id="LOCK_EMP_LOCKED", kind="region", value="月锁地区A"),
        EmployeeFavorite(emp_id="LOCK_EMP_FREE", kind="region", value="月锁地区A"),
    ])
    db_session.flush()
    db_session.add(AttendanceRecord(
        emp_id="LOCK_EMP_LOCKED",
        furnace_id=furnace_a.id,
        squad="早班",
        post="出料工",
        employ_type="正式",
        scan_time=datetime(2026, 7, 1, 10),
        source="扫码",
        natual_date="2026-07-01",
        status="active",
        work_hours=8,
    ))
    db_session.commit()
    set_month_locks(
        db_session,
        month="2026-07",
        furnace_ids=[furnace_a.id],
        locked=True,
        actor="LOCK_HR_A",
    )
    captured: list[list[str] | None] = []

    def capture_start_pull(db, date_from, date_to, actor, **kwargs):
        captured.append(kwargs.get("codes"))
        return {"started": True, "alreadyRunning": False}

    _stub_background_jobs(monkeypatch)
    monkeypatch.setattr(attendance_service, "start_pull", capture_start_pull)

    locked = client.post(
        "/api/v1/admin/dingtalk-attendance/pull",
        json=RANGE,
        headers={"x-test-user": "LOCK_HR_A", "x-test-role": "hr"},
    )
    other_region = client.post(
        "/api/v1/admin/dingtalk-attendance/pull",
        json=RANGE,
        headers={"x-test-user": "LOCK_HR_B", "x-test-role": "hr"},
    )

    # 封账不再拦死整个地区：剔除封账工作点员工后照常启动
    assert locked.status_code == 200
    assert other_region.status_code == 200
    narrowed = captured[0]
    assert narrowed is not None
    assert "LOCK_EMP_LOCKED" not in narrowed
    assert "LOCK_EMP_FREE" in narrowed


def test_hr_attendance_pull_excludes_employees_locked_in_other_region(
    client, db_session, monkeypatch
):
    """冻结口径是全局封账工作点：本地区 HR 拉取时也必须剔除外区已封账的跨地区员工。"""
    from app.models import AttendanceRecord
    from app.services.monthly_export_lock_service import set_month_locks

    furnace_a = Furnace(region="跨锁地区A", furnace_no="跨锁A")
    furnace_b = Furnace(region="跨锁地区B", furnace_no="跨锁B")
    db_session.add_all([
        Region(code="XLOCK_A", name="跨锁地区A", sort=108),
        Region(code="XLOCK_B", name="跨锁地区B", sort=109),
        furnace_a,
        furnace_b,
        Employee(emp_id="XLOCK_HR_A", name="A人事", role="hr"),
        Employee(emp_id="XLOCK_CROSS", name="跨地区员工", role="worker"),
        Employee(emp_id="XLOCK_LOCAL", name="本地区员工", role="worker"),
        EmployeeFavorite(emp_id="XLOCK_HR_A", kind="region", value="跨锁地区A"),
        EmployeeFavorite(emp_id="XLOCK_CROSS", kind="region", value="跨锁地区A"),
        EmployeeFavorite(emp_id="XLOCK_LOCAL", kind="region", value="跨锁地区A"),
    ])
    db_session.flush()
    for emp_id, furnace in (
        ("XLOCK_CROSS", furnace_a),
        ("XLOCK_CROSS", furnace_b),
        ("XLOCK_LOCAL", furnace_a),
    ):
        db_session.add(AttendanceRecord(
            emp_id=emp_id,
            furnace_id=furnace.id,
            squad="早班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime(2026, 7, 1, 10),
            source="扫码",
            natual_date="2026-07-01",
            status="active",
            work_hours=8,
        ))
    db_session.commit()
    set_month_locks(
        db_session,
        month="2026-07",
        furnace_ids=[furnace_b.id],
        locked=True,
        actor="admin",
    )
    captured: list[list[str] | None] = []

    def capture_start_pull(db, date_from, date_to, actor, **kwargs):
        captured.append(kwargs.get("codes"))
        return {"started": True, "alreadyRunning": False}

    _stub_background_jobs(monkeypatch)
    monkeypatch.setattr(attendance_service, "start_pull", capture_start_pull)

    r = client.post(
        "/api/v1/admin/dingtalk-attendance/pull",
        json=RANGE,
        headers={"x-test-user": "XLOCK_HR_A", "x-test-role": "hr"},
    )
    assert r.status_code == 200, r.text
    narrowed = captured[0]
    assert narrowed is not None
    assert "XLOCK_CROSS" not in narrowed
    assert "XLOCK_LOCAL" in narrowed


def test_hr_attendance_pull_rejects_when_all_region_employees_are_locked(
    client, db_session, monkeypatch
):
    """地区内员工全部属于封账工作点时无可同步数据：明确拒绝而不是空跑。"""
    from app.models import AttendanceRecord
    from app.services.monthly_export_lock_service import set_month_locks

    furnace = Furnace(region="全锁地区", furnace_no="全锁工作点")
    db_session.add_all([
        Region(code="LOCK_ALL", name="全锁地区", sort=107),
        furnace,
        Employee(emp_id="ALL_LOCK_HR", name="全锁人事", role="hr"),
        EmployeeFavorite(emp_id="ALL_LOCK_HR", kind="region", value="全锁地区"),
    ])
    db_session.flush()
    db_session.add(AttendanceRecord(
        emp_id="ALL_LOCK_HR",
        furnace_id=furnace.id,
        squad="早班",
        post="出料工",
        employ_type="正式",
        scan_time=datetime(2026, 7, 1, 10),
        source="扫码",
        natual_date="2026-07-01",
        status="active",
        work_hours=8,
    ))
    db_session.commit()
    set_month_locks(
        db_session,
        month="2026-07",
        furnace_ids=[furnace.id],
        locked=True,
        actor="ALL_LOCK_HR",
    )
    _stub_background_jobs(monkeypatch)

    locked = client.post(
        "/api/v1/admin/dingtalk-attendance/pull",
        json=RANGE,
        headers={"x-test-user": "ALL_LOCK_HR", "x-test-role": "hr"},
    )

    assert locked.status_code == 409
    assert "已封账" in locked.json()["detail"]


def test_csv_transfer_endpoints_are_removed():
    registered = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("GET", "/api/v1/admin/dingtalk-attendance/export") not in registered
    assert ("POST", "/api/v1/admin/dingtalk-attendance/import") not in registered


def test_hr_raw_attendance_reads_and_jobs_are_scoped_to_server_region(
    client, db_session, monkeypatch
):
    db_session.add_all([
        Region(code="RAW_LOCAL", name="示例厂区A", sort=90),
        Employee(emp_id="RAW_HR", name="地区人事", role="hr"),
        Employee(emp_id="RAW_LOCAL_EMP", name="本地员工", role="worker"),
        Employee(emp_id="RAW_REMOTE_EMP", name="外地员工", role="worker"),
        EmployeeFavorite(emp_id="RAW_HR", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="RAW_LOCAL_EMP", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="RAW_REMOTE_EMP", kind="region", value="示例厂区B"),
        DingtalkAttendanceRaw(
            code="RAW_LOCAL_EMP",
            name="本地员工",
            natual_date="2026-07-01",
            att_time=datetime(2026, 7, 1, 8),
        ),
        DingtalkAttendanceRaw(
            code="RAW_REMOTE_EMP",
            name="外地员工",
            natual_date="2026-07-01",
            att_time=datetime(2026, 7, 1, 8),
        ),
        ExternalAttendanceResultRaw(
            user_code="RAW_LOCAL_EMP", user_name="本地员工", kq_date="2026-07-01"
        ),
        ExternalAttendanceResultRaw(
            user_code="RAW_REMOTE_EMP", user_name="外地员工", kq_date="2026-07-01"
        ),
        DingtalkSyncState(
            kind="attendance_transform",
            status="running",
            current_root="示例厂区B",
            message="外地任务",
        ),
        DingtalkSyncState(
            kind="attendance_result_pull",
            status="running",
            current_root="示例厂区B",
            message="外地结果任务",
        ),
        AuditLog(
            actor="RAW_HR",
            action="attendance_auto_sync",
            payload=json.dumps({"region": "示例厂区A", "trigger": "manual"}),
        ),
        AuditLog(
            actor="REMOTE_HR",
            action="attendance_auto_sync",
            payload=json.dumps({"region": "示例厂区B", "trigger": "manual"}),
        ),
        AuditLog(
            actor="scheduler",
            action="attendance_auto_sync",
            payload=json.dumps({"region": None, "trigger": "auto"}),
        ),
    ])
    db_session.commit()
    captured: list[tuple[str, list[str] | None]] = []

    monkeypatch.setattr(
        attendance_service,
        "start_pull",
        lambda db, date_from, date_to, actor, **kwargs: (
            captured.append(("pull", kwargs.get("codes")))
            or {"started": True, "alreadyRunning": False}
        ),
    )
    monkeypatch.setattr(
        attendance_service,
        "start_transform",
        lambda db, date_from, date_to, **kwargs: (
            captured.append(("transform", kwargs.get("codes")))
            or {"started": True, "alreadyRunning": False}
        ),
    )
    monkeypatch.setattr(
        attendance_service,
        "start_attendance_result_pull",
        lambda db, date_from, date_to, **kwargs: (
            captured.append(("result", kwargs.get("codes")))
            or {"started": True, "alreadyRunning": False}
        ),
    )
    headers = {"x-test-user": "RAW_HR", "x-test-role": "hr"}

    raw = client.get(
        "/api/v1/admin/dingtalk-attendance/raw", params=RANGE, headers=headers
    ).json()
    result = client.get(
        "/api/v1/admin/dingtalk-attendance/result", params=RANGE, headers=headers
    ).json()
    transform_status = client.get(
        "/api/v1/admin/dingtalk-attendance/transform-status", headers=headers
    ).json()
    result_status = client.get(
        "/api/v1/admin/dingtalk-attendance/result/status", headers=headers
    ).json()
    logs = client.get(
        "/api/v1/admin/dingtalk-attendance/sync-logs", headers=headers
    ).json()
    client.post("/api/v1/admin/dingtalk-attendance/pull", json=RANGE, headers=headers)
    client.post("/api/v1/admin/dingtalk-attendance/transform", json=RANGE, headers=headers)
    client.post("/api/v1/admin/dingtalk-attendance/result/pull", json=RANGE, headers=headers)

    assert [item["code"] for item in raw["items"]] == ["RAW_LOCAL_EMP"]
    assert [item["userCode"] for item in result["items"]] == ["RAW_LOCAL_EMP"]
    assert transform_status["status"] == "running"
    assert transform_status["message"] == "其他地区同步任务占用中"
    assert result_status["status"] == "running"
    assert result_status["message"] == "其他地区同步任务占用中"
    assert {item["actor"] for item in logs["items"]} == {"RAW_HR", "scheduler"}
    assert {name for name, _ in captured} == {"pull", "transform", "result"}
    for _, codes in captured:
        assert "RAW_LOCAL_EMP" in (codes or [])
        assert "RAW_REMOTE_EMP" not in (codes or [])


def test_real_sync_log_keeps_region_and_filters_before_limit(client, db_session):
    db_session.add_all([
        Region(code="RAW_LOG_REGION", name="同步日志地区", sort=92),
        Employee(emp_id="RAW_LOG_HR", name="日志人事", role="hr"),
        EmployeeFavorite(emp_id="RAW_LOG_HR", kind="region", value="同步日志地区"),
    ])
    db_session.commit()
    attendance_service._log_sync(
        db_session,
        actor="RAW_LOG_HR",
        trigger="manual",
        result={
            "dateFrom": "2026-07-01",
            "dateTo": "2026-07-01",
            "region": "同步日志地区",
            "synced": {"fetched": 1, "upserted": 1},
            "transformed": {"inserted": 1, "matched": 1},
            "error": None,
        },
    )
    local_log = db_session.query(AuditLog).filter_by(
        action="attendance_auto_sync", actor="RAW_LOG_HR"
    ).one()
    assert json.loads(local_log.payload)["region"] == "同步日志地区"

    # 最新一条故意属于其它地区；若仍在 LIMIT 后过滤，limit=1 会错误返回空列表。
    local_log.ts = datetime(2026, 7, 1, 8)
    db_session.add(AuditLog(
        actor="REMOTE_LOG_HR",
        action="attendance_auto_sync",
        payload=json.dumps({"region": "其它地区", "trigger": "manual"}),
        ts=datetime(2026, 7, 2, 8),
    ))
    db_session.commit()

    response = client.get(
        "/api/v1/admin/dingtalk-attendance/sync-logs",
        params={"limit": 1},
        headers={"x-test-user": "RAW_LOG_HR", "x-test-role": "hr"},
    )
    assert response.status_code == 200
    assert [item["actor"] for item in response.json()["items"]] == ["RAW_LOG_HR"]


def test_region_status_keeps_its_error_when_another_region_starts(db_session):
    local = attendance_service._task_state(
        db_session, attendance_service.TRANSFORM_KIND, "状态地区甲"
    )
    attendance_service._transform_update(
        db_session,
        local,
        status="error",
        current_root="状态地区甲",
        message="甲地区同步失败",
    )
    remote = attendance_service._task_state(
        db_session, attendance_service.TRANSFORM_KIND, "状态地区乙"
    )
    attendance_service._transform_update(
        db_session,
        remote,
        status="running",
        current_root="状态地区乙",
        message="乙地区同步中",
    )

    status = attendance_service.get_transform_status(db_session, "状态地区甲")

    assert status["status"] == "error"
    assert status["message"] == "甲地区同步失败"


def test_stale_running_status_recovers_without_resetting_attendance(db_session):
    stale = attendance_service._task_state(
        db_session, attendance_service.TRANSFORM_KIND, "僵尸地区"
    )
    attendance_service._transform_update(
        db_session,
        stale,
        status="running",
        current_root="僵尸地区",
    )
    stale.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.commit()

    status = attendance_service.get_transform_status(db_session, "僵尸地区")

    assert status["status"] == "error"
    assert "可重新发起" in status["message"]


def test_active_database_lease_prevents_other_worker_from_recovering_state(
    db_session,
):
    stale = attendance_service._task_state(
        db_session, attendance_service.TRANSFORM_KIND, "跨进程地区"
    )
    attendance_service._transform_update(
        db_session,
        stale,
        status="running",
        current_root="跨进程地区",
        message="另一个 worker 正在同步",
    )
    stale.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.commit()
    token = attendance_service._claim_task_lease(db_session)

    try:
        status = attendance_service.get_transform_status(
            db_session, "跨进程地区"
        )
        assert status["status"] == "running"
        assert status["message"] == "另一个 worker 正在同步"
        assert attendance_service._claim_task_lease(db_session) is None
    finally:
        attendance_service._release_task_lease(db_session, token)


def test_old_owner_cannot_release_database_lease_after_takeover(db_session):
    old_token = attendance_service._claim_task_lease(db_session)
    lease = attendance_service._task_state(
        db_session, attendance_service.TASK_LEASE_KIND
    )
    lease.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db_session.commit()

    new_token = attendance_service._claim_task_lease(db_session)
    attendance_service._release_task_lease(db_session, old_token)
    db_session.expire_all()
    lease = attendance_service._task_state(
        db_session, attendance_service.TASK_LEASE_KIND
    )

    assert new_token and new_token != old_token
    assert lease.owner_token == new_token
    attendance_service._release_task_lease(db_session, new_token)


def test_database_lease_claim_error_releases_process_lock(db_session, monkeypatch):
    def fail_claim(_db):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(attendance_service, "_claim_task_lease", fail_claim)

    with pytest.raises(RuntimeError, match="database unavailable"):
        attendance_service.start_transform(
            db_session, "2026-07-01", "2026-07-01"
        )
    assert attendance_service._transform_lock.acquire(blocking=False)
    attendance_service._transform_lock.release()


def test_region_state_keys_fit_the_database_column():
    transform = attendance_service._state_kind(
        attendance_service.TRANSFORM_KIND, "示例厂区A"
    )
    result = attendance_service._state_kind(
        attendance_service.ATT_RESULT_KIND, "示例厂区A"
    )

    assert len(transform) <= 32
    assert len(result) <= 32
    assert result.startswith("attendance_result_pull:")
    assert result != attendance_service._state_kind(
        attendance_service.ATT_RESULT_KIND, "示例厂区B"
    )


def test_task_state_recovers_when_another_request_wins_insert_race():
    winner = DingtalkSyncState(
        kind="attendance_transform:1234567890", status="idle"
    )

    class RacingSession:
        def __init__(self):
            self.scalar_calls = 0
            self.rolled_back = False

        def scalar(self, _statement):
            self.scalar_calls += 1
            return None if self.scalar_calls == 1 else winner

        def add(self, _row):
            return None

        def commit(self):
            raise IntegrityError("insert", {}, Exception("duplicate"))

        def rollback(self):
            self.rolled_back = True

    session = RacingSession()
    state = attendance_service._task_state(
        session, attendance_service.TRANSFORM_KIND, "并发地区"
    )

    assert state is winner
    assert session.rolled_back is True
