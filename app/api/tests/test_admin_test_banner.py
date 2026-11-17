import pytest

from app.config import Settings, settings


@pytest.mark.parametrize("enabled", [False, True])
def test_admin_banner_is_independent_of_test_login(client, monkeypatch, enabled):
    assert Settings.model_fields["fdc_admin_test_banner"].default is False
    monkeypatch.setattr(settings, "fdc_admin_test_banner", enabled)
    monkeypatch.setattr(settings, "fdc_test_mode", False)
    response = client.get("/api/v1/me")
    assert response.status_code == 200
    assert response.json()["adminTestBanner"] is enabled
    # 显示测试标识不会开启生产测试登录。
    assert client.post("/api/v1/auth/login", json={"userid": "admin", "role": "admin"}).status_code == 404
