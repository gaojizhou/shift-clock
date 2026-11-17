import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import (
    AuditLog,
    DingtalkEmployeeSyncRun,
    DingtalkEmployeeRaw,
    Employee,
    EmployeeFavorite,
    Region,
    RosterRootDept,
    RosterTitleMap,
)
from app.services import employee_difference_sync_service as sync_service
from app.services.employee_difference_sync_service import (
    apply_rule,
    preview,
)


def _login(client):
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})


def _seed(db):
    db.add_all([
        Region(code="AUTO_REG", name="自动地区", sort=90),
        RosterRootDept(
            label="自动根部门", dept_id=88001, region_code="AUTO_REG", sort=90
        ),
        RosterTitleMap(title="自动职位", role="worker", post="自动岗位", sort=90),
        Employee(emp_id="AUTO1", name="普通一", role="worker"),
        Employee(emp_id="AUTO2", name="普通二", role="worker"),
        Employee(emp_id="AUTO_HR", name="保护人事", role="hr"),
        EmployeeFavorite(emp_id="AUTO1", kind="region", value="旧地区"),
        EmployeeFavorite(emp_id="AUTO1", kind="post", value="旧岗位"),
        DingtalkEmployeeRaw(
            userid="AUTO1", job_number="AUTO1", name="普通一", title="自动职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/35号工作点",
        ),
        DingtalkEmployeeRaw(
            userid="AUTO2", job_number="AUTO2", name="普通二", title="自动职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/45号工作点",
        ),
        DingtalkEmployeeRaw(
            userid="AUTO_HR", job_number="AUTO_HR", name="保护人事", title="自动职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/35号工作点",
        ),
    ])
    db.commit()


def _rule(field="region", **values):
    rule = {
        "field": field,
        "enabled": True,
        "scope": "mapped",
        "updateMode": "append" if field != "role" else "replace",
        "differenceTypes": [],
        "skipMulti": True,
    }
    rule.update(values)
    return rule


def test_preview_filters_and_reports_actions(db_session):
    _seed(db_session)
    result = preview(
        db_session,
        _rule(
            localRegions=["旧地区"],
            currentRoles=["worker"],
            remoteTitles=["自动职位"],
            departments=["自动根部门"],
            furnaces=["35号工作点"],
            differenceTypes=["missing_remote"],
        ),
    )
    assert result["matched"] == 1
    item = result["items"][0]
    assert item["empId"] == "AUTO1"
    assert item["action"] == "append"
    assert item["willAdd"] == ["自动地区"]
    assert item["willRemove"] == []

    # 即使已包含远程值，替换模式仍应识别并删除其他旧值。
    db_session.add(EmployeeFavorite(emp_id="AUTO1", kind="region", value="自动地区"))
    db_session.commit()
    # 同时属于“包含远程值”和“本地多值”，用 multi 选择该替换场景。
    replaced = preview(
        db_session,
        _rule(updateMode="replace", q="AUTO1", differenceTypes=["multi"], skipMulti=False),
    )
    assert replaced["willUpdate"] == 1
    assert replaced["items"][0]["willRemove"] == ["旧地区"]


def test_preview_scopes_multi_and_protected(db_session):
    _seed(db_session)
    db_session.add(EmployeeFavorite(emp_id="AUTO1", kind="region", value="第二地区"))
    db_session.commit()

    multi_skipped = preview(
        db_session, _rule(q="AUTO1", differenceTypes=["multi"], skipMulti=True)
    )
    assert multi_skipped["skipped"] == 1
    assert "多个" in multi_skipped["items"][0]["skipReason"]

    local_empty = preview(
        db_session, _rule(scope="local_empty", differenceTypes=["empty_remote"])
    )
    assert {item["empId"] for item in local_empty["items"]} == {"AUTO2", "AUTO_HR"}
    assert next(item for item in local_empty["items"] if item["empId"] == "AUTO2")["action"] == "append"

    protected = preview(
        db_session, _rule(q="AUTO_HR", differenceTypes=["empty_remote"])
    )
    assert protected["skipped"] == 1
    assert "admin/HR/统计" in protected["items"][0]["skipReason"]


