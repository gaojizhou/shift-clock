import pytest
from sqlalchemy import select

from app.models import Employee
from app.services import dingtalk_employee_service, dingtalk_service


def _login_info(userid: str, *, name: str = "测试员工", title: str = "出料工") -> dict:
    detail = {
        "userid": userid,
        "name": name,
        "job_number": "E900",
        "title": title,
        "dept_id_list": [],
    }
    return {
        "configured": True,
        "userid": userid,
        "name": name,
        "title": title,
        "raw": {"errcode": 0, "result": detail},
    }


def test_login_existing_employee_does_not_import(client, monkeypatch):
    calls = {"login": 0}

    async def fake_login(code: str):
        calls["login"] += 1
        return _login_info("T0001")

    def unexpected_import(*args, **kwargs):
        raise AssertionError("existing employee must not be imported")

    monkeypatch.setattr(dingtalk_service, "login_by_code", fake_login)
    monkeypatch.setattr(dingtalk_employee_service, "import_single_user", unexpected_import)

    response = client.post("/api/v1/dingtalk/login", json={"authCode": "once"})

    assert response.status_code == 200
    assert response.json()["matched"] is True
    assert response.json()["imported"] is False
    assert calls["login"] == 1
    assert client.cookies.get("fdc_session")


def test_login_auto_imports_with_reused_user_detail(client, db_session, monkeypatch):
    calls = {"login": 0, "import": 0}
    info = _login_info("DD900")

    async def fake_login(code: str):
        calls["login"] += 1
        return info

    def fake_import(db, userid: str, **kwargs):
        calls["import"] += 1
        assert userid == "DD900"
        assert kwargs["user_detail"] is info["raw"]["result"]
        db.add(Employee(emp_id="E900", name="测试员工", dd_user_id=userid, role="worker"))
        db.commit()
        return {"empId": "E900", "role": "worker", "chiefBound": False}

    monkeypatch.setattr(dingtalk_service, "login_by_code", fake_login)
    monkeypatch.setattr(dingtalk_employee_service, "import_single_user", fake_import)

    response = client.post("/api/v1/dingtalk/login", json={"authCode": "once"})

    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert body["imported"] is True
    assert body["resolvedUserid"] == "E900"
    assert calls == {"login": 1, "import": 1}
    assert db_session.scalar(select(Employee).where(Employee.emp_id == "E900"))
    assert client.get("/api/v1/me").json()["userid"] == "E900"


@pytest.mark.parametrize(
    ("dd_userid", "emp_id", "expected_role"),
    [
        ("HR_REMOTE_USER", "E901", "worker"),
        ("123456789", "L0777", "chief"),
    ],
)
def test_first_login_infers_role_from_imported_employee_id(
    client, monkeypatch, dd_userid, emp_id, expected_role
):
    async def fake_login(code: str):
        return _login_info(dd_userid)

    def fake_import(db, userid: str, **kwargs):
        db.add(Employee(emp_id=emp_id, name="首次登录员工", dd_user_id=userid, role=None))
        db.commit()
        return {"empId": emp_id, "role": None, "chiefBound": False}

    monkeypatch.setattr(dingtalk_service, "login_by_code", fake_login)
    monkeypatch.setattr(dingtalk_employee_service, "import_single_user", fake_import)

    response = client.post("/api/v1/dingtalk/login", json={"authCode": "once"})

    assert response.status_code == 200
    assert response.json()["resolvedUserid"] == emp_id
    assert response.json()["role"] == expected_role
    assert client.get("/api/v1/me").json()["role"] == expected_role


def test_login_returns_import_failure_without_session(client, monkeypatch):
    async def fake_login(code: str):
        return _login_info("DD901")

    def fake_import(*args, **kwargs):
        raise ValueError("职位未配置")

    monkeypatch.setattr(dingtalk_service, "login_by_code", fake_login)
    monkeypatch.setattr(dingtalk_employee_service, "import_single_user", fake_import)

    response = client.post("/api/v1/dingtalk/login", json={"authCode": "once"})

    assert response.status_code == 200
    assert response.json()["matched"] is False
    assert "职位未配置" in response.json()["message"]
    assert client.cookies.get("fdc_session") is None
