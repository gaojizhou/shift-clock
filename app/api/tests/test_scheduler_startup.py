import asyncio
import builtins

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.config import settings
from app.main import lifespan


def test_scheduler_import_error_fails_application_startup(monkeypatch):
    real_import = builtins.__import__

    def fail_apscheduler(name, *args, **kwargs):
        if name.startswith("apscheduler"):
            raise ImportError("broken tzlocal")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_apscheduler)
    test_app = FastAPI()

    async def start():
        async with lifespan(test_app):
            pass

    with pytest.raises(ImportError, match="broken tzlocal"):
        asyncio.run(start())
    assert test_app.state.scheduler_status["started"] is False
    assert "broken tzlocal" in test_app.state.scheduler_status["error"]


def test_invalid_timezone_degrades_instead_of_crashing_startup(monkeypatch):
    """FDC_ESCALATION_TZ 配置笔误不得让应用启动失败（restart 循环会拖垮打卡主链路）：
    调度器降级停用、error 给出可操作提示，应用照常提供接口。"""
    monkeypatch.setattr(main_module, "_scheduler", None)
    monkeypatch.setattr(settings, "fdc_escalation_tz", "Not/AZone")
    test_app = FastAPI()
    captured = {}

    async def start():
        async with lifespan(test_app):
            captured.update(test_app.state.scheduler_status)

    asyncio.run(start())
    assert captured["started"] is False
    assert captured["jobIds"] == []
    assert "Not/AZone" in captured["error"]
    assert "FDC_ESCALATION_TZ" in captured["error"]


def test_single_job_import_failure_degrades_only_that_job(monkeypatch):
    """单个 job 模块导入失败只降级该 job，其余任务照常启动。"""
    real_resolve = main_module._resolve_job

    def fail_employee_sync(job_id):
        if job_id == "fdc_employee_difference_sync_daily":
            raise ImportError("employee sync module renamed")
        return real_resolve(job_id)

    monkeypatch.setattr(main_module, "_resolve_job", fail_employee_sync)
    # 外部考勤源已配置，确保考勤任务参与本次「其余任务照常启动」的断言。
    monkeypatch.setattr(settings, "attendance_mysql_host", "127.0.0.1")
    test_app = FastAPI()
    captured = {}

    async def start():
        async with lifespan(test_app):
            captured.update(test_app.state.scheduler_status)

    asyncio.run(start())
    assert captured["started"] is False
    assert captured.get("allHealthy") is False
    assert "fdc_employee_difference_sync_daily" not in captured["jobIds"]
    assert {
        "fdc_escalation_daily",
        "fdc_shift_push_minutely",
        "fdc_attendance_sync_daily",
    } <= set(captured["jobIds"])
    assert any("fdc_employee_difference_sync_daily" in item for item in captured["failedJobs"])
    assert captured["error"] is not None


def test_attendance_scheduler_can_be_disabled_for_maintenance(monkeypatch):
    monkeypatch.setattr(main_module.settings, "fdc_attendance_sync_scheduler_enabled", False)
    monkeypatch.setattr(settings, "attendance_mysql_host", "127.0.0.1")
    test_app = FastAPI()
    captured = {}

    async def start():
        async with lifespan(test_app):
            captured.update(test_app.state.scheduler_status)

    asyncio.run(start())
    assert captured["started"] is True
    assert captured["allHealthy"] is True
    assert "fdc_attendance_sync_daily" not in captured["jobIds"]


def test_attendance_scheduler_skipped_when_source_not_configured(monkeypatch):
    """外部考勤源未配置时不要注册每日拉取任务，避免持续报连接错误。"""
    monkeypatch.setattr(settings, "attendance_mysql_host", "")
    test_app = FastAPI()
    captured = {}

    async def start():
        async with lifespan(test_app):
            captured.update(test_app.state.scheduler_status)

    asyncio.run(start())
    assert "fdc_attendance_sync_daily" not in captured["jobIds"]