def test_whitelist_blocks_preview_apply_and_legacy_apply(client, db_session):
    _seed(db_session)
    _login(client)
    added = client.post(
        "/api/v1/admin/dingtalk-employee/sync-whitelist",
        json={"empIds": ["AUTO2"]},
    )
    assert added.status_code == 200

    blocked = preview(
        db_session, _rule(q="AUTO2", scope="local_empty", differenceTypes=["empty_remote"])
    )
    assert blocked["skipped"] == 1
    assert blocked["items"][0]["whitelisted"] is True

    applied = apply_rule(db_session, _rule(q="AUTO2"), "admin", "manual")
    assert applied["changed"] == 0
    legacy = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["AUTO2"], "field": "region"},
    ).json()
    assert legacy["changed"] == 0
    assert legacy["skippedItems"][0]["reason"] == "员工在同步白名单中"


def test_config_defaults_off_and_persists_independent_rules(client, db_session):
    _login(client)
    initial = client.get(
        "/api/v1/admin/dingtalk-employee/auto-sync-config"
    ).json()
    assert initial["enabled"] is False
    assert initial["rules"]["region"]["updateMode"] == "append"

    rules = initial["rules"]
    rules["region"]["enabled"] = True
    rules["region"]["scope"] = "local_empty"
    saved = client.put(
        "/api/v1/admin/dingtalk-employee/auto-sync-config",
        json={"enabled": True, "rules": rules},
    )
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["rules"]["region"]["scope"] == "local_empty"
    assert saved.json()["rules"]["post"]["enabled"] is False


def test_inferred_chief_is_protected_and_rules_never_demote_chiefs(db_session):
    _seed(db_session)
    db_session.add_all([
        Employee(emp_id="L9001", name="推断值班长", dd_user_id="123456789", role=None),
        Employee(emp_id="AUTO_CHIEF", name="显式值班长", role="chief"),
        DingtalkEmployeeRaw(
            userid="L9001", job_number="L9001", name="推断值班长", title="自动职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/35号工作点",
        ),
        DingtalkEmployeeRaw(
            userid="AUTO_CHIEF", job_number="AUTO_CHIEF", name="显式值班长", title="自动职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/35号工作点",
        ),
    ])
    db_session.commit()

    inferred = preview(
        db_session,
        _rule("role", q="L9001", updateMode="fill_empty", differenceTypes=[]),
    )
    assert inferred["items"][0]["current"] == "chief"
    assert inferred["items"][0]["action"] == "skip"
    assert "不执行值班长降级" in inferred["items"][0]["skipReason"]

    manual = apply_rule(
        db_session,
        _rule("role", q="AUTO_CHIEF", updateMode="replace", differenceTypes=[]),
        "admin",
        "manual",
    )
    assert manual["changed"] == 0
    assert manual["skipReasons"]["规则同步不执行值班长降级，请走员工管理页手工调整"] == 1
    assert db_session.query(Employee).filter_by(emp_id="AUTO_CHIEF").one().role == "chief"


def test_role_fill_empty_uses_persisted_role_nullness(db_session):
    _seed(db_session)
    db_session.add_all([
        Employee(emp_id="AUTO_EMPTY_ROLE", name="空角色", role=None),
        DingtalkEmployeeRaw(
            userid="AUTO_EMPTY_ROLE",
            job_number="AUTO_EMPTY_ROLE",
            name="空角色",
            title="自动职位",
            active=True,
            root_label="自动根部门",
            dept_path="自动根部门/35号工作点",
        ),
    ])
    db_session.commit()

    rule = _rule(
        "role",
        q="AUTO_EMPTY_ROLE",
        scope="local_empty",
        updateMode="fill_empty",
        differenceTypes=["empty_remote"],
    )
    result = preview(db_session, rule)
    assert result["willUpdate"] == 1
    assert result["items"][0]["action"] == "fill"

    applied = apply_rule(db_session, rule, "admin", "manual")
    assert applied["changed"] == 1
    assert db_session.query(Employee).filter_by(emp_id="AUTO_EMPTY_ROLE").one().role == "worker"


