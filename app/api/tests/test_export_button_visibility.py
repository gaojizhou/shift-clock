from app.models import AppSetting


ADMIN = {"x-test-user": "ADMIN01", "x-test-role": "admin"}
HR = {"x-test-user": "HR01", "x-test-role": "hr"}
STAT = {"x-test-user": "STAT01", "x-test-role": "stat"}
WORKER = {"x-test-user": "T0001", "x-test-role": "worker"}


def test_export_button_visibility_defaults_to_all_export_roles(client):
    r = client.get("/api/v1/export/button-visibility", headers=HR)
    assert r.status_code == 200
    buttons = r.json()["buttons"]
    assert buttons["combined"] == ["hr", "stat"]
    assert buttons["overtimePerson"] == ["hr", "stat"]


def test_admin_can_persist_each_button_visibility(client, db_session):
    current = client.get("/api/v1/export/button-visibility", headers=ADMIN).json()["buttons"]
    current["combined"] = ["hr"]
    current["payroll"] = ["stat"]
    current["anomaly"] = []

    r = client.put(
        "/api/v1/admin/export/button-visibility",
        headers=ADMIN,
        json={"buttons": current},
    )
    assert r.status_code == 200
    assert r.json()["buttons"]["combined"] == ["hr"]
    assert r.json()["buttons"]["payroll"] == ["stat"]
    assert r.json()["buttons"]["anomaly"] == []
    assert db_session.get(AppSetting, "export_button_visibility") is not None

    assert client.get("/api/v1/export/button-visibility", headers=STAT).json() == r.json()


def test_only_admin_can_update_visibility_and_worker_cannot_read(client):
    buttons = client.get("/api/v1/export/button-visibility", headers=HR).json()["buttons"]
    assert client.put(
        "/api/v1/admin/export/button-visibility", headers=HR, json={"buttons": buttons}
    ).status_code == 403
    assert client.put(
        "/api/v1/admin/export/button-visibility", headers=STAT, json={"buttons": buttons}
    ).status_code == 403
    assert client.get("/api/v1/export/button-visibility", headers=WORKER).status_code == 403


def test_visibility_config_does_not_change_export_api_permission(client):
    buttons = client.get("/api/v1/export/button-visibility", headers=ADMIN).json()["buttons"]
    buttons["combined"] = []
    assert client.put(
        "/api/v1/admin/export/button-visibility", headers=ADMIN, json={"buttons": buttons}
    ).status_code == 200

    r = client.post(
        "/api/v1/export",
        headers=HR,
        json={"month": "2026-04", "furnaces": ["35号工作点"], "format": "meta"},
    )
    assert r.status_code == 200
