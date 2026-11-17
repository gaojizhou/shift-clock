"""生产安全门禁：测试模式关闭时,演示身份切换登录必须不可用。"""

import time

from app.config import settings
from app.signing import sign, sign_login

SECRET = "test-secret-abc123"


def _signed(client, userid, role=None, redirect=None, exp=None):
    """用 SECRET 造一个签名 /h5/test/auth URL（默认 120s 有效）。"""
    exp = exp if exp is not None else int(time.time()) + 120
    sig = sign_login(SECRET, userid, role, redirect, exp)
    q = f"userid={userid}&exp={exp}&sig={sig}"
    if role is not None:
        q += f"&role={role}"
    if redirect is not None:
        q += f"&redirect={redirect}"
    return client.get(f"/h5/test/auth?{q}", follow_redirects=False)


def test_auth_login_disabled_without_test_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "fdc_test_mode", False)
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 404


def test_auth_login_enabled_in_test_mode(client):
    r = client.post("/api/v1/auth/login", json={"userid": "L0001", "role": "chief"})
    assert r.status_code == 200


def test_health_exposes_test_mode(client, monkeypatch):
    health = client.get("/api/v1/health").json()
    assert health["testMode"] is True
    assert health["status"] == "ok"
    scheduler = health["scheduler"]
    assert scheduler["started"] is True
    assert scheduler["allHealthy"] is True
    assert scheduler["error"] is None
    # 可选集成（如外部考勤）未配置时不注册对应任务，这里只断言核心任务存在。
    assert {
        "fdc_escalation_daily",
        "fdc_shift_push_minutely",
        "fdc_employee_difference_sync_daily",
    } <= set(scheduler["jobIds"])
    monkeypatch.setattr(settings, "fdc_test_mode", False)
    assert client.get("/api/v1/health").json()["testMode"] is False


def test_test_headers_ignored_without_test_mode(client, monkeypatch):
    """生产门禁：测试模式关闭时，X-Test-User/Role 头不得授予任何身份（防高危提权）。"""
    monkeypatch.setattr(settings, "fdc_test_mode", False)
    # 头被忽略 → 视为未认证 → 受保护后台接口返回 401
    r = client.get(
        "/api/v1/admin/employees",
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert r.status_code == 401


def test_test_headers_grant_identity_in_test_mode(client):
    """测试模式下演示身份头仍可用（本地/演示环境依赖）。"""
    r = client.get(
        "/api/v1/admin/employees",
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert r.status_code == 200


def test_test_auth_signed_redirect_sets_cookie_and_302(client, monkeypatch):
    """签名令牌：有效签名 + 合法站内 redirect → 302 跳转且写入 fdc_session。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    r = _signed(client, "L0001", role="chief", redirect="/h5/")
    assert r.status_code == 302
    assert r.headers["location"] == "/h5/"
    assert "fdc_session" in r.headers.get("set-cookie", "")


def test_test_auth_rejects_open_redirect(client, monkeypatch):
    """开放重定向防护：协议相对 // 与绝对 URL 被拒，退回 JSON（仍登录，因签名有效）。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    for bad in ("//evil.com", "https://evil.com"):
        r = _signed(client, "L0001", role="chief", redirect=bad)
        assert r.status_code == 200
        assert r.json()["role"] == "chief"


def test_test_auth_requires_secret_configured(client, monkeypatch):
    """未配置 SECRET → 签名登录入口关闭（即便带了 sig 也 403）。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", "")
    r = _signed(client, "L0001", role="chief", redirect="/h5/")
    assert r.status_code == 403


def test_test_auth_rejects_bad_signature(client, monkeypatch):
    """错误签名（如他人用别的密钥伪造）→ 403。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    exp = int(time.time()) + 120
    r = client.get(
        f"/h5/test/auth?userid=admin&role=admin&exp={exp}&sig=deadbeef",
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_test_auth_rejects_expired(client, monkeypatch):
    """过期 exp → 403（防重放）。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    r = _signed(client, "L0001", role="chief", redirect="/h5/", exp=int(time.time()) - 1)
    assert r.status_code == 403


def test_test_employees_signed_lists_roster(client, monkeypatch):
    """签名授权 → 返回在册员工（含 empId/name/role），供启动器动态列身份。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    exp = int(time.time()) + 120
    sig = sign(SECRET, ["employees"], exp)
    r = client.get(f"/h5/test/employees?exp={exp}&sig={sig}")
    assert r.status_code == 200
    items = r.json()["items"]
    ids = {e["empId"] for e in items}
    assert {"T0001", "L0001"} <= ids
    chief = next(e for e in items if e["empId"] == "L0001")
    assert chief["role"] == "chief"


def test_test_employees_rejects_unsigned(client, monkeypatch):
    """无 sig → 403（公网不可拉取花名册）。"""
    monkeypatch.setattr(settings, "fdc_test_login_secret", SECRET)
    assert client.get("/h5/test/employees").status_code == 403
    # 登录签名不能拿来当列表签名（parts 不同）
    exp = int(time.time()) + 120
    bad = sign_login(SECRET, "x", None, None, exp)
    assert client.get(f"/h5/test/employees?exp={exp}&sig={bad}").status_code == 403