def test_apply_rechecks_protected_role_after_preview(db_session):
    _seed(db_session)
    employee = db_session.query(Employee).filter_by(emp_id="AUTO2").one()
    employee.role = None
    db_session.commit()
    rule = _rule(
        "role",
        q="AUTO2",
        scope="local_empty",
        updateMode="fill_empty",
        differenceTypes=["empty_remote"],
    )
    snapshot = preview(db_session, rule)
    assert snapshot["items"][0]["action"] == "fill"

    employee.role = "hr"
    db_session.commit()
    applied = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )

    assert applied["changed"] == 0
    assert applied["skippedItems"][0]["reason"] == "当前或目标为 admin/HR/统计"
    assert employee.role == "hr"


def test_apply_rechecks_fill_empty_after_preview(db_session):
    _seed(db_session)
    rule = _rule(
        "region",
        q="AUTO2",
        scope="local_empty",
        updateMode="fill_empty",
        differenceTypes=["empty_remote"],
    )
    snapshot = preview(db_session, rule)
    assert snapshot["items"][0]["action"] == "fill"

    db_session.add(EmployeeFavorite(emp_id="AUTO2", kind="region", value="人工地区"))
    db_session.commit()
    applied = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )

    assert applied["changed"] == 0
    assert applied["skippedItems"][0]["reason"] == "本地已有值，仅填空模式不修改"
    values = {
        row.value
        for row in db_session.query(EmployeeFavorite).filter_by(
            emp_id="AUTO2", kind="region"
        )
    }
    assert values == {"人工地区"}


def test_apply_rechecks_whitelist_and_deleted_employee_after_preview(db_session):
    _seed(db_session)
    rule = _rule(q="AUTO2", scope="local_empty", differenceTypes=["empty_remote"])
    snapshot = preview(db_session, rule)
    assert snapshot["willUpdate"] == 1

    sync_service.add_whitelist(db_session, ["AUTO2"], "admin")
    blocked = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )
    assert blocked["changed"] == 0
    assert blocked["skippedItems"] == [
        {"empId": "AUTO2", "reason": "同步白名单"}
    ]

    sync_service.remove_whitelist(db_session, "AUTO2", "admin")
    employee = db_session.query(Employee).filter_by(emp_id="AUTO2").one()
    employee.deleted = True
    db_session.commit()
    deleted = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )
    assert deleted["changed"] == 0
    assert deleted["skippedItems"] == [
        {"empId": "AUTO2", "reason": "员工已删除或远程记录已失效"}
    ]


def test_apply_accumulates_preview_and_runtime_skip_reasons(db_session):
    _seed(db_session)
    employee = db_session.query(Employee).filter_by(emp_id="AUTO2").one()
    employee.role = None
    db_session.commit()
    rule = _rule(
        "role",
        q="AUTO2",
        scope="local_empty",
        updateMode="fill_empty",
        differenceTypes=["empty_remote"],
    )
    snapshot = preview(db_session, rule)
    assert snapshot["willUpdate"] == 1
    # 模拟同一预检已有一个同原因跳过项，验证运行期复检不会覆盖预检计数。
    snapshot["skipReasons"] = {"当前或目标为 admin/HR/统计": 1}
    snapshot["skipped"] = 1

    employee.role = "hr"
    db_session.commit()
    applied = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )
    assert applied["skipReasons"]["当前或目标为 admin/HR/统计"] == 2
    assert sum(applied["skipReasons"].values()) == applied["skipped"]


def test_apply_skips_role_when_remote_snapshot_disappears(db_session):
    _seed(db_session)
    employee = db_session.query(Employee).filter_by(emp_id="AUTO2").one()
    employee.role = None
    db_session.commit()
    rule = _rule(
        "role",
        q="AUTO2",
        scope="local_empty",
        updateMode="fill_empty",
        differenceTypes=["empty_remote"],
    )
    snapshot = preview(db_session, rule)
    raw = db_session.query(DingtalkEmployeeRaw).filter_by(job_number="AUTO2").one()
    db_session.delete(raw)
    db_session.commit()

    applied = apply_rule(
        db_session, rule, "admin", "manual", precomputed=snapshot
    )

    assert applied["changed"] == 0
    assert applied["skippedItems"] == [
        {"empId": "AUTO2", "reason": "员工已删除或远程记录已失效"}
    ]


