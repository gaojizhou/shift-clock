from datetime import datetime, timezone

from app.models import AppSetting, AttendanceRecord, AuditLog, ChiefWorker, Employee, Furnace


ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}
HR = {"x-test-user": "HR_VIS", "x-test-role": "hr"}
STAT = {"x-test-user": "STAT_VIS", "x-test-role": "stat"}
CHIEF = {"x-test-user": "L0001", "x-test-role": "chief"}
DAY = "2026-04-22"


def test_departed_visibility_defaults_on_and_hr_admin_can_update(client, db_session):
    default = client.get(
        "/api/v1/admin/employees/chief-departed-visibility", headers=HR
    )
    assert default.status_code == 200
    assert default.json() == {"showDepartedToChief": True}

    disabled = client.put(
        "/api/v1/admin/employees/chief-departed-visibility",
        headers=HR,
        json={"showDepartedToChief": False},
    )
    assert disabled.status_code == 200
    assert disabled.json() == {"showDepartedToChief": False}
    assert db_session.get(AppSetting, "chief_show_departed_employees").value == "false"
    assert db_session.query(AuditLog).filter_by(
        action="chief_departed_employee_visibility"
    ).count() == 1

    enabled = client.put(
        "/api/v1/admin/employees/chief-departed-visibility",
        headers=ADMIN,
        json={"showDepartedToChief": True},
    )
    assert enabled.status_code == 200
    assert enabled.json() == {"showDepartedToChief": True}

    assert client.get(
        "/api/v1/admin/employees/chief-departed-visibility", headers=STAT
    ).status_code == 403
    assert client.put(
        "/api/v1/admin/employees/chief-departed-visibility",
        headers=CHIEF,
        json={"showDepartedToChief": False},
    ).status_code == 403


def test_chief_candidate_sources_hide_departed_when_setting_is_off(client, db_session):
    furnace = db_session.query(Furnace).filter_by(furnace_no="35号工作点").first()
    departed = Employee(
        emp_id="VIS_DEPARTED",
        name="可见性离职员工",
        role="worker",
        employment_status="departed",
    )
    active = Employee(
        emp_id="VIS_ACTIVE",
        name="可见性在职员工",
        role="worker",
        employment_status="active",
    )
    unbound_departed = Employee(
        emp_id="VIS_DEPARTED_NEW",
        name="可见性未绑定离职员工",
        role="worker",
        employment_status="departed",
    )
    db_session.add_all([
        departed,
        active,
        unbound_departed,
        ChiefWorker(chief_user_id="L0001", emp_id="VIS_DEPARTED"),
        ChiefWorker(chief_user_id="L0001", emp_id="VIS_ACTIVE"),
        AttendanceRecord(
            emp_id="VIS_DEPARTED",
            furnace_id=furnace.id,
            squad="3班",
            post="出料工",
            employ_type="正式",
            scan_time=datetime.now(timezone.utc),
            source="扫码",
            natual_date=DAY,
            work_hours=8.0,
        ),
    ])
    db_session.commit()

    default_search = client.get("/api/v1/employees/search?q=VIS_", headers=CHIEF)
    assert default_search.status_code == 200
    assert {row["empId"] for row in default_search.json()["items"]} == {
        "VIS_ACTIVE",
        "VIS_DEPARTED",
        "VIS_DEPARTED_NEW",
    }
    default_workers = client.get("/api/v1/chief-workers", headers=CHIEF).json()["items"]
    assert {row["empId"] for row in default_workers} == {"VIS_ACTIVE", "VIS_DEPARTED"}
    assert next(
        row for row in default_workers if row["empId"] == "VIS_DEPARTED"
    )["employmentStatus"] == "departed"

    switched = client.put(
        "/api/v1/admin/employees/chief-departed-visibility",
        headers=HR,
        json={"showDepartedToChief": False},
    )
    assert switched.status_code == 200

    hidden_search = client.get("/api/v1/employees/search?q=VIS_", headers=CHIEF)
    assert [row["empId"] for row in hidden_search.json()["items"]] == ["VIS_ACTIVE"]
    hidden_workers = client.get("/api/v1/chief-workers", headers=CHIEF)
    assert [row["empId"] for row in hidden_workers.json()["items"]] == ["VIS_ACTIVE"]

    direct_add = client.post(
        "/api/v1/chief-workers",
        headers=CHIEF,
        json={"empId": "VIS_DEPARTED_NEW"},
    )
    assert direct_add.status_code == 404

    chief_confirm = client.get(
        "/api/v1/confirm",
        headers=CHIEF,
        params={"furnace": "35号工作点", "region": "示例厂区A", "date": DAY},
    )
    assert chief_confirm.status_code == 200
    assert all(
        row["empId"] != "VIS_DEPARTED"
        for row in chief_confirm.json()["overtimeCandidates"]
    )
    scanned_departed = next(
        row
        for row in chief_confirm.json()["scanned"]
        if row["empId"] == "VIS_DEPARTED"
    )
    assert scanned_departed["boundToChief"] is True
    assert all(
        row["empId"] != "VIS_DEPARTED"
        for row in chief_confirm.json()["external"]
    )

    admin_confirm = client.get(
        "/api/v1/confirm",
        headers=ADMIN,
        params={"furnace": "35号工作点", "region": "示例厂区A", "date": DAY},
    )
    departed_candidate = next(
        row
        for row in admin_confirm.json()["overtimeCandidates"]
        if row["empId"] == "VIS_DEPARTED"
    )
    assert departed_candidate["employmentStatus"] == "departed"

    # 开关只限制值班长视图；HR 仍可查找、查看全部绑定人员。
    hr_search = client.get("/api/v1/employees/search?q=VIS_", headers=HR)
    assert {row["empId"] for row in hr_search.json()["items"]} == {
        "VIS_ACTIVE",
        "VIS_DEPARTED",
        "VIS_DEPARTED_NEW",
    }
    hr_workers = client.get(
        "/api/v1/chief-workers", headers=HR, params={"chief": "L0001"}
    )
    assert {row["empId"] for row in hr_workers.json()["items"]} == {
        "VIS_ACTIVE",
        "VIS_DEPARTED",
    }
