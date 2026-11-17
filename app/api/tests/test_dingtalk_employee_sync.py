"""通讯录部门树必须以本次钉钉返回结果为准，不能复用失效缓存。"""

from sqlalchemy import select

from app.models import (
    AuditLog,
    DingtalkDepartmentRaw,
    DingtalkEmployeeRaw,
    DingtalkSyncState,
    Employee,
)
from app.services import dingtalk_employee_service as svc


ROOT_ID = 100
CURRENT_DEPT_ID = 101
STALE_DEPT_ID = 999
ROOT_LABEL = "测试根部门"


def _state(db_session):
    state = DingtalkSyncState(kind="roster", status="running")
    db_session.add(state)
    db_session.commit()
    return state


def _sqlite_upsert_dept(db, dept_id, parent_id, name, layer, face_count, root_dept_id):
    row = db.scalar(
        select(DingtalkDepartmentRaw).where(DingtalkDepartmentRaw.dept_id == dept_id)
    )
    if row is None:
        row = DingtalkDepartmentRaw(dept_id=dept_id)
        db.add(row)
    row.parent_id = parent_id
    row.name = name
    row.layer = layer
    row.face_count = face_count
    row.root_dept_id = root_dept_id


def test_start_sync_persists_running_before_background_thread(db_session, monkeypatch):
    db_session.add(DingtalkSyncState(kind="roster", status="done", phase="完成"))
    db_session.commit()

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(svc.threading, "Thread", FakeThread)
    try:
        result = svc.start_sync(db_session)
        status = svc.get_state(db_session)
    finally:
        svc.release_directory_sync()

    assert result["started"] is True
    assert status["status"] == "running"
    assert status["phase"] == "等待执行"
    assert status["startedAt"] is not None


def test_sync_uses_fresh_department_tree_and_removes_stale_cache(db_session, monkeypatch):
    db_session.add(
        DingtalkDepartmentRaw(
            dept_id=STALE_DEPT_ID,
            parent_id=ROOT_ID,
            name="已删除部门",
            layer=f"{ROOT_LABEL}/已删除部门",
            root_dept_id=ROOT_ID,
        )
    )
    db_session.commit()
    state = _state(db_session)

    def fake_oapi(path, token, payload):
        assert path == "/topapi/v2/department/listsub"
        if payload["dept_id"] == ROOT_ID:
            return {
                "result": [
                    {
                        "dept_id": CURRENT_DEPT_ID,
                        "parent_id": ROOT_ID,
                        "name": "当前部门",
                    }
                ]
            }
        assert payload["dept_id"] == CURRENT_DEPT_ID
        return {"result": []}

    monkeypatch.setattr(svc, "_oapi", fake_oapi)
    monkeypatch.setattr(svc, "_upsert_dept", _sqlite_upsert_dept)

    paths, current_depts = svc.sync_departments(
        db_session,
        "token",
        {ROOT_ID: ROOT_LABEL},
        state,
        {ROOT_LABEL: ROOT_ID},
    )

    assert current_depts == {ROOT_ID: [ROOT_ID, CURRENT_DEPT_ID]}
    assert paths == {
        ROOT_ID: ROOT_LABEL,
        CURRENT_DEPT_ID: f"{ROOT_LABEL}/当前部门",
    }
    assert db_session.scalar(
        select(DingtalkDepartmentRaw).where(DingtalkDepartmentRaw.dept_id == STALE_DEPT_ID)
    ) is None