def test_legacy_replace_preview_reports_values_that_apply_will_remove(db_session):
    _seed(db_session)
    db_session.add(EmployeeFavorite(emp_id="AUTO1", kind="region", value="自动地区"))
    db_session.commit()

    result = sync_service.directory.list_employee_differences(
        db_session,
        q="AUTO1",
        field="region",
        update_mode="replace",
    )
    assert result["total"] == 1
    field = result["items"][0]["fields"]["region"]
    assert field["different"] is True
    assert field["willRemove"] == ["旧地区"]


def test_apply_phase_error_does_not_overwrite_directory_status(monkeypatch, db_session):
    run = DingtalkEmployeeSyncRun(
        trigger="manual",
        actor="admin",
        status="running",
        phase="等待执行",
        rule_json="[]",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    marked: list[str] = []
    test_session_local = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(sync_service, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        sync_service.directory,
        "run_directory_sync_locked",
        lambda _db: {"refreshedEmpIds": [], "failedDepts": []},
    )
    monkeypatch.setattr(
        sync_service.directory,
        "mark_directory_sync_error",
        lambda _db, error: marked.append(str(error)),
    )
    monkeypatch.setattr(
        sync_service,
        "apply_rule",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("应用阶段失败")),
    )

    assert sync_service.directory.try_acquire_directory_sync() is True
    sync_service._execute_run(run.id, [_rule("region")])

    db_session.expire_all()
    saved = db_session.get(DingtalkEmployeeSyncRun, run.id)
    assert saved.status == "error"
    assert "region 失败：应用阶段失败" in saved.message
    assert json.loads(saved.result_json)["failedFields"] == [
        {"field": "region", "error": "应用阶段失败"}
    ]
    assert marked == []


