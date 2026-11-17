from datetime import datetime, timezone

from app.models import (
    AuditLog,
    DingtalkEmployeeRaw,
    Employee,
    EmployeeFavorite,
    Furnace,
    FurnaceChief,
    Region,
    RosterRootDept,
    RosterTitleMap,
)
from app.services import dingtalk_employee_service as directory_service


def _login(client, userid="admin", role="admin"):
    client.post("/api/v1/auth/login", json={"userid": userid, "role": role})


def _seed_mapping(db_session):
    db_session.add_all([
        Region(code="REG_DIFF", name="差异地区", sort=99),
        RosterRootDept(
            label="差异根部门", dept_id=99001, region_code="REG_DIFF", sort=99
        ),
        RosterTitleMap(title="远程值班长", role="chief", post="值班长", sort=99),
    ])


def test_difference_preview_and_field_only_apply(client, db_session):
    _seed_mapping(db_session)
    db_session.add_all([
        Employee(
            emp_id="D100",
            name="差异员工",
            dd_user_id="u-d100",
            role="worker",
            push_enabled=True,
        ),
        EmployeeFavorite(emp_id="D100", kind="post", value="旧职位"),
        DingtalkEmployeeRaw(
            userid="u-d100",
            job_number="D100",
            name="远程姓名不应覆盖",
            title="远程值班长",
            active=True,
            root_label="差异根部门",
            dept_path="差异根部门/35号工作点",
        ),
    ])
    db_session.commit()
    _login(client)

    preview = client.get("/api/v1/admin/dingtalk-employee/differences")
    assert preview.status_code == 200
    item = next(x for x in preview.json()["items"] if x["empId"] == "D100")
    assert item["fields"]["region"]["current"] == []
    assert item["fields"]["region"]["remote"] == "差异地区"
    assert item["fields"]["role"]["remote"] == "chief"
    assert item["fields"]["post"]["remote"] == "值班长"

    applied = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D100"], "field": "region"},
    )
    assert applied.status_code == 200
    assert applied.json()["changed"] == 1
    db_session.expire_all()
    employee = db_session.query(Employee).filter_by(emp_id="D100").one()
    assert employee.name == "差异员工"
    assert employee.role == "worker"
    assert employee.push_enabled is True
    assert {
        (f.kind, f.value)
        for f in db_session.query(EmployeeFavorite).filter_by(emp_id="D100").all()
    } == {("region", "差异地区"), ("post", "旧职位")}

    repeated = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D100"], "field": "region"},
    )
    assert repeated.json()["unchanged"] == 1
    assert repeated.json()["changed"] == 0


def test_role_protection_chief_binding_and_unbinding(client, db_session):
    _seed_mapping(db_session)
    furnace = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    furnace.region = "差异地区"
    db_session.add_all([
        Employee(emp_id="D200", name="普通员工", dd_user_id="u-d200", role="worker"),
        Employee(emp_id="D201", name="管理员工", dd_user_id="u-d201", role="hr"),
        DingtalkEmployeeRaw(
            userid="u-d200", job_number="D200", name="普通员工", title="远程值班长",
            active=True, root_label="差异根部门", dept_path="差异根部门/35号工作点",
        ),
        DingtalkEmployeeRaw(
            userid="u-d201", job_number="D201", name="管理员工", title="远程值班长",
            active=True, root_label="差异根部门", dept_path="差异根部门/35号工作点",
        ),
    ])
    db_session.commit()
    _login(client)

    batch = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={
            "empIds": ["D200", "D201"],
            "field": "role",
            "updateMode": "replace",
        },
    ).json()
    assert batch["changed"] == 1
    assert batch["skipped"] == 1
    db_session.expire_all()
    assert db_session.query(Employee).filter_by(emp_id="D200").one().role == "chief"
    assert db_session.query(Employee).filter_by(emp_id="D201").one().role == "hr"
    assert db_session.query(FurnaceChief).filter_by(
        furnace_id=furnace.id, emp_id="D200", deleted=False
    ).one()

    title_map = db_session.query(RosterTitleMap).filter_by(title="远程值班长").one()
    title_map.role = "worker"
    db_session.commit()
    leave = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={
            "empIds": ["D200"],
            "field": "role",
            "confirmProtected": True,
            "updateMode": "replace",
        },
    ).json()
    assert leave["changed"] == 1
    db_session.expire_all()
    assert db_session.query(FurnaceChief).filter_by(
        furnace_id=furnace.id, emp_id="D200"
    ).count() == 0
    assert db_session.query(AuditLog).filter_by(
        action="employee_directory_field_sync"
    ).count() == 2

    _login(client, "HR-X", "hr")
    assert client.get("/api/v1/admin/dingtalk-employee/differences").status_code == 403


