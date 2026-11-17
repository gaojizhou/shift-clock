"""Review 修复回归：补录/对账重算的地区校验、停用地区前校验 HR/统计账号归属。"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import (
    AttendanceRecord,
    AuditLog,
    Employee,
    EmployeeFavorite,
    Furnace,
    FurnaceChief,
    ExternalAttendanceResultRaw,
    NotificationRecord,
    ReconResult,
    Region,
)


def _seed_remote_region(db):
    """新增一个外地地区 + 工作点 + 值班长，用于跨地区用例；同时补示例厂区A字典行
    （服务端地区范围按 Region 表校验，conftest 种子未建该行）。"""
    db.add_all([
        Region(code="RG_LOCAL", name="示例厂区A", sort=1),
        Region(code="RG_REMOTE", name="外地地区", sort=210),
        Employee(emp_id="RM_CHIEF", name="外地值班长", role="chief"),
        Employee(emp_id="RM_WORKER", name="外地工人", role="worker"),
    ])
    db.flush()
    furnace = Furnace(
        region="外地地区",
        furnace_no="外地1号工作点",
        furnace_type_code="FT01",
        name="外地1号工作点",
    )
    db.add(furnace)
    db.flush()
    db.add(FurnaceChief(furnace_id=furnace.id, emp_id="RM_CHIEF"))
    db.commit()
    return furnace


def test_chief_review_does_not_leak_same_numbered_furnace_from_other_region(
    client, db_session
):
    """值班长审核列表必须按 furnace_id 隔离，不能把外区同号工作点员工混入。"""
    _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    remote_same_number = Furnace(
        region="外地地区",
        furnace_no="35号工作点",
        furnace_type_code="FT01",
        name="外地35号工作点",
    )
    db_session.add(remote_same_number)
    db_session.flush()

    date = "2026-06-04"
    db_session.add_all(
        [
            AttendanceRecord(
                emp_id="T0001",
                furnace_id=local_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                scan_time=datetime(2026, 6, 4, 8, 0, 0),
                natual_date=date,
            ),
            AttendanceRecord(
                emp_id="RM_WORKER",
                furnace_id=remote_same_number.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                scan_time=datetime(2026, 6, 4, 8, 0, 0),
                natual_date=date,
            ),
            ReconResult(
                emp_id="T0001",
                date=date,
                status="工作点有劳勤无",
                hr_status="待值班长确认",
            ),
            ReconResult(
                emp_id="RM_WORKER",
                date=date,
                status="工作点有劳勤无",
                hr_status="待值班长确认",
            ),
        ]
    )
    db_session.commit()

    response = client.get(
        "/api/v1/chief/review",
        params={"date": date},
        headers={"x-test-user": "L0001", "x-test-role": "chief"},
    )

    assert response.status_code == 200
    delegated_emp_ids = {item["empId"] for item in response.json()["delegated"]}
    assert "T0001" in delegated_emp_ids
    assert "RM_WORKER" not in delegated_emp_ids


def test_hr_delegate_resolves_and_binds_chief_by_furnace_id(client, db_session):
    """同号工作点转交必须查找、绑定员工所在地区的工作点，不能命中另一地区。"""
    _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    remote_same_number = Furnace(
        region="外地地区",
        furnace_no="35号工作点",
        furnace_type_code="FT01",
        name="外地35号工作点",
    )
    db_session.add(remote_same_number)
    db_session.flush()
    db_session.add(
        FurnaceChief(
            furnace_id=remote_same_number.id,
            emp_id="RM_CHIEF",
            deleted=True,
            created_by="old-admin",
            updated_by="old-admin",
        )
    )
    db_session.add_all(
        [
            Employee(
                emp_id="DEPARTED_CHIEF",
                name="已离职值班长",
                role="chief",
                employment_status="departed",
            ),
            EmployeeFavorite(
                emp_id="DEPARTED_CHIEF", kind="region", value="外地地区"
            ),
            FurnaceChief(
                furnace_id=remote_same_number.id,
                emp_id="DEPARTED_CHIEF",
                created_by="old-admin",
                updated_by="old-admin",
            ),
        ]
    )

    date = "2026-06-05"
    db_session.add_all(
        [
            AttendanceRecord(
                emp_id="RM_WORKER",
                furnace_id=remote_same_number.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                scan_time=datetime(2026, 6, 5, 8, 0, 0),
                natual_date=date,
            ),
            ReconResult(emp_id="RM_WORKER", date=date, status="工作点有劳勤无"),
        ]
    )
    db_session.commit()
    admin = {"x-test-user": "admin", "x-test-role": "admin"}
    payload = {
        "empId": "RM_WORKER",
        "date": date,
        "kind": "hr",
        "status": "待值班长确认",
    }

    unassigned = client.post("/api/v1/anomalies/confirm", json=payload, headers=admin)
    assert unassigned.status_code == 200
    assert unassigned.json()["ok"] is False
    assert unassigned.json()["needChief"] is True
    assert unassigned.json()["currentStatus"] == "未确认"
    db_session.expire_all()
    unassigned_row = db_session.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == "RM_WORKER", ReconResult.date == date
        )
    )
    assert unassigned_row.hr_status == "未确认"
    assert unassigned_row.delegate_furnace_id is None
    from app.services.anomaly_service import _chief_name_map

    assert remote_same_number.id not in _chief_name_map(db_session)

    db_session.add_all(
        [
            Employee(emp_id="LOCAL_CHIEF", name="本地值班长", role="chief"),
            EmployeeFavorite(
                emp_id="LOCAL_CHIEF", kind="region", value="示例厂区A"
            ),
        ]
    )
    db_session.commit()
    wrong_region = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "LOCAL_CHIEF"},
        headers=admin,
    )
    assert wrong_region.status_code == 400
    assert "不属于工作点地区" in wrong_region.json()["detail"]

    db_session.add_all(
        [
            Employee(emp_id="NEW_CHIEF", name="未配置地区值班长", role="chief"),
            Employee(emp_id="L0999", name="推断角色值班长", dd_user_id="123456789"),
            EmployeeFavorite(emp_id="L0999", kind="region", value="外地地区"),
        ]
    )
    db_session.commit()
    missing_region = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "NEW_CHIEF"},
        headers=admin,
    )
    assert missing_region.status_code == 400
    assert "尚未配置所属地区" in missing_region.json()["detail"]

    inferred_role = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "L0999"},
        headers=admin,
    )
    assert inferred_role.status_code == 400
    assert "系统角色不是值班长" in inferred_role.json()["detail"]
    assert not db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == remote_same_number.id,
            FurnaceChief.emp_id == "L0999",
            FurnaceChief.deleted.is_(False),
        )
    )
    inferred_employee = db_session.scalar(
        select(Employee).where(Employee.emp_id == "L0999")
    )
    inferred_employee.role = "chief"
    db_session.commit()
    explicit_role = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "L0999"},
        headers=admin,
    )
    assert explicit_role.status_code == 200
    assert db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == remote_same_number.id,
            FurnaceChief.emp_id == "L0999",
            FurnaceChief.deleted.is_(False),
        )
    )
    # 下面独立验证软删除 RM_CHIEF 绑定可恢复；先移除刚创建的候选绑定，
    # 让目标工作点重新处于“无在职值班长”，不依赖多值班长的 id 排序选人。
    l0999_binding = db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == remote_same_number.id,
            FurnaceChief.emp_id == "L0999",
        )
    )
    l0999_binding.deleted = True
    db_session.commit()

    assigned = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "RM_CHIEF"},
        headers=admin,
    )
    assert assigned.status_code == 200
    assert assigned.json()["needChief"] is False
    restored_binding = db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == remote_same_number.id,
            FurnaceChief.emp_id == "RM_CHIEF",
            FurnaceChief.deleted.is_(False),
        )
    )
    assert restored_binding
    assert restored_binding.updated_by == "admin"
    binding_audit = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "furnace_chief_bind")
        .order_by(AuditLog.id.desc())
    )
    assert binding_audit
    assert '"operation": "restore"' in binding_audit.payload
    assert not db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == local_furnace.id,
            FurnaceChief.emp_id == "RM_CHIEF",
            FurnaceChief.deleted.is_(False),
        )
    )

    automatic = client.post("/api/v1/anomalies/confirm", json=payload, headers=admin)
    assert automatic.status_code == 200
    assert automatic.json()["needChief"] is False
    latest_notification = db_session.scalar(
        select(NotificationRecord).order_by(NotificationRecord.id.desc())
    )
    assert latest_notification.target == "RM_CHIEF"


def test_delegated_queue_keeps_original_furnace_after_attendance_changes(
    client, db_session
):
    """委托后新增更晚的跨工作点考勤，不得把已通知待办漂移给另一值班长。"""
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    date = "2026-06-08"
    db_session.add_all(
        [
            Employee(emp_id="FIXED_CHIEF", name="固定值班长", role="chief"),
            Employee(emp_id="DRIFT_WORKER", name="漂移测试工人", role="worker"),
            EmployeeFavorite(
                emp_id="FIXED_CHIEF", kind="region", value="示例厂区A"
            ),
            FurnaceChief(furnace_id=local_furnace.id, emp_id="FIXED_CHIEF"),
            AttendanceRecord(
                emp_id="DRIFT_WORKER",
                furnace_id=local_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                scan_time=datetime(2026, 6, 8, 8, 0, 0),
                natual_date=date,
            ),
            ReconResult(
                emp_id="DRIFT_WORKER", date=date, status="工作点有劳勤无"
            ),
        ]
    )
    db_session.commit()

    delegated = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "DRIFT_WORKER",
            "date": date,
            "kind": "hr",
            "status": "待值班长确认",
            "chiefEmpId": "FIXED_CHIEF",
        },
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert delegated.status_code == 200
    db_session.expire_all()
    row = db_session.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == "DRIFT_WORKER", ReconResult.date == date
        )
    )
    assert row.delegate_furnace_id == local_furnace.id

    db_session.add(
        AttendanceRecord(
            emp_id="DRIFT_WORKER",
            furnace_id=remote_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="补录",
            status="active",
            scan_time=datetime(2026, 6, 8, 9, 0, 0),
            natual_date=date,
        )
    )
    db_session.commit()

    stale_target = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "DRIFT_WORKER",
            "date": date,
            "kind": "hr",
            "status": "待值班长确认",
            "chiefEmpId": "FIXED_CHIEF",
            "furnaceId": remote_furnace.id,
        },
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert stale_target.status_code == 409
    assert "委托目标工作点已变化" in stale_target.json()["detail"]

    local_workbench = client.get(
        "/api/v1/anomalies",
        params={"date": date},
        headers={"x-test-user": "FIXED_CHIEF", "x-test-role": "chief"},
    )
    remote_workbench = client.get(
        "/api/v1/anomalies",
        params={"date": date},
        headers={"x-test-user": "RM_CHIEF", "x-test-role": "chief"},
    )
    assert local_workbench.status_code == 200
    assert remote_workbench.status_code == 200
    local_row = next(
        item
        for item in local_workbench.json()["items"]
        if item["empId"] == "DRIFT_WORKER" and item["rowKind"] == "recon"
    )
    remote_row = next(
        item
        for item in remote_workbench.json()["items"]
        if item["empId"] == "DRIFT_WORKER" and item["rowKind"] == "recon"
    )
    assert local_row["chiefCanConfirm"] is True
    assert remote_row["chiefCanConfirm"] is False

    local_queue = client.get(
        "/api/v1/chief/review",
        params={"date": date},
        headers={"x-test-user": "FIXED_CHIEF", "x-test-role": "chief"},
    )
    remote_queue = client.get(
        "/api/v1/chief/review",
        params={"date": date},
        headers={"x-test-user": "RM_CHIEF", "x-test-role": "chief"},
    )
    assert local_queue.status_code == 200
    assert remote_queue.status_code == 200
    assert {item["empId"] for item in local_queue.json()["delegated"]} == {
        "DRIFT_WORKER"
    }
    assert "DRIFT_WORKER" not in {
        item["empId"] for item in remote_queue.json()["delegated"]
    }

    from app.services.anomaly_service import get_m2_review_queue

    assert get_m2_review_queue(db_session, furnace_id=local_furnace.id, date=date)
    assert not get_m2_review_queue(
        db_session, furnace_id=remote_furnace.id, date=date
    )

    denied = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "DRIFT_WORKER",
            "date": date,
            "kind": "chief",
            "status": "通过",
        },
        headers={"x-test-user": "RM_CHIEF", "x-test-role": "chief"},
    )
    assert denied.status_code == 403
    db_session.expire_all()
    assert row.chief_status == "未确认"

    confirmed = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "DRIFT_WORKER",
            "date": date,
            "kind": "chief",
            "status": "通过",
        },
        headers={"x-test-user": "FIXED_CHIEF", "x-test-role": "chief"},
    )
    assert confirmed.status_code == 200
    db_session.expire_all()
    assert row.chief_status == "通过"
    assert row.delegate_furnace_id == local_furnace.id


def test_stale_cross_region_delegate_uses_current_active_furnace(
    client, db_session
):
    """原工作点有效证据取消后，旧委托不能继续授予跨地区值班长写权限。"""
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    date = "2026-06-11"
    local_attendance = AttendanceRecord(
        emp_id="STALE_WORKER",
        furnace_id=local_furnace.id,
        squad="3班",
        post="出料工",
        employ_type="正式",
        source="扫码",
        status="active",
        scan_time=datetime(2026, 6, 11, 8, 0, 0),
        natual_date=date,
    )
    row = ReconResult(
        emp_id="STALE_WORKER", date=date, status="工作点有劳勤无"
    )
    db_session.add_all(
        [
            Employee(emp_id="STALE_WORKER", name="跨区改记工人", role="worker"),
            local_attendance,
            row,
        ]
    )
    db_session.commit()

    delegated = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "STALE_WORKER",
            "date": date,
            "kind": "hr",
            "status": "待值班长确认",
        },
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert delegated.status_code == 200
    db_session.expire_all()
    assert row.delegate_furnace_id == local_furnace.id

    local_attendance.status = "已取消"
    db_session.add(
        AttendanceRecord(
            emp_id="STALE_WORKER",
            furnace_id=remote_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="扫码",
            status="active",
            scan_time=datetime(2026, 6, 11, 9, 0, 0),
            natual_date=date,
        )
    )
    db_session.commit()

    from app.services.anomaly_service import resolve_review_furnace_id

    assert (
        resolve_review_furnace_id(db_session, "STALE_WORKER", date, row)
        == remote_furnace.id
    )
    local_queue = client.get(
        "/api/v1/chief/review",
        params={"date": date},
        headers={"x-test-user": "L0001", "x-test-role": "chief"},
    )
    remote_queue = client.get(
        "/api/v1/chief/review",
        params={"date": date},
        headers={"x-test-user": "RM_CHIEF", "x-test-role": "chief"},
    )
    assert "STALE_WORKER" not in {
        item["empId"] for item in local_queue.json()["delegated"]
    }
    assert "STALE_WORKER" in {
        item["empId"] for item in remote_queue.json()["delegated"]
    }

    denied = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "STALE_WORKER",
            "date": date,
            "kind": "chief",
            "status": "通过",
        },
        headers={"x-test-user": "L0001", "x-test-role": "chief"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "STALE_WORKER",
            "date": date,
            "kind": "chief",
            "status": "通过",
        },
        headers={"x-test-user": "RM_CHIEF", "x-test-role": "chief"},
    )
    assert allowed.status_code == 200
    db_session.expire_all()
    assert row.chief_status == "通过"
    assert row.delegate_furnace_id == remote_furnace.id


def test_legacy_bound_chief_is_candidate_and_stale_override_is_ignored(
    client, db_session
):
    """role=NULL 的存量值班长凭有效绑定可候选；已有值班长时忽略陈旧的另选值。"""
    remote_furnace = _seed_remote_region(db_session)
    target_furnace = Furnace(
        region="外地地区",
        furnace_no="候选测试工作点",
        furnace_type_code="FT01",
        name="候选测试工作点",
    )
    db_session.add(target_furnace)
    db_session.flush()
    db_session.add_all(
        [
            Employee(
                emp_id="L0888",
                name="存量空角色值班长",
                role=None,
                employment_status="active",
            ),
            # 收藏故意在另一地区；已有外地工作点绑定仍应让外地 HR 看见候选。
            EmployeeFavorite(
                emp_id="L0888", kind="region", value="示例厂区A"
            ),
            FurnaceChief(furnace_id=remote_furnace.id, emp_id="L0888"),
            Employee(emp_id="REMOTE_HR", name="外地人事", role="hr"),
            EmployeeFavorite(
                emp_id="REMOTE_HR", kind="region", value="外地地区"
            ),
            Employee(emp_id="LEGACY_WORKER", name="候选测试工人", role="worker"),
            AttendanceRecord(
                emp_id="LEGACY_WORKER",
                furnace_id=target_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                natual_date="2026-06-12",
            ),
            ReconResult(
                emp_id="LEGACY_WORKER",
                date="2026-06-12",
                status="工作点有劳勤无",
            ),
        ]
    )
    db_session.commit()
    hr_headers = {"x-test-user": "REMOTE_HR", "x-test-role": "hr"}

    candidates = client.get(
        "/api/v1/admin/employees",
        params={"employment_status": "active"},
        headers=hr_headers,
    )
    assert candidates.status_code == 200
    assert candidates.json()["candidateContractVersion"] == 2
    legacy = next(
        item for item in candidates.json()["items"] if item["empId"] == "L0888"
    )
    assert legacy["explicitRole"] is None
    assert legacy["chiefCandidateEligible"] is True
    assert "外地地区" in legacy["chiefRegions"]

    payload = {
        "empId": "LEGACY_WORKER",
        "date": "2026-06-12",
        "kind": "hr",
        "status": "待值班长确认",
        "chiefEmpId": "L0888",
    }
    assigned = client.post(
        "/api/v1/anomalies/confirm", json=payload, headers=hr_headers
    )
    assert assigned.status_code == 200
    assert db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == target_furnace.id,
            FurnaceChief.emp_id == "L0888",
            FurnaceChief.deleted.is_(False),
        )
    )

    # 模拟前端残留“缺值班长”状态又提交另一人：后端应沿用现有 L0888，
    # 不新增 RM_CHIEF 对目标工作点的管辖权。
    stale_override = client.post(
        "/api/v1/anomalies/confirm",
        json={**payload, "chiefEmpId": "RM_CHIEF"},
        headers=hr_headers,
    )
    assert stale_override.status_code == 200
    assert not db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == target_furnace.id,
            FurnaceChief.emp_id == "RM_CHIEF",
            FurnaceChief.deleted.is_(False),
        )
    )
    latest_notification = db_session.scalar(
        select(NotificationRecord).order_by(NotificationRecord.id.desc())
    )
    assert latest_notification.target == "L0888"


def test_binding_failure_rolls_back_delegation_state(
    monkeypatch, client, db_session
):
    """HTTP 写路径绑定失败时，确认状态和委托工作点必须仍保持未写入。"""
    from app.services import anomaly_service

    db_session.add_all(
        [
            Region(code="RG_ATOMIC", name="原子测试地区", sort=220),
            Employee(emp_id="ATOMIC_CHIEF", name="原子测试值班长", role="chief"),
            Employee(emp_id="ATOMIC_WORKER", name="原子测试工人", role="worker"),
        ]
    )
    db_session.flush()
    furnace = Furnace(
        region="原子测试地区",
        furnace_no="原子1号工作点",
        furnace_type_code="FT01",
        name="原子1号工作点",
    )
    db_session.add(furnace)
    db_session.flush()
    db_session.add_all(
        [
            EmployeeFavorite(
                emp_id="ATOMIC_CHIEF", kind="region", value="原子测试地区"
            ),
            AttendanceRecord(
                emp_id="ATOMIC_WORKER",
                furnace_id=furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                natual_date="2026-06-09",
            ),
            ReconResult(
                emp_id="ATOMIC_WORKER",
                date="2026-06-09",
                status="工作点有劳勤无",
            ),
        ]
    )
    db_session.commit()

    def fail_binding(*_args, **_kwargs):
        raise RuntimeError("simulated binding failure")

    monkeypatch.setattr(anomaly_service, "_bind_chief", fail_binding)
    with pytest.raises(RuntimeError, match="simulated binding failure"):
        client.post(
            "/api/v1/anomalies/confirm",
            json={
                "empId": "ATOMIC_WORKER",
                "date": "2026-06-09",
                "kind": "hr",
                "status": "待值班长确认",
                "chiefEmpId": "ATOMIC_CHIEF",
                "furnaceId": furnace.id,
            },
            headers={"x-test-user": "admin", "x-test-role": "admin"},
        )
    db_session.expire_all()

    row = db_session.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == "ATOMIC_WORKER",
            ReconResult.date == "2026-06-09",
        )
    )
    assert row.hr_status == "未确认"
    assert row.delegate_furnace_id is None
    assert not db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == furnace.id,
            FurnaceChief.emp_id == "ATOMIC_CHIEF",
        )
    )


def test_workbench_and_delegation_ignore_overtime_furnace(client, db_session):
    """基础出勤工作台与委托工作点口径一致，不能被另一工作点的加班记录污染。"""
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    date = "2026-06-10"
    db_session.add_all(
        [
            Employee(emp_id="ALIGN_WORKER", name="工作点口径工人", role="worker"),
            AttendanceRecord(
                emp_id="ALIGN_WORKER",
                furnace_id=local_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="补录",
                status="待审",
                natual_date=date,
            ),
            AttendanceRecord(
                emp_id="ALIGN_WORKER",
                furnace_id=remote_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="扫码",
                status="active",
                record_kind="加班",
                scan_time=datetime(2026, 6, 10, 20, 0, 0),
                natual_date=date,
            ),
            ReconResult(
                emp_id="ALIGN_WORKER",
                date=date,
                status="工作点有劳勤无",
                hr_by="admin",
            ),
        ]
    )
    db_session.commit()

    from app.services.anomaly_service import resolve_review_furnace_id

    assert resolve_review_furnace_id(db_session, "ALIGN_WORKER", date) == local_furnace.id
    response = client.get(
        "/api/v1/anomalies",
        params={"date": date},
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert response.status_code == 200
    base_row = next(
        item
        for item in response.json()["items"]
        if item["empId"] == "ALIGN_WORKER" and item["rowKind"] == "recon"
    )
    assert base_row["furnaceId"] == local_furnace.id
    assert base_row["delegateFurnaceId"] == local_furnace.id
    assert base_row["delegateRegion"] == "示例厂区A"
    assert base_row["delegateChiefEmpId"] == "L0001"
    overtime_row = next(
        item
        for item in response.json()["items"]
        if item["empId"] == "ALIGN_WORKER" and item["rowKind"] == "overtime"
    )
    assert overtime_row["furnaceId"] == remote_furnace.id
    assert overtime_row["chiefEmpId"] == "RM_CHIEF"
    assert overtime_row["delegateFurnaceId"] == local_furnace.id
    assert overtime_row["delegateChiefEmpId"] == "L0001"

    local_chief_view = client.get(
        "/api/v1/anomalies",
        params={"date": date},
        headers={"x-test-user": "L0001", "x-test-role": "chief"},
    )
    assert local_chief_view.status_code == 200
    local_items = [
        item
        for item in local_chief_view.json()["items"]
        if item["empId"] == "ALIGN_WORKER"
    ]
    assert {item["rowKind"] for item in local_items} == {"recon"}
    assert "外地地区" not in local_chief_view.json()["options"]["regions"]
    assert "外地1号工作点" not in local_chief_view.json()["options"]["furnaces"]


def test_scoped_hr_cannot_delegate_latest_foreign_furnace(client, db_session):
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(
            Furnace.region == "示例厂区A", Furnace.furnace_no == "35号工作点"
        )
    )
    db_session.add_all(
        [
            Employee(emp_id="DELEGATE_HR", name="本地人事", role="hr"),
            EmployeeFavorite(
                emp_id="DELEGATE_HR", kind="region", value="示例厂区A"
            ),
            AttendanceRecord(
                emp_id="T0005",
                furnace_id=local_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="补录",
                status="active",
                scan_time=datetime(2026, 6, 6, 8, 0, 0),
                natual_date="2026-06-06",
            ),
            AttendanceRecord(
                emp_id="T0005",
                furnace_id=remote_furnace.id,
                squad="3班",
                post="出料工",
                employ_type="正式",
                source="补录",
                status="active",
                scan_time=datetime(2026, 6, 6, 9, 0, 0),
                natual_date="2026-06-06",
            ),
            ReconResult(
                emp_id="T0005", date="2026-06-06", status="工作点有劳勤无"
            ),
        ]
    )
    db_session.commit()

    response = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "T0005",
            "date": "2026-06-06",
            "kind": "hr",
            "status": "待值班长确认",
            "chiefEmpId": "L0001",
        },
        headers={"x-test-user": "DELEGATE_HR", "x-test-role": "hr"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "员工当日工作点不在您的管辖地区，请联系该工作点所属地区的 HR 发起委托"
    )
    assert "外地地区" not in response.json()["detail"]
    assert "最近有效扫码" not in response.json()["detail"]
    row = db_session.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == "T0005", ReconResult.date == "2026-06-06"
        )
    )
    assert row.hr_status == "未确认"
    assert not db_session.scalar(select(NotificationRecord.id))
    assert not db_session.scalar(
        select(FurnaceChief).where(
            FurnaceChief.furnace_id == remote_furnace.id,
            FurnaceChief.emp_id == "L0001",
        )
    )


def test_hr_delegate_rejects_recon_without_furnace_evidence(client, db_session):
    db_session.add(
        ReconResult(emp_id="T0003", date="2026-06-07", status="工作点有劳勤无")
    )
    db_session.commit()

    denied = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "T0003",
            "date": "2026-06-07",
            "kind": "hr",
            "status": "待值班长确认",
        },
        headers={"x-test-user": "L0001", "x-test-role": "chief"},
    )
    assert denied.status_code == 403
    assert "仅统计/人事/管理员" in denied.json()["detail"]

    response = client.post(
        "/api/v1/anomalies/confirm",
        json={
            "empId": "T0003",
            "date": "2026-06-07",
            "kind": "hr",
            "status": "待值班长确认",
            "chiefEmpId": "L0001",
        },
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )

    assert response.status_code == 400
    assert "无法定位该员工当日工作点" in response.json()["detail"]
    row = db_session.scalar(
        select(ReconResult).where(
            ReconResult.emp_id == "T0003", ReconResult.date == "2026-06-07"
        )
    )
    assert row.hr_status == "未确认"
    assert not db_session.scalar(select(NotificationRecord.id))


def test_makeup_create_requires_region_scope_for_hr(client, db_session):
    remote_furnace = _seed_remote_region(db_session)
    db_session.add_all([
        Employee(emp_id="RG_HR", name="本地人事", role="hr"),
        EmployeeFavorite(emp_id="RG_HR", kind="region", value="示例厂区A"),
    ])
    db_session.commit()
    headers = {"x-test-user": "RG_HR", "x-test-role": "hr"}

    denied = client.post(
        "/api/v1/makeup",
        json={
            "userid": "RM_WORKER",
            "date": "2026-06-01",
            "furnace": "外地1号工作点",
            "region": "外地地区",
            "post": "出料工",
        },
        headers=headers,
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/api/v1/makeup",
        json={
            "userid": "T0001",
            "date": "2026-06-01",
            "furnace": "35号工作点",
            "region": "示例厂区A",
            "post": "出料工",
        },
        headers=headers,
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "待审"


def test_recon_run_scoped_to_hr_region(client, db_session):
    """地区 HR/统计触发对账重算只覆盖本地区员工，不触发全库重算。"""
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    db_session.add_all([
        Employee(emp_id="RG_STAT", name="本地统计", role="stat"),
        EmployeeFavorite(emp_id="RG_STAT", kind="region", value="示例厂区A"),
        Employee(emp_id="RG_LOCAL_W", name="本地工人", role="worker"),
        EmployeeFavorite(emp_id="RG_LOCAL_W", kind="region", value="示例厂区A"),
        EmployeeFavorite(emp_id="RM_WORKER", kind="region", value="外地地区"),
        AttendanceRecord(
            emp_id="RG_LOCAL_W",
            furnace_id=local_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="扫码",
            status="active",
            natual_date="2026-06-02",
            work_hours=8.0,
            recon_dingtalk=True,
        ),
        AttendanceRecord(
            emp_id="RM_WORKER",
            furnace_id=remote_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="扫码",
            status="active",
            natual_date="2026-06-02",
            work_hours=8.0,
            recon_dingtalk=True,
        ),
        ExternalAttendanceResultRaw(
            user_code="RG_LOCAL_W", user_name="RG_LOCAL_W", kq_date="2026-06-02",
            status_name="正常", cqgs="8.00000",
        ),
        ExternalAttendanceResultRaw(
            user_code="RM_WORKER", user_name="RM_WORKER", kq_date="2026-06-02",
            status_name="正常", cqgs="8.00000",
        ),
    ])
    db_session.commit()
    headers = {"x-test-user": "RG_STAT", "x-test-role": "stat"}

    resp = client.post("/api/v1/recon/run", params={"date": "2026-06-02"}, headers=headers)
    assert resp.status_code == 200

    recon_emp_ids = {
        row.emp_id
        for row in db_session.query(ReconResult)
        .filter(ReconResult.date == "2026-06-02")
        .all()
    }
    assert "RG_LOCAL_W" in recon_emp_ids
    assert "RM_WORKER" not in recon_emp_ids


def test_dict_delete_region_blocked_while_hr_bound(client, db_session):
    """停用地区前必须先改派仍归属该地区的 HR/统计账号。"""
    db_session.add_all([
        Region(code="RG_DEL", name="待停用地区", sort=211),
        Employee(emp_id="RG_DEL_HR", name="待停用地区人事", role="hr"),
        EmployeeFavorite(emp_id="RG_DEL_HR", kind="region", value="待停用地区"),
    ])
    db_session.commit()
    admin = {"x-test-user": "admin", "x-test-role": "admin"}

    blocked = client.delete("/api/v1/admin/dict/region/RG_DEL", headers=admin)
    assert blocked.status_code == 409
    assert "HR/统计账号" in blocked.json()["detail"]

    db_session.query(EmployeeFavorite).filter_by(
        emp_id="RG_DEL_HR", kind="region"
    ).delete()
    db_session.commit()

    deleted = client.delete("/api/v1/admin/dict/region/RG_DEL", headers=admin)
    assert deleted.status_code == 200
    db_session.expire_all()
    assert db_session.get(Region, "RG_DEL").deleted is True


def test_anomalies_scoped_row_trims_cross_region_fields(client, db_session):
    """地区隔离到字段级：跨地区行保留后，工作点/值班长/工时只展示本地区部分。"""
    remote_furnace = _seed_remote_region(db_session)
    local_furnace = db_session.scalar(
        select(Furnace).where(Furnace.furnace_no == "35号工作点")
    )
    db_session.add_all([
        Employee(emp_id="RG_A_HR", name="本地区人事", role="hr"),
        EmployeeFavorite(emp_id="RG_A_HR", kind="region", value="示例厂区A"),
        # 同一员工当天在两个地区各扫一条 8h（最近一次扫码在他地区）
        AttendanceRecord(
            emp_id="T0005",
            furnace_id=local_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="扫码",
            status="active",
            scan_time=datetime(2026, 6, 3, 8, 0, 0),
            natual_date="2026-06-03",
            work_hours=8.0,
            recon_dingtalk=True,
        ),
        AttendanceRecord(
            emp_id="T0005",
            furnace_id=remote_furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            source="扫码",
            status="active",
            scan_time=datetime(2026, 6, 3, 16, 0, 0),
            natual_date="2026-06-03",
            work_hours=8.0,
            recon_dingtalk=True,
        ),
    ])
    db_session.commit()
    headers = {"x-test-user": "RG_A_HR", "x-test-role": "hr"}

    resp = client.get(
        "/api/v1/anomalies",
        params={"date": "2026-06-03"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    rows = [item for item in body["items"] if item["empId"] == "T0005"]
    assert len(rows) == 1
    row = rows[0]
    # 行保留（含本地区工作点），但字段裁剪为本地区：工时只算本地区 8h，
    # 工作点/值班长取本地区记录，最近一次他地区扫码不得顶替展示。
    assert row["furnaces"] == [{"region": "示例厂区A", "furnace": "35号工作点"}]
    assert row["furnace"] == "35号工作点"
    assert row["workHours"] == 8
    assert row["furnaceId"] == local_furnace.id
    assert row["chiefName"] == "L0001"
    assert {c["empId"] for c in row["furnaceChiefs"]} == {"L0001"}
    # 筛选项同样不能泄露他地区工作点/值班长
    assert "外地1号工作点" not in body["options"]["furnaces"]
    assert "外地值班长" not in body["options"]["chiefs"]