def test_member_sync_never_queries_stale_cached_department(db_session, monkeypatch):
    db_session.add_all(
        [
            DingtalkDepartmentRaw(
                dept_id=CURRENT_DEPT_ID,
                parent_id=ROOT_ID,
                name="当前部门",
                layer=f"{ROOT_LABEL}/当前部门",
                root_dept_id=ROOT_ID,
            ),
            DingtalkDepartmentRaw(
                dept_id=STALE_DEPT_ID,
                parent_id=ROOT_ID,
                name="已删除部门",
                layer=f"{ROOT_LABEL}/已删除部门",
                root_dept_id=ROOT_ID,
            ),
        ]
    )
    db_session.commit()
    state = _state(db_session)
    queried: list[int] = []

    def fake_oapi(path, token, payload):
        assert path == "/topapi/v2/user/list"
        queried.append(payload["dept_id"])
        return {"result": {"list": [], "has_more": False}}

    monkeypatch.setattr(svc, "_oapi", fake_oapi)

    failed = svc.sync_members(
        db_session,
        "token",
        {
            ROOT_ID: ROOT_LABEL,
            CURRENT_DEPT_ID: f"{ROOT_LABEL}/当前部门",
        },
        state,
        {ROOT_LABEL: ROOT_ID},
        {ROOT_ID: [ROOT_ID, CURRENT_DEPT_ID]},
    )

    assert queried == [ROOT_ID, CURRENT_DEPT_ID]
    assert STALE_DEPT_ID not in queried
    assert failed == []
    assert state.member_total_depts == 2
    assert state.member_done_depts == 2


def test_member_sync_skips_failed_department_and_reports_reason(db_session, monkeypatch):
    state = _state(db_session)
    failed_id = 102
    queried: list[int] = []

    def fake_oapi(path, token, payload):
        did = payload["dept_id"]
        queried.append(did)
        if did == failed_id:
            raise RuntimeError("/topapi/v2/user/list: errcode=60003 部门不存在")
        return {"result": {"list": [], "has_more": False}}

    monkeypatch.setattr(svc, "_oapi", fake_oapi)

    failed = svc.sync_members(
        db_session,
        "token",
        {
            ROOT_ID: ROOT_LABEL,
            failed_id: f"{ROOT_LABEL}/已删除部门",
            CURRENT_DEPT_ID: f"{ROOT_LABEL}/当前部门",
        },
        state,
        {ROOT_LABEL: ROOT_ID},
        {ROOT_ID: [ROOT_ID, failed_id, CURRENT_DEPT_ID]},
    )

    assert queried == [ROOT_ID, failed_id, CURRENT_DEPT_ID]
    assert failed == [
        {
            "deptId": failed_id,
            "deptPath": f"{ROOT_LABEL}/已删除部门",
            "reason": "/topapi/v2/user/list: errcode=60003 部门不存在",
        }
    ]
    assert state.member_total_depts == 3
    assert state.member_done_depts == 3
    summary = svc._member_failure_summary(failed)
    assert "跳过 1 个失败部门" in summary
    assert "60003 部门不存在" in summary


def test_directory_status_sync_marks_departed_and_reactivated_without_deleting(db_session):
    db_session.add_all([
        Employee(
            emp_id="STATUS_ACTIVE",
            name="恢复在职",
            dd_user_id="dd-status-active",
            employment_status="departed",
        ),
        Employee(
            emp_id="STATUS_DEPARTED",
            name="同步离职",
            dd_user_id="dd-status-departed",
            employment_status="active",
        ),
        Employee(
            emp_id="STATUS_UNKNOWN",
            name="未知保留",
            dd_user_id="dd-status-unknown",
            employment_status="departed",
        ),
        DingtalkEmployeeRaw(
            userid="dd-status-active",
            job_number="STATUS_ACTIVE",
            active=True,
        ),
        DingtalkEmployeeRaw(
            userid="dd-status-departed",
            job_number="STATUS_DEPARTED",
            active=False,
        ),
        DingtalkEmployeeRaw(
            userid="dd-status-unknown",
            job_number="STATUS_UNKNOWN",
            active=None,
        ),
    ])
    db_session.commit()

    result = svc._sync_employment_statuses(db_session, {
        "dd-status-active", "dd-status-departed", "dd-status-unknown",
    })

    assert result == {
        "matched": 2,
        "changed": 2,
        "active": 1,
        "departed": 1,
        "unknown": 1,
        "conflicts": 0,
    }
    employees = {
        employee.emp_id: employee
        for employee in db_session.query(Employee).filter(
            Employee.emp_id.like("STATUS_%")
        ).all()
    }
    assert employees["STATUS_ACTIVE"].employment_status == "active"
    assert employees["STATUS_DEPARTED"].employment_status == "departed"
    assert employees["STATUS_UNKNOWN"].employment_status == "departed"
    assert all(employee.deleted is False for employee in employees.values())
    assert db_session.query(AuditLog).filter_by(
        action="employee_employment_status_sync"
    ).count() == 2
