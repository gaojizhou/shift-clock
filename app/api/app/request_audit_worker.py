"""Consume request audit events from Redis Streams and batch-write PostgreSQL.

Run as a separate service::

    python -m app.request_audit_worker

The worker acknowledges Redis entries only after the database commit.  A unique
``event_id`` makes crash recovery idempotent when a committed batch is replayed.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import AppSession, AuditLog
from app.services.request_audit_queue import RequestAuditEvent
from app.session_store import session_ttl_seconds

logger = logging.getLogger(__name__)
_stop = threading.Event()


def _parse_occurred_at(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def persist_events(db: Session, events: list[RequestAuditEvent]) -> int:
    """Persist one batch and renew still-valid sessions; caller owns commit/rollback."""
    event_ids = list(dict.fromkeys(event.event_id for event in events))
    existing = set(
        db.scalars(select(AuditLog.event_id).where(AuditLog.event_id.in_(event_ids))).all()
    )
    inserted = 0
    for event in events:
        if event.event_id in existing:
            continue
        db.add(
            AuditLog(
                event_id=event.event_id,
                actor=event.actor,
                action=event.action,
                payload=event.payload,
                ts=_parse_occurred_at(event.occurred_at),
            )
        )
        existing.add(event.event_id)
        inserted += 1

    session_ids = {event.session_id for event in events if event.session_id}
    if session_ids:
        now = datetime.now(UTC)
        db.execute(
            update(AppSession)
            .where(
                AppSession.session_id.in_(session_ids),
                AppSession.expires_at > now,
            )
            .values(
                updated_at=now,
                expires_at=now + timedelta(seconds=session_ttl_seconds()),
            )
        )
    return inserted


def _ensure_group(client: Redis, stream: str, group: str) -> None:
    try:
        client.xgroup_create(stream, group, id="0-0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _claim_stale(client: Redis, stream: str, group: str, consumer: str) -> list:
    claimed = client.xautoclaim(
        stream,
        group,
        consumer,
        min_idle_time=settings.fdc_request_audit_claim_idle_ms,
        start_id="0-0",
        count=settings.fdc_request_audit_batch_size,
    )
    return list(claimed[1]) if len(claimed) > 1 else []


def _read_new(
    client: Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    count: int | None = None,
    block_ms: int | None = None,
) -> list:
    rows = client.xreadgroup(
        group,
        consumer,
        {stream: ">"},
        count=count or settings.fdc_request_audit_batch_size,
        block=block_ms or settings.fdc_request_audit_block_ms,
    )
    return list(rows[0][1]) if rows else []


def _fill_batch(
    client: Redis,
    stream: str,
    group: str,
    consumer: str,
    messages: list,
) -> list:
    """Linger briefly after the first event so bursts become one database commit."""
    deadline = time.monotonic() + settings.fdc_request_audit_flush_interval_ms / 1000
    while len(messages) < settings.fdc_request_audit_batch_size:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        more = _read_new(
            client,
            stream,
            group,
            consumer,
            count=settings.fdc_request_audit_batch_size - len(messages),
            block_ms=max(1, remaining_ms),
        )
        if more:
            messages.extend(more)
    return messages


def _dead_letter(
    client: Redis,
    stream: str,
    group: str,
    message_id: str,
    fields: dict[str, Any],
    exc: Exception,
) -> None:
    client.xadd(
        f"{stream}:dead",
        {
            "sourceId": message_id,
            "event": str(fields.get("event") or "")[:65536],
            "error": f"{exc.__class__.__name__}: {exc}"[:1000],
        },
        maxlen=1000,
        approximate=True,
    )
    pipe = client.pipeline(transaction=False)
    pipe.xack(stream, group, message_id)
    pipe.xdel(stream, message_id)
    pipe.execute()


def _process_messages(client: Redis, stream: str, group: str, messages: list) -> int:
    valid: list[tuple[str, RequestAuditEvent]] = []
    for message_id, fields in messages:
        try:
            valid.append((message_id, RequestAuditEvent.from_json(fields["event"])))
        except Exception as exc:  # noqa: BLE001 - poison messages must not block the stream forever
            logger.error("请求审计消息 %s 无效，移入死信队列：%s", message_id, exc)
            _dead_letter(client, stream, group, message_id, fields, exc)

    if not valid:
        return 0
    with SessionLocal() as db:
        try:
            inserted = persist_events(db, [event for _, event in valid])
            db.commit()
        except Exception:
            db.rollback()
            raise

    ids = [message_id for message_id, _ in valid]
    pipe = client.pipeline(transaction=False)
    pipe.xack(stream, group, *ids)
    pipe.xdel(stream, *ids)
    pipe.execute()
    return inserted


def _request_stop(_signum, _frame) -> None:
    _stop.set()


def _heartbeat(client: Redis, stream: str) -> None:
    """Publish a short-lived liveness marker consumed by the API health probe."""
    client.set(
        f"{stream}:worker-heartbeat",
        f"{time.time():.3f}",
        ex=settings.fdc_request_audit_worker_heartbeat_ttl_seconds,
    )


def _try_heartbeat(client: Redis, stream: str) -> None:
    """Heartbeat failure must not prevent the worker from draining the stream."""
    try:
        _heartbeat(client, stream)
    except Exception:  # noqa: BLE001 - Redis may reject SET at maxmemory
        logger.warning("请求审计 worker 心跳写入失败，继续消费 Stream", exc_info=True)


def _validate_runtime_settings() -> None:
    longest_poll_seconds = (
        settings.fdc_request_audit_block_ms
        + settings.fdc_request_audit_flush_interval_ms
    ) / 1000
    minimum_heartbeat_ttl = longest_poll_seconds + 2
    if settings.fdc_request_audit_worker_heartbeat_ttl_seconds <= minimum_heartbeat_ttl:
        raise RuntimeError(
            "FDC_REQUEST_AUDIT_WORKER_HEARTBEAT_TTL_SECONDS 必须大于 "
            "block + flush + 2s 安全余量"
        )


def run() -> None:
    if not settings.fdc_request_audit_redis_url.strip():
        raise RuntimeError("FDC_REQUEST_AUDIT_REDIS_URL 未配置，审计 worker 拒绝启动")
    _validate_runtime_settings()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    stream = settings.fdc_request_audit_stream
    group = settings.fdc_request_audit_group
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    client = Redis.from_url(
        settings.fdc_request_audit_redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.fdc_request_audit_redis_connect_timeout_seconds,
        socket_timeout=max(
            settings.fdc_request_audit_redis_command_timeout_seconds,
            (
                settings.fdc_request_audit_block_ms
                + settings.fdc_request_audit_flush_interval_ms
            )
            / 1000
            + 1,
        ),
        health_check_interval=30,
    )
    backoff = 1.0
    logger.info("请求审计 worker 启动：stream=%s group=%s consumer=%s", stream, group, consumer)
    while not _stop.is_set():
        try:
            _ensure_group(client, stream, group)
            messages = _claim_stale(client, stream, group, consumer)
            if not messages:
                messages = _read_new(client, stream, group, consumer)
            if messages:
                messages = _fill_batch(client, stream, group, consumer, messages)
                inserted = _process_messages(client, stream, group, messages)
                logger.info("请求审计批次完成：收到=%s 新增=%s", len(messages), inserted)
            # 只有成功完成一轮消费（包括无消息的成功轮询）才续心跳。
            # PostgreSQL 持久化失败时会直接进入 except，不会伪报健康。
            _try_heartbeat(client, stream)
            backoff = 1.0
        except Exception:
            logger.exception("请求审计批量写入失败，%.1f 秒后重试", backoff)
            _stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
    client.close()
    logger.info("请求审计 worker 已停止")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
