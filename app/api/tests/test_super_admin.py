"""超级管理员（TOTP 两步验证）+ 身份切换。"""

from app.config import settings
from app.totp import generate_secret, totp_at

SECRET = generate_secret()


def _code() -> str:
    return totp_at(SECRET)


def _bad_code() -> str:
    good = _code()
    return str((int(good) + 1) % 1000000).zfill(6)


def test_admin_login_disabled_when_secret_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", "")
    r = client.post("/api/v1/auth/admin-login", json={"code": "123456"})
    assert r.status_code == 403


def test_admin_login_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", SECRET)
    r = client.post("/api/v1/auth/admin-login", json={"code": _bad_code()})
    assert r.status_code == 401


def test_admin_login_grants_super(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", SECRET)
    r = client.post("/api/v1/auth/admin-login", json={"code": _code()})
    assert r.status_code == 200
    me = client.get("/api/v1/me").json()
    assert me["role"] == "admin"
    assert me["super"] is True
    assert me["impersonating"] is False


def test_switch_employees_requires_super(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", SECRET)
    # 无会话 → 401（require_user 先拦）
    assert client.get("/api/v1/admin/switch/employees").status_code == 401
    # 普通登录（非 super）→ 403
    client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert client.get("/api/v1/admin/switch/employees").status_code == 403


def test_impersonate_keeps_super_and_exit_reverts(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", SECRET)
    client.post("/api/v1/auth/admin-login", json={"code": _code()})

    # 列出员工
    emps = client.get("/api/v1/admin/switch/employees").json()["items"]
    assert any(e["empId"] == "L0001" for e in emps)

    # 模拟值班长 L0001
    r = client.post("/api/v1/admin/switch/impersonate", json={"empId": "L0001"})
    assert r.status_code == 200
    assert r.json()["role"] == "chief"
    me = client.get("/api/v1/me").json()
    assert me["userid"] == "L0001"
    assert me["role"] == "chief"
    assert me["super"] is True
    assert me["impersonating"] is True

    # super 仍在 → 可再切到工人 T0001
    r2 = client.post("/api/v1/admin/switch/impersonate", json={"empId": "T0001"})
    assert r2.status_code == 200
    assert r2.json()["role"] == "worker"
    assert client.get("/api/v1/me").json()["userid"] == "T0001"

    # 切回超管
    r3 = client.post("/api/v1/admin/switch/exit", json={})
    assert r3.status_code == 200
    me2 = client.get("/api/v1/me").json()
    assert me2["userid"] == "admin"
    assert me2["role"] == "admin"
    assert me2["impersonating"] is False


def test_dd_admin_can_switch_and_gets_promoted(client):
    """钉钉登录的 admin 角色（非密钥会话）也能用切换页；首次模拟时提升为 super 以便切回。"""
    client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"})
    # admin 角色可列出员工
    assert client.get("/api/v1/admin/switch/employees").status_code == 200
    # 模拟工人 → 会话被提升为 super
    r = client.post("/api/v1/admin/switch/impersonate", json={"empId": "T0001"})
    assert r.status_code == 200
    me = client.get("/api/v1/me").json()
    assert me["userid"] == "T0001"
    assert me["super"] is True
    # 仍能切回
    assert client.post("/api/v1/admin/switch/exit", json={}).status_code == 200
    assert client.get("/api/v1/me").json()["role"] == "admin"


def test_impersonate_unknown_employee(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_admin_totp_secret", SECRET)
    client.post("/api/v1/auth/admin-login", json={"code": _code()})
    r = client.post("/api/v1/admin/switch/impersonate", json={"empId": "ZZZZ"})
    assert r.status_code == 404
