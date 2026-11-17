"""Redis Stream buffer for high-volume ``request_*`` audit records.

Business audit rows (locks, proxy sign-in, employee changes, and so on) stay in
their original database transactions.  Only the generic request middleware uses
this buffer, so a Redis incident cannot affect business-data consistency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestAuditEvent:
    event_id: str
    actor: str
    action: str
    payload: str
    occurred_at: str
    session_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        actor: str,
        action: str,
        payload: str,
        session_id: str | None,
    ) -> RequestAuditEvent:
        return cls(
            event_id=str(uuid4()),
            actor=actor[:64],
            action=action[:64],
            payload=payload,
            occurred_at=datetime.now(UTC).isoformat(),
            session_id=session_id,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> RequestAuditEvent:
        data: dict[str, Any] = json.loads(raw)
        event = cls(
            event_id=str(data["event_id"]),
            actor=str(data["actor"]),
            action=str(data["action"]),
            payload=str(data["payload"]),
            occurred_at=str(data["occurred_at"]),
            session_id=(str(data["session_id"]) if data.get("session_id") else None),
        )
        if not event.event_id or len(event.event_id) > 64:
            raise ValueError("invalid event_id")
        if not event.action.startswith("request_") or len(event.action) > 64:
            raise ValueError("invalid request audit action")
        if not event.actor or len(event.actor) > 64:
            raise ValueError("invalid actor")
        if event.session_id and len(event.session_id) > 64:
            raise ValueError("invalid session_id")
        # Fail malformed events before they can poison a whole database batch.
        json.loads(event.payload)
        datetime.fromisoformat(event.occurred_at)
        return event


@dataclass(frozen=True, slots=True)
class _BufferedEvent:
    event_id: str
    raw: str
    serialized_bytes: int
    queued_at: float


class RequestAuditBuffer:
    """Two-level producer: bounded in-process queue, then batched Redis Stream writes."""

    def __init__(
        self,
        redis_url: str,
        stream: str,
        *,
        memory_queue_size: int = 5000,
        memory_queue_bytes: int = 32 * 1024 * 1024,
        writer_count: int = 2,
        redis_batch_size: int = 200,
        redis_flush_interval_ms: int = 25,
        shutdown_timeout_seconds: float = 5.0,
        redis_connect_timeout_seconds: float = 0.5,
        redis_command_timeout_seconds: float = 2.0,
        redis_pipeline_timeout_seconds: float = 2.0,
        stream_maxlen: int = 25000,
        worker_heartbeat_ttl_seconds: int = 15,
    ) -> None:
        self.redis_url = redis_url.strip()
        self.stream = stream
        self.memory_queue_size = memory_queue_size
        self.memory_queue_bytes = memory_queue_bytes
        self.writer_count = writer_count
        self.redis_batch_size = redis_batch_size
        self.redis_flush_interval = redis_flush_interval_ms / 1000
        self.shutdown_timeout = shutdown_timeout_seconds
        self.redis_connect_timeout = redis_connect_timeout_seconds
        self.redis_command_timeout = redis_command_timeout_seconds
        self.redis_pipeline_timeout = redis_pipeline_timeout_seconds
        self.stream_maxlen = stream_maxlen
        self.worker_heartbeat_ttl = worker_heartbeat_ttl_seconds
        self.worker_heartbeat_key = f"{stream}:worker-heartbeat"
        self._client: Redis | None = None
        # Queue/lock are created by start() in the active lifespan event loop.
        # Module-level construction must not bind them to an arbitrary earlier loop.
        self._queue: asyncio.Queue[_BufferedEvent] | None = None
        self._writers: list[asyncio.Task[None]] = []
        self._diagnostics_lock: asyncio.Lock | None = None
        self._accepting = False
        self._available = False
        self._accepted = 0
        self._enqueued = 0
        self._failed = 0
        self._overflowed = 0
        self._byte_overflowed = 0
        self._redis_unavailable_fallbacks = 0
        self._redis_write_failures = 0
        self._redis_retry_events = 0
        self._pending_since: dict[str, float] = {}
        self._pending_items: dict[str, _BufferedEvent] = {}
        self._pending_bytes = 0
        self._shutdown_fallback_persisted = 0
        self._shutdown_dropped = 0
        self._last_error: str | None = None
        self._unavailable_since: float | None = None
        self._last_warning_at = 0.0
        self._stream_health: dict[str, Any] | None = None
        self._stream_health_sampled_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.redis_url)

    def _get_client(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                max_connections=10,
                socket_connect_timeout=self.redis_connect_timeout,
                socket_timeout=self.redis_command_timeout,
                health_check_interval=30,
            )
        return self._client

    async def start(self) -> None:
        if not self.enabled or self._writers:
            return
        self._queue = asyncio.Queue(maxsize=self.memory_queue_size)
        self._diagnostics_lock = asyncio.Lock()
        try:
            await asyncio.wait_for(
                self._get_client().ping(),
                timeout=self.redis_command_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - logging must never block application startup
            self._record_redis_failure(exc, event_count=0)
            logger.warning("请求审计 Redis 暂不可用，业务接口继续启动：%s", exc)
        else:
            self._record_redis_available()
            logger.info("请求审计 Redis Stream 已连接：%s", self.stream)
        self._accepting = True
        self._writers = [
            asyncio.create_task(
                self._writer_loop(index),
                name=f"request-audit-redis-writer-{index}",
            )
            for index in range(self.writer_count)
        ]

    async def close(self) -> None:
        self._accepting = False
        queue = self._queue
        if self._writers:
            try:
                if queue is not None:
                    await asyncio.wait_for(queue.join(), timeout=self.shutdown_timeout)
            except TimeoutError:
                logger.warning(
                    "请求审计内存队列关闭前未排空：pending=%s bytes=%s",
                    len(self._pending_since),
                    self._pending_bytes,
                )
            for task in self._writers:
                task.cancel()
            await asyncio.gather(*self._writers, return_exceptions=True)
            self._writers.clear()
        leftovers = list(self._pending_items.values())
        if leftovers:
            persisted, secured = self._persist_shutdown_security_events(leftovers)
            dropped = len(leftovers) - secured
            self._shutdown_fallback_persisted += persisted
            self._shutdown_dropped += dropped
            self._failed += dropped
            logger.error(
                "请求审计关停时剩余事件：pending=%s securityPersisted=%s dropped=%s",
                len(leftovers),
                persisted,
                dropped,
            )
            self._pending_items.clear()
            self._pending_since.clear()
            self._pending_bytes = 0
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._available = False
        self._queue = None
        self._diagnostics_lock = None

    async def enqueue(self, event: RequestAuditEvent) -> bool:
        if not self.enabled or not self._accepting:
            return False
        queue = self._queue
        if queue is None or not self._writers_alive():
            self._failed += 1
            if event.action != "request_ok":
                self._redis_unavailable_fallbacks += 1
            return False
        # 安全相关的拒绝/错误日志不能只停留在进程内存。Redis 已明确不可用时
        # 直接交回中间件同步落库；普通 request_ok 仍可短时排队等待 Redis 恢复。
        if event.action != "request_ok":
            stream_health = self._stream_health or {}
            consumer_lag = stream_health.get("consumerLag")
            worker_unhealthy = stream_health.get("workerHealthy") is False
            lag_critical = (
                isinstance(consumer_lag, (int, float))
                and consumer_lag >= self.stream_maxlen * 0.8
            )
            if not self._available or worker_unhealthy or lag_critical:
                self._failed += 1
                self._redis_unavailable_fallbacks += 1
                if worker_unhealthy or lag_critical:
                    self._warn_rate_limited(
                        "请求审计 worker 不健康或积压接近裁剪上限，安全事件改走同步兜底："
                        "workerHealthy=%s consumerLag=%s maxlen=%s",
                        stream_health.get("workerHealthy"),
                        consumer_lag,
                        self.stream_maxlen,
                    )
                return False
        raw = event.to_json()
        item = _BufferedEvent(
            event_id=event.event_id,
            raw=raw,
            serialized_bytes=len(raw.encode("utf-8")),
            queued_at=time.monotonic(),
        )
        if self._pending_bytes + item.serialized_bytes > self.memory_queue_bytes:
            self._failed += 1
            self._overflowed += 1
            self._byte_overflowed += 1
            self._last_error = f"MemoryQueueBytesFull: capacity={self.memory_queue_bytes}"
            self._warn_rate_limited(
                "请求审计内存队列字节上限已满，调用方执行分级降级：capacityBytes=%s",
                self.memory_queue_bytes,
            )
            return False
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self._failed += 1
            self._overflowed += 1
            self._last_error = f"MemoryQueueFull: capacity={self.memory_queue_size}"
            self._warn_rate_limited(
                "请求审计内存队列已满，调用方执行分级降级：capacity=%s",
                self.memory_queue_size,
            )
            return False
        self._accepted += 1
        self._pending_since[item.event_id] = item.queued_at
        self._pending_items[item.event_id] = item
        self._pending_bytes += item.serialized_bytes
        return True

    async def _writer_loop(self, index: int) -> None:
        while True:
            batch: list[_BufferedEvent] | None = None
            try:
                batch = await self._collect_batch()
                backoff = 0.05
                while True:
                    try:
                        await self._write_batch(batch)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - retained batch is retried
                        self._record_redis_failure(exc, event_count=len(batch))
                        self._warn_rate_limited(
                            "请求审计 Redis 批量写入失败，writer=%s batch=%s，将重试：%s",
                            index,
                            len(batch),
                            exc,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 2.0)
                        continue
                    self._record_batch_success(batch)
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - writer must remain supervised
                event_count = len(batch) if batch is not None else 0
                self._record_redis_failure(exc, event_count=event_count)
                self._warn_rate_limited(
                    "请求审计 Redis writer 异常，writer=%s batch=%s，将自动恢复：%s",
                    index,
                    event_count,
                    exc,
                )
                await asyncio.sleep(0.05)

    async def _collect_batch(self) -> list[_BufferedEvent]:
        queue = self._queue
        if queue is None:
            raise RuntimeError("request audit queue is not started")
        batch = [await queue.get()]
        deadline = time.monotonic() + self.redis_flush_interval
        while len(batch) < self.redis_batch_size:
            try:
                batch.append(queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
            try:
                batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _write_batch(self, batch: list[_BufferedEvent]) -> None:
        pipe = self._get_client().pipeline(transaction=False)
        for item in batch:
            pipe.xadd(
                self.stream,
                {"event": item.raw},
                maxlen=self.stream_maxlen,
                approximate=True,
            )
        # pipeline 超时后 Redis 可能已经执行了部分或全部 XADD；整批重投是有意的，
        # 消费端依靠 event_id 唯一索引保证至少一次投递不会重复落库。
        await asyncio.wait_for(
            pipe.execute(),
            timeout=self.redis_pipeline_timeout,
        )

    def _record_batch_success(self, batch: list[_BufferedEvent]) -> None:
        self._record_redis_available()
        self._enqueued += len(batch)
        for item in batch:
            self._pending_since.pop(item.event_id, None)
            self._pending_items.pop(item.event_id, None)
            self._pending_bytes = max(0, self._pending_bytes - item.serialized_bytes)
            if self._queue is not None:
                self._queue.task_done()

    def _writers_alive(self) -> bool:
        return bool(self._writers) and all(not task.done() for task in self._writers)

    def _persist_shutdown_security_events(
        self, items: list[_BufferedEvent]
    ) -> tuple[int, int]:
        """Return (newly inserted, secured including already-present duplicates)."""
        from app.db import SessionLocal
        from app.models import AuditLog

        events: list[RequestAuditEvent] = []
        for item in items:
            try:
                event = RequestAuditEvent.from_json(item.raw)
            except Exception:
                continue
            if event.action != "request_ok":
                events.append(event)
        if not events:
            return 0, 0
        inserted = 0
        secured = 0
        try:
            with SessionLocal() as db:
                for event in events:
                    try:
                        with db.begin_nested():
                            db.add(
                                AuditLog(
                                    event_id=event.event_id,
                                    actor=event.actor,
                                    action=event.action,
                                    payload=event.payload,
                                    ts=datetime.fromisoformat(event.occurred_at),
                                )
                            )
                            # SAVEPOINT 内 flush，让单条唯一冲突在此处隔离。
                            db.flush()
                    except IntegrityError:
                        # 该 event_id 已由 worker 并发落库，视为已安全保存。
                        secured += 1
                        continue
                    except Exception:  # noqa: BLE001 - continue with remaining security rows
                        logger.exception(
                            "请求审计关停兜底单条写入失败：eventId=%s",
                            event.event_id,
                        )
                        continue
                    inserted += 1
                    secured += 1
                db.commit()
        except Exception:  # noqa: BLE001 - shutdown must continue after logging failure
            logger.exception("请求审计关停同步兜底写库失败")
            return 0, 0
        return inserted, secured

    def _record_redis_failure(self, exc: Exception, *, event_count: int) -> None:
        self._available = False
        if self._unavailable_since is None:
            self._unavailable_since = time.monotonic()
        if event_count:
            self._redis_write_failures += 1
        self._redis_retry_events += event_count
        self._last_error = f"{exc.__class__.__name__}: {exc}"[:500]

    def _record_redis_available(self) -> None:
        self._available = True
        self._unavailable_since = None
        self._last_error = None

    def _warn_rate_limited(self, message: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._last_warning_at >= 30:
            logger.warning(message, *args)
            self._last_warning_at = now

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        oldest = min(self._pending_since.values(), default=None)
        waiting = self._queue.qsize() if self._queue is not None else 0
        pending = len(self._pending_since)
        item_utilization = waiting * 100 / self.memory_queue_size
        byte_utilization = self._pending_bytes * 100 / self.memory_queue_bytes
        return {
            "enabled": self.enabled,
            "available": self._available if self.enabled else None,
            "stream": self.stream if self.enabled else None,
            "acceptedSinceStart": self._accepted,
            "enqueuedSinceStart": self._enqueued,
            "failedSinceStart": self._failed,
            "overflowedSinceStart": self._overflowed,
            "byteOverflowedSinceStart": self._byte_overflowed,
            "redisUnavailableFallbacksSinceStart": self._redis_unavailable_fallbacks,
            "redisWriteFailuresSinceStart": self._redis_write_failures,
            "redisRetryEventsSinceStart": self._redis_retry_events,
            "shutdownFallbackPersistedSinceStart": self._shutdown_fallback_persisted,
            "shutdownDroppedSinceStart": self._shutdown_dropped,
            "unavailableDurationSeconds": (
                round(now - self._unavailable_since, 1)
                if self._unavailable_since is not None
                else 0.0
            ),
            "memoryQueue": {
                "capacity": self.memory_queue_size,
                "capacityBytes": self.memory_queue_bytes,
                "waiting": waiting,
                "inFlight": max(0, pending - waiting),
                "pending": pending,
                "utilizationPercent": round(max(item_utilization, byte_utilization), 1),
                "itemUtilizationPercent": round(item_utilization, 1),
                "byteUtilizationPercent": round(byte_utilization, 1),
                "serializedBytes": self._pending_bytes,
                "oldestEventAgeMs": round((now - oldest) * 1000, 1) if oldest else 0.0,
                "writerCount": self.writer_count,
                "writersAlive": sum(not task.done() for task in self._writers),
                "batchSize": self.redis_batch_size,
            },
            "streamHealth": self._stream_health,
            "lastError": self._last_error,
        }

    async def diagnostics(self) -> dict[str, Any]:
        """Return admin-only details, refreshing Redis/worker metrics at most every 5s."""
        if not self.enabled:
            return self.snapshot()
        lock = self._diagnostics_lock
        if lock is None:
            return self.snapshot()
        async with lock:
            now = time.monotonic()
            if now - self._stream_health_sampled_at >= 5:
                try:
                    length, groups, heartbeat = await asyncio.wait_for(
                        asyncio.gather(
                            self._get_client().xlen(self.stream),
                            self._get_client().xinfo_groups(self.stream),
                            self._get_client().get(self.worker_heartbeat_key),
                        ),
                        timeout=self.redis_command_timeout,
                    )
                    group = next(
                        (
                            item
                            for item in groups
                            if str(item.get("name") or "") == settings.fdc_request_audit_group
                        ),
                        {},
                    )
                    heartbeat_age = (
                        max(0.0, time.time() - float(heartbeat))
                        if heartbeat is not None
                        else None
                    )
                    self._stream_health = {
                        "length": int(length),
                        "pending": int(group.get("pending") or 0),
                        "consumerLag": int(group.get("lag") or 0),
                        "workerHeartbeatAgeSeconds": (
                            round(heartbeat_age, 1) if heartbeat_age is not None else None
                        ),
                        "workerHealthy": bool(
                            heartbeat_age is not None
                            and heartbeat_age <= self.worker_heartbeat_ttl
                        ),
                        "maxLength": self.stream_maxlen,
                        "sampledAt": datetime.now(UTC).isoformat(),
                        "probeError": None,
                    }
                    # 无业务流量时，成功的真实 Redis 探针也应恢复生产者可用状态。
                    self._record_redis_available()
                except Exception as exc:  # noqa: BLE001 - diagnostics must not break health
                    self._stream_health = {
                        "length": None,
                        "pending": None,
                        "consumerLag": None,
                        "workerHeartbeatAgeSeconds": None,
                        "workerHealthy": False,
                        "maxLength": self.stream_maxlen,
                        "sampledAt": datetime.now(UTC).isoformat(),
                        "probeError": exc.__class__.__name__,
                    }
                self._stream_health_sampled_at = now
        return self.snapshot()

    async def public_diagnostics(self) -> dict[str, Any]:
        """Expose only coarse health to unauthenticated callers."""
        details = await self.diagnostics()
        memory = details["memoryQueue"]
        stream_health = details.get("streamHealth") or {}
        utilization = float(memory.get("utilizationPercent") or 0)
        backlog_level = (
            "critical" if utilization >= 90 else "warning" if utilization >= 70 else "normal"
        )
        worker_healthy = stream_health.get("workerHealthy")
        healthy = (not details["enabled"]) or (
            bool(details.get("available"))
            and self._writers_alive()
            and backlog_level != "critical"
            and worker_healthy is not False
        )
        return {
            "enabled": details["enabled"],
            "available": details["available"],
            "status": "healthy" if healthy else "degraded",
            "backlogLevel": backlog_level,
            "workerHealthy": worker_healthy,
        }


request_audit_buffer = RequestAuditBuffer(
    settings.fdc_request_audit_redis_url,
    settings.fdc_request_audit_stream,
    memory_queue_size=settings.fdc_request_audit_memory_queue_size,
    memory_queue_bytes=settings.fdc_request_audit_memory_queue_bytes,
    writer_count=settings.fdc_request_audit_redis_writer_count,
    redis_batch_size=settings.fdc_request_audit_redis_batch_size,
    redis_flush_interval_ms=settings.fdc_request_audit_redis_flush_interval_ms,
    shutdown_timeout_seconds=settings.fdc_request_audit_shutdown_timeout_seconds,
    redis_connect_timeout_seconds=settings.fdc_request_audit_redis_connect_timeout_seconds,
    redis_command_timeout_seconds=settings.fdc_request_audit_redis_command_timeout_seconds,
    redis_pipeline_timeout_seconds=settings.fdc_request_audit_redis_pipeline_timeout_seconds,
    stream_maxlen=settings.fdc_request_audit_stream_maxlen,
    worker_heartbeat_ttl_seconds=settings.fdc_request_audit_worker_heartbeat_ttl_seconds,
)
