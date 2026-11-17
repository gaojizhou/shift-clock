"""Bounded, read-only DingTalk user-detail load test for administrators."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.services.dingtalk_service import _get_access_token

_OAPI = "https://oapi.dingtalk.com"
MAX_REQUESTS = 100
MAX_CONCURRENCY = 50
SAFE_CONCURRENCY = 10
MAX_USERIDS = 20
REQUEST_TIMEOUT_SECONDS = 10.0
MIN_RUN_INTERVAL_SECONDS = 180.0

_run_lock = asyncio.Lock()
_last_run_finished_at = 0.0


class DingtalkLoadTestBusyError(RuntimeError):
    """Only one load test may run in an API process at a time."""


class DingtalkLoadTestCooldownError(RuntimeError):
    """Protect the shared DingTalk quota from back-to-back load tests."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"压测冷却中，请 {retry_after_seconds} 秒后再试")


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return round(ordered[index], 1)


def _is_rate_limited(errcode: Any, errmsg: str) -> bool:
    return str(errcode) in {"88", "90002"} or "90002" in errmsg


async def _run_with_client(
    client: httpx.AsyncClient,
    token: str,
    userids: list[str],
    request_count: int,
    concurrency: int,
) -> dict[str, Any]:
    semaphore = asyncio.Semaphore(concurrency)
    active = 0
    peak_concurrency = 0

    async def fetch_one(index: int, userid: str) -> dict[str, Any]:
        nonlocal active, peak_concurrency
        async with semaphore:
            active += 1
            peak_concurrency = max(peak_concurrency, active)
            started = time.perf_counter()
            try:
                response = await client.post(
                    f"{_OAPI}/topapi/v2/user/get",
                    params={"access_token": token},
                    json={"userid": userid},
                )
                response.raise_for_status()
                payload = response.json()
                errcode = payload.get("errcode", 0)
                errmsg = str(payload.get("errmsg") or "")
                ok = errcode == 0
                result = payload.get("result") or {}
                return {
                    "index": index,
                    "userid": userid,
                    "ok": ok,
                    "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                    "errcode": errcode,
                    "error": None if ok else (errmsg or f"errcode={errcode}"),
                    "rateLimited": _is_rate_limited(errcode, errmsg),
                    "name": result.get("name") if ok else None,
                    "title": result.get("title") if ok else None,
                    "departmentCount": len(result.get("dept_id_list") or []) if ok else None,
                }
            except httpx.TimeoutException:
                return {
                    "index": index,
                    "userid": userid,
                    "ok": False,
                    "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                    "errcode": None,
                    "error": f"请求超过 {REQUEST_TIMEOUT_SECONDS:g} 秒",
                    "rateLimited": False,
                    "name": None,
                    "title": None,
                    "departmentCount": None,
                }
            except (httpx.HTTPError, ValueError) as exc:
                return {
                    "index": index,
                    "userid": userid,
                    "ok": False,
                    "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                    "errcode": None,
                    "error": str(exc)[:300],
                    "rateLimited": False,
                    "name": None,
                    "title": None,
                    "departmentCount": None,
                }
            finally:
                active -= 1

    started = time.perf_counter()
    tasks = [
        fetch_one(index + 1, userids[index % len(userids)])
        for index in range(request_count)
    ]
    results = await asyncio.gather(*tasks)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    latencies = [float(item["latencyMs"]) for item in results]
    successes = sum(1 for item in results if item["ok"])
    rate_limited = sum(1 for item in results if item["rateLimited"])
    average = round(sum(latencies) / len(latencies), 1) if latencies else 0.0

    return {
        "generatedAt": datetime.now(UTC).isoformat(),
        "mode": "read_only_user_get",
        "requestCount": request_count,
        "configuredConcurrency": concurrency,
        "peakConcurrency": peak_concurrency,
        "useridCount": len(userids),
        "durationMs": duration_ms,
        "throughputRps": round(request_count / max(duration_ms / 1000, 0.001), 2),
        "successCount": successes,
        "errorCount": request_count - successes,
        "successRate": round(successes * 100 / request_count, 1),
        "rateLimitedCount": rate_limited,
        "latencyMs": {
            "min": round(min(latencies), 1) if latencies else 0.0,
            "average": average,
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "max": round(max(latencies), 1) if latencies else 0.0,
        },
        "results": results,
        "notes": [
            "只读调用钉钉 user/get，不写员工、收藏或工作点绑定数据。",
            "该结果衡量钉钉用户详情读取；完整首次登录还包含免登换码和部门路径解析。",
            "并发压测会消耗钉钉接口配额，出现 90002 表示触发平台限流。",
        ],
    }


async def run_user_fetch_load_test(
    userids: list[str],
    request_count: int,
    concurrency: int,
    *,
    token_provider: Callable[[], Awaitable[str]] = _get_access_token,
) -> dict[str, Any]:
    """Run one bounded test; reject overlap and back-to-back quota bursts."""
    global _last_run_finished_at

    if _run_lock.locked():
        raise DingtalkLoadTestBusyError("已有钉钉信息获取压测正在运行，请等待完成")
    remaining = MIN_RUN_INTERVAL_SECONDS - (time.monotonic() - _last_run_finished_at)
    if _last_run_finished_at and remaining > 0:
        raise DingtalkLoadTestCooldownError(math.ceil(remaining))
    async with _run_lock:
        try:
            token = await token_provider()
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                return await _run_with_client(
                    client,
                    token,
                    userids,
                    request_count,
                    concurrency,
                )
        finally:
            _last_run_finished_at = time.monotonic()