def test_role_fill_empty_preserves_effective_role_and_rejects_append(client, db_session):
    _seed_mapping(db_session)
    title_map = db_session.query(RosterTitleMap).filter_by(title="远程值班长").one()
    title_map.role = "worker"
    db_session.add_all([
        Employee(emp_id="D_ROLE", name="已有角色", role="leader"),
        DingtalkEmployeeRaw(
            userid="D_ROLE", job_number="D_ROLE", name="已有角色", title="远程值班长",
            active=True, root_label="差异根部门",
        ),
    ])
    db_session.commit()
    _login(client)

    filled = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D_ROLE"], "field": "role", "updateMode": "fill_empty"},
    )
    appended = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D_ROLE"], "field": "role", "updateMode": "append"},
    )

    assert filled.status_code == 200
    assert filled.json()["changed"] == 0
    assert filled.json()["skippedItems"][0]["reason"] == "本地已有值，仅填空模式不修改"
    assert appended.status_code == 422
    db_session.refresh(db_session.query(Employee).filter_by(emp_id="D_ROLE").one())
    assert db_session.query(Employee).filter_by(emp_id="D_ROLE").one().role == "leader"


def test_manual_role_fill_empty_writes_when_persisted_role_is_null(client, db_session):
    _seed_mapping(db_session)
    title_map = db_session.query(RosterTitleMap).filter_by(title="远程值班长").one()
    title_map.role = "worker"
    db_session.add_all([
        Employee(emp_id="D_EMPTY_ROLE", name="空角色", role=None),
        DingtalkEmployeeRaw(
            userid="D_EMPTY_ROLE",
            job_number="D_EMPTY_ROLE",
            name="空角色",
            title="远程值班长",
            active=True,
            root_label="差异根部门",
        ),
    ])
    db_session.commit()
    _login(client)

    applied = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D_EMPTY_ROLE"], "field": "role", "updateMode": "fill_empty"},
    )
    assert applied.status_code == 200
    assert applied.json()["changed"] == 1
    db_session.expire_all()
    assert db_session.query(Employee).filter_by(emp_id="D_EMPTY_ROLE").one().role == "worker"


def test_legacy_apply_rejects_while_directory_sync_lock_is_held(client, db_session):
    _seed_mapping(db_session)
    db_session.add_all([
        Employee(emp_id="D_LOCKED", name="锁内员工", role="worker"),
        DingtalkEmployeeRaw(
            userid="D_LOCKED", job_number="D_LOCKED", name="锁内员工",
            title="远程值班长", active=True, root_label="差异根部门",
        ),
    ])
    db_session.commit()
    _login(client)
    assert directory_service.try_acquire_directory_sync() is True
    try:
        response = client.post(
            "/api/v1/admin/dingtalk-employee/apply-differences",
            json={"empIds": ["D_LOCKED"], "field": "role", "updateMode": "replace"},
        )
    finally:
        directory_service.release_directory_sync()
    assert response.status_code == 409
    assert "正在执行" in response.json()["detail"]


def test_manual_role_apply_skips_historical_invalid_mapping(client, db_session):
    _seed_mapping(db_session)
    title_map = db_session.query(RosterTitleMap).filter_by(title="远程值班长").one()
    title_map.role = "invalid-role"
    db_session.add_all([
        Employee(emp_id="D_BAD_ROLE", name="脏映射员工", role="worker"),
        DingtalkEmployeeRaw(
            userid="D_BAD_ROLE", job_number="D_BAD_ROLE", name="脏映射员工",
            title="远程值班长", active=True, root_label="差异根部门",
        ),
    ])
    db_session.commit()
    _login(client)

    result = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D_BAD_ROLE"], "field": "role", "updateMode": "replace"},
    )

    assert result.status_code == 200
    assert result.json()["changed"] == 0
    assert result.json()["skippedItems"][0]["reason"] == "远程角色映射非法"
    assert db_session.query(Employee).filter_by(emp_id="D_BAD_ROLE").one().role == "worker"


