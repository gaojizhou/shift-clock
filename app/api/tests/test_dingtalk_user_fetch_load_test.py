import asyncio
import json
import time

import httpx
import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.services import dingtalk_load_test_service as service

ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}
HR = {"x-test-user": "HR01", "x-test-role": "hr"}


def test_load_test_service_aggregates_success_error_and_rate_limit(monkeypatch):
    monkeypatch.setattr(service, "_last_run_finished_at", 0.0)
    def handler(request: httpx.Request) -> httpx.Response:
        userid = str(httpx.QueryParams(request.url.query).get("access_token"))
        assert userid == "token"
        requested = request.content.decode("utf-8")
        if "DD_LIMIT" in requested:
            return httpx.Response(200, json={"errcode": 88, "errmsg": "触发 90002 限流"})
        return httpx.Response(
            200,
            json={
                "errcode": 0,
                "result": {
                    "name": "测试用户",
                    "title": "出料工",
                    "dept_id_list": [1, 2],
                },
            },
        )

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await service._run_with_client(
                client,
                "token",
                ["DD_OK", "DD_LIMIT"],
                request_count=4,
                concurrency=2,
            )

    result = asyncio.run(run())
    assert result["requestCount"] == 4
    assert result["successCount"] == 2
    assert result["errorCount"] == 2
    assert result["rateLimitedCount"] == 2
    assert result["successRate"] == 50.0
    assert result["peakConcurrency"] <= 2
    assert result["results"][0]["name"] == "测试用户"
    assert result["results"][1]["rateLimited"] is True
    assert result["latencyMs"]["p95"] >= 0


def test_load_test_service_enforces_cooldown(monkeypatch):
    monkeypatch.setattr(service, "_last_run_finished_at", time.monotonic())

    async def run():
        async def token_provider():
            raise AssertionError("cooldown must reject before requesting a token")

        return await service.run_user_fetch_load_test(
            ["DD001"], 1, 1, token_provider=token_provider
        )

    with pytest.raises(service.DingtalkLoadTestCooldownError) as exc_info:
        asyncio.run(run())
    assert 1 <= exc_info.value.retry_after_seconds <= service.MIN_RUN_INTERVAL_SECONDS


def test_load_test_endpoint_is_admin_only_and_validates_bounds(client, monkeypatch):
    from app.services import dingtalk_service

    monkeypatch.setattr(dingtalk_service, "is_configured", lambda: True)

    payload = {"userids": ["DD001"], "requestCount": 2, "concurrency": 1}
    assert client.post("/api/v1/admin/dingtalk-user-fetch-load-test", json=payload).status_code == 401
    assert client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test", json=payload, headers=HR
    ).status_code == 403
    assert client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={**payload, "requestCount": 101},
        headers=ADMIN,
    ).status_code == 422
    assert client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={**payload, "requestCount": 100, "concurrency": 51},
        headers=ADMIN,
    ).status_code == 422
    response = client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={**payload, "requestCount": 1, "concurrency": 2},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "并发数不能大于请求总数"
    response = client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={**payload, "requestCount": 11, "concurrency": 11},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "并发超过 10 必须显式确认高压测试"


def test_load_test_endpoint_returns_service_result(client, db_session, monkeypatch):
    from app.services import dingtalk_load_test_service, dingtalk_service

    monkeypatch.setattr(dingtalk_service, "is_configured", lambda: True)

    async def fake_run(userids, request_count, concurrency):
        assert userids == ["DD001", "DD002"]
        assert request_count == 3
        assert concurrency == 2
        return {"successCount": 3, "requestCount": 3}

    monkeypatch.setattr(dingtalk_load_test_service, "run_user_fetch_load_test", fake_run)
    response = client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={
            "userids": [" DD001 ", "DD001", "DD002"],
            "requestCount": 3,
            "concurrency": 2,
        },
        headers=ADMIN,
    )
    assert response.status_code == 200
    assert response.json() == {"successCount": 3, "requestCount": 3}
    audits = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action.in_(["dingtalk-load-test-start", "dingtalk-load-test-end"]))
        .order_by(AuditLog.id)
    ).all()
    assert [row.action for row in audits] == [
        "dingtalk-load-test-start",
        "dingtalk-load-test-end",
    ]
    assert json.loads(audits[0].payload)["concurrency"] == 2
    assert json.loads(audits[1].payload)["status"] == "completed"


def test_load_test_endpoint_accepts_explicit_high_load_confirmation(client, monkeypatch):
    from app.services import dingtalk_load_test_service, dingtalk_service

    monkeypatch.setattr(dingtalk_service, "is_configured", lambda: True)

    async def fake_run(userids, request_count, concurrency):
        assert concurrency == 11
        return {"successCount": request_count, "requestCount": request_count}

    monkeypatch.setattr(dingtalk_load_test_service, "run_user_fetch_load_test", fake_run)
    response = client.post(
        "/api/v1/admin/dingtalk-user-fetch-load-test",
        json={
            "userids": ["DD001"],
            "requestCount": 11,
            "concurrency": 11,
            "confirmHighLoad": True,
        },
        headers=ADMIN,
    )
    assert response.status_code == 200