def test_all_rule_limits_are_preflighted_before_any_field_is_applied(
    monkeypatch, db_session
):
    run = DingtalkEmployeeSyncRun(
        trigger="manual",
        actor="admin",
        status="running",
        phase="等待执行",
        rule_json="[]",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    test_session_local = sessionmaker(bind=db_session.get_bind())
    applied: list[str] = []
    monkeypatch.setattr(sync_service, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        sync_service.directory,
        "run_directory_sync_locked",
        lambda _db: {"refreshedEmpIds": [], "failedDepts": []},
    )
    monkeypatch.setattr(
        sync_service,
        "preview",
        lambda _db, rule, **_kwargs: {
            "willUpdate": 501 if rule["field"] == "post" else 1
        },
    )
    monkeypatch.setattr(
        sync_service,
        "apply_rule",
        lambda _db, rule, *_args, **_kwargs: applied.append(rule["field"]),
    )

    assert sync_service.directory.try_acquire_directory_sync() is True
    sync_service._execute_run(run.id, [_rule("region"), _rule("post")])

    db_session.expire_all()
    saved = db_session.get(DingtalkEmployeeSyncRun, run.id)
    assert saved.status == "error"
    assert "post 命中条数超过单次上限 500" in saved.message
    assert applied == []


def test_preflight_snapshots_are_reused_when_rules_are_applied(monkeypatch, db_session):
    run = DingtalkEmployeeSyncRun(
        trigger="manual",
        actor="admin",
        status="running",
        phase="等待执行",
        rule_json="[]",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    test_session_local = sessionmaker(bind=db_session.get_bind())
    checks: dict[str, dict] = {}
    received: dict[str, dict] = {}
    monkeypatch.setattr(sync_service, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        sync_service.directory,
        "run_directory_sync_locked",
        lambda _db: {"refreshedEmpIds": ["E1"], "failedDepts": []},
    )

    def fake_preview(_db, rule, **_kwargs):
        check = {"willUpdate": 1, "rule": rule, "items": [{"empId": "E1"}]}
        checks[rule["field"]] = check
        return check

    def fake_apply(_db, rule, *_args, precomputed=None, **_kwargs):
        received[rule["field"]] = precomputed
        return {"field": rule["field"], "changed": 1}

    monkeypatch.setattr(sync_service, "preview", fake_preview)
    monkeypatch.setattr(sync_service, "apply_rule", fake_apply)

    assert sync_service.directory.try_acquire_directory_sync() is True
    sync_service._execute_run(run.id, [_rule("region"), _rule("post")])

    assert received == checks


def test_chained_rules_recheck_filter_before_writing(monkeypatch, db_session):
    """多规则串联：前一条规则改写角色后，后一条规则不得再按旧快照写入已不匹配的员工。"""
    _seed(db_session)
    db_session.add_all([
        RosterTitleMap(title="连锁职位", role="leader", post="连锁岗位", sort=91),
        Employee(emp_id="CHAIN1", name="连锁员工", role="worker"),
        DingtalkEmployeeRaw(
            userid="CHAIN1", job_number="CHAIN1", name="连锁员工", title="连锁职位",
            active=True, root_label="自动根部门", dept_path="自动根部门/35号工作点",
        ),
    ])
    db_session.commit()

    run = DingtalkEmployeeSyncRun(
        trigger="manual",
        actor="admin",
        status="running",
        phase="等待执行",
        rule_json="[]",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    test_session_local = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(sync_service, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        sync_service.directory,
        "run_directory_sync_locked",
        lambda _db: {"refreshedEmpIds": ["CHAIN1"], "failedDepts": []},
    )

    assert sync_service.directory.try_acquire_directory_sync() is True
    sync_service._execute_run(
        run.id,
        [
            _rule("role", currentRoles=["worker"]),
            _rule("post", currentRoles=["worker"]),
        ],
    )

    db_session.expire_all()
    saved = db_session.get(DingtalkEmployeeSyncRun, run.id)
    assert saved.status == "done"
    employee = db_session.query(Employee).filter_by(emp_id="CHAIN1").one()
    assert employee.role == "leader"
    # role 已被前一条规则改为 leader，post 规则的 currentRoles=worker 复核不通过
    posts = {
        row.value
        for row in db_session.query(EmployeeFavorite).filter_by(
            emp_id="CHAIN1", kind="post"
        )
    }
    assert "连锁岗位" not in posts
    summary = json.loads(saved.result_json)
    post_field = next(item for item in summary["fields"] if item["field"] == "post")
    assert post_field["changed"] == 0
    assert {item["empId"] for item in post_field["skippedItems"]} == {"CHAIN1"}
    assert post_field["skipReasons"]["应用前复核不再满足规则筛选条件（员工状态已被同批前序规则变更）"] == 1


def test_unrelated_running_auto_run_is_not_marked_as_startup_failure(db_session):
    running = DingtalkEmployeeSyncRun(
        trigger="auto",
        actor="scheduler",
        status="running",
        phase="同步region",
        rule_json="[]",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(running)
    db_session.commit()

    sync_service._record_automatic_failure(db_session, ValueError("配置读取失败"))

    db_session.expire_all()
    assert db_session.get(DingtalkEmployeeSyncRun, running.id).status == "running"
    newest = db_session.query(DingtalkEmployeeSyncRun).order_by(
        DingtalkEmployeeSyncRun.id.desc()
    ).first()
    assert newest.id != running.id
    assert newest.status == "error"
    assert newest.phase == "启动失败"


def test_start_run_does_not_release_lock_after_thread_handoff(monkeypatch, db_session):
    released: list[bool] = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(sync_service.directory, "try_acquire_directory_sync", lambda: True)
    monkeypatch.setattr(
        sync_service.directory,
        "release_directory_sync",
        lambda: released.append(True),
    )
    monkeypatch.setattr(sync_service.threading, "Thread", FakeThread)
    result = sync_service.start_run(
        db_session, [_rule("region")], "admin", "manual"
    )

    assert result["status"] == "running"
    assert result["id"] is not None
    assert released == []


def test_automatic_job_records_unexpected_startup_failure(monkeypatch, db_session):
    test_session_local = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(sync_service, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        sync_service,
        "get_config",
        lambda _db: (_ for _ in ()).throw(ValueError("rules_json 非法")),
    )

    result = sync_service.automatic_sync_job()

    assert result["started"] is False
    assert result["errorType"] == "ValueError"
    db_session.expire_all()
    run = db_session.query(DingtalkEmployeeSyncRun).order_by(
        DingtalkEmployeeSyncRun.id.desc()
    ).first()
    assert run is not None
    assert run.trigger == "auto"
    assert run.status == "error"
    assert "ValueError: rules_json 非法" in run.message
    audit = db_session.query(AuditLog).filter_by(
        action="employee_difference_sync_run", actor="scheduler"
    ).one()
    payload = json.loads(audit.payload)
    assert payload["runId"] == run.id
    assert payload["errorType"] == "ValueError"