def test_multi_value_favorites_append_but_hr_region_requires_single_confirm(
    client, db_session
):
    _seed_mapping(db_session)
    confirmed_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    db_session.add_all([
        Employee(emp_id="D300", name="多值员工", role="worker"),
        EmployeeFavorite(emp_id="D300", kind="region", value="原地区"),
        EmployeeFavorite(emp_id="D300", kind="post", value="原职位"),
        Employee(
            emp_id="D301",
            name="人事员工",
            role="hr",
            region_confirmed_at=confirmed_at,
        ),
        EmployeeFavorite(emp_id="D301", kind="region", value="原地区"),
        DingtalkEmployeeRaw(
            userid="D300", job_number="D300", name="多值员工", title="远程值班长",
            active=True, root_label="差异根部门",
        ),
        DingtalkEmployeeRaw(
            userid="D301", job_number="D301", name="人事员工", title="远程值班长",
            active=True, root_label="差异根部门",
        ),
    ])
    db_session.commit()
    confirmed_saved = db_session.query(Employee).filter_by(
        emp_id="D301"
    ).one().region_confirmed_at
    _login(client)

    for field in ("region", "post"):
        result = client.post(
            "/api/v1/admin/dingtalk-employee/apply-differences",
            json={"empIds": ["D300"], "field": field},
        ).json()
        assert result["changed"] == 1
    db_session.expire_all()
    assert {
        favorite.value
        for favorite in db_session.query(EmployeeFavorite).filter_by(
            emp_id="D300", kind="region"
        )
    } == {"原地区", "差异地区"}
    assert {
        favorite.value
        for favorite in db_session.query(EmployeeFavorite).filter_by(
            emp_id="D300", kind="post"
        )
    } == {"原职位", "值班长"}

    batch = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D300", "D301"], "field": "region"},
    ).json()
    assert batch["skipped"] == 1
    assert batch["skippedItems"][0]["empId"] == "D301"

    preview = client.get("/api/v1/admin/dingtalk-employee/differences").json()
    hr_region = next(
        row for row in preview["items"] if row["empId"] == "D301"
    )["fields"]["region"]
    assert hr_region["protected"] is True
    assert hr_region["willRemove"] == ["原地区"]

    denied = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D301"], "field": "region"},
    ).json()
    assert denied["skipped"] == 1
    applied = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={"empIds": ["D301"], "field": "region", "confirmProtected": True},
    ).json()
    assert applied["changed"] == 1
    db_session.expire_all()
    hr = db_session.query(Employee).filter_by(emp_id="D301").one()
    assert hr.region_confirmed_at == confirmed_saved
    assert [
        favorite.value
        for favorite in db_session.query(EmployeeFavorite).filter_by(
            emp_id="D301", kind="region"
        )
    ] == ["差异地区"]


def test_rule_apply_rechecks_skip_multi_on_server(client, db_session):
    _seed_mapping(db_session)
    db_session.add_all([
        Employee(emp_id="D_MULTI_GUARD", name="多地区保护员工", role="worker"),
        EmployeeFavorite(emp_id="D_MULTI_GUARD", kind="region", value="原地区甲"),
        EmployeeFavorite(emp_id="D_MULTI_GUARD", kind="region", value="原地区乙"),
        DingtalkEmployeeRaw(
            userid="D_MULTI_GUARD",
            job_number="D_MULTI_GUARD",
            name="多地区保护员工",
            title="远程值班长",
            active=True,
            root_label="差异根部门",
        ),
    ])
    db_session.commit()
    _login(client)
    rule = {
        "field": "region",
        "enabled": True,
        "scope": "different",
        "updateMode": "append",
        "q": "",
        "localRegions": [],
        "currentRoles": [],
        "targetRoles": [],
        "remoteTitles": [],
        "departments": [],
        "furnaces": [],
        "differenceTypes": ["missing_remote", "multi"],
        "skipMulti": True,
    }

    response = client.post(
        "/api/v1/admin/dingtalk-employee/apply-differences",
        json={
            "empIds": ["D_MULTI_GUARD"],
            "field": "region",
            "updateMode": "append",
            "rule": rule,
        },
    )

    assert response.status_code == 200
    assert response.json()["changed"] == 0
    assert response.json()["skippedItems"] == [{
        "empId": "D_MULTI_GUARD",
        "reason": "本地存在多个地区或职位",
    }]
    assert {
        favorite.value
        for favorite in db_session.query(EmployeeFavorite).filter_by(
            emp_id="D_MULTI_GUARD", kind="region"
        )
    } == {"原地区甲", "原地区乙"}
