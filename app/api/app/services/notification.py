"""Notification channels (mock / dingtalk)."""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Employee, NotificationRecord

logger = logging.getLogger("fdc.notify")
SH = ZoneInfo("Asia/Shanghai")


def _readable_text(subject: str, body: str) -> str:
    """把站内通知拼成钉钉工作通知的纯文本：body 若是 JSON(缺编/上岗等)取其 detail。"""
    text = body or ""
    try:
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict) and parsed.get("detail"):
            text = str(parsed["detail"])
    except (json.JSONDecodeError, TypeError):
        pass
    return f"{subject}\n{text}".strip() if subject else text


def _send_work_message_blocking(dd: str, text: str, timeout: float = 8.0) -> dict:
    """发钉钉工作通知(asyncsend 受理)并返回受理结果，最多阻塞 timeout 秒。

    统一丢到新线程跑 asyncio.run（线程内必无运行中事件循环，避免「loop already running」，
    同时 join(timeout) 给主流程一个上限，钉钉迟缓/不可达时不至于把扫码等接口卡死）。
    超时抛 TimeoutError（后台线程仍会 best-effort 继续发，但本次按未确认处理）。
    """
    import asyncio

    from app.services.dingtalk_service import send_work_message

    box: dict = {}

    def _r() -> None:
        try:
            box["r"] = asyncio.run(send_work_message(dd, text))  # errcode!=0 抛错
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_r, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"钉钉推送 {timeout}s 未确认")
    if "e" in box:
        raise box["e"]
    return box.get("r", {})


def _mirror_to_dingtalk(db: Session, target: str, subject: str, body: str) -> tuple[str, dict]:
    """把站内通知镜像一条真实钉钉工作通知给目标员工。返回 (需在通知中心标注的提示, 投递结果)。

    规则：① 未配置钉钉凭据 → 不推也不标注（全局未启用真实推送）；
    ② 目标员工无钉钉号 → 返回「钉钉推送失败：该员工无钉钉号」；
    ③ 有钉钉号 → 同步发送并捕获真实受理结果（不再 fire-and-forget 丢结果，避免进程重启/异常时
       静默丢失且日志误显示「已送达」）。失败时返回失败提示 + 结果，由调用方记入推送记录。
    """
    from app.services.dingtalk_service import is_configured

    if not is_configured():
        return "", {}
    try:
        emp = db.scalar(select(Employee).where(Employee.emp_id == target))
        dd = (emp.dd_user_id or "").strip() if emp else ""
    except Exception:  # noqa: BLE001
        dd = ""
    if not dd:
        return "钉钉推送失败：该员工无钉钉号", {}
    # 钉钉工作通知带上发送时间戳（上海时区）
    ts = datetime.now(SH).strftime("%Y-%m-%d %H:%M")
    text = f"{_readable_text(subject, body)}\n时间：{ts}"
    try:
        accept = _send_work_message_blocking(dd, text)
        if accept.get("skipped"):
            # 用户未开启钉钉通知 → 未发送，标注但不算失败
            reason = accept.get("reason") or "该员工未开启钉钉通知"
            return f"钉钉未推送：{reason}", {"ok": False, "skipped": True, "error": reason}
        return "", {"ok": True, "errcode": accept.get("errcode", 0), "taskId": accept.get("task_id")}
    except Exception as e:  # noqa: BLE001
        logger.info("镜像钉钉推送失败(target=%s dd=%s)：%s", target, dd, e)
        return f"钉钉推送失败：{e}", {"ok": False, "error": str(e)}


class NotificationChannel(ABC):
    @abstractmethod
    def send(self, db: Session, *, target: str, subject: str, body: str) -> None:
        ...


class MockChannel(NotificationChannel):
    def send(self, db: Session, *, target: str, subject: str, body: str) -> None:
        # 同步镜像真实钉钉推送并记录真实结果（不再 fire-and-forget），推送日志据此显示真实送达状态
        push_note, result = _mirror_to_dingtalk(db, target, subject, body)
        payload: dict = {"subject": subject, "body": body}
        if push_note:
            payload["pushNote"] = push_note
        if result:
            payload["result"] = result
        if result.get("ok"):
            status = "sent"
        elif result:  # 已尝试但失败
            status = "failed"
        elif push_note:  # 无钉钉号，未尝试
            status = "skipped"
        else:  # 未配置钉钉，全局未启用真实推送
            status = ""
        db.add(
            NotificationRecord(
                channel="mock",
                target=target,
                status=status,
                payload=json.dumps(payload, ensure_ascii=False),
                ts=datetime.now(timezone.utc),
            )
        )


class DingTalkChannel(NotificationChannel):
    def send(self, db: Session, *, target: str, subject: str, body: str) -> None:
        # Placeholder: real钉钉凭据接入后实现
        MockChannel().send(db, target=target, subject=subject, body=body)


def get_notification_channel() -> NotificationChannel:
    if settings.fdc_notify_mode == "dingtalk":
        return DingTalkChannel()
    return MockChannel()
