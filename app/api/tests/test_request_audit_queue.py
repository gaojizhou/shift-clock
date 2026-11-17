import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.models import AppSession, AuditLog
from app.request_audit_worker import persist_events
from app.services.request_audit_queue import RequestAuditBuffer, RequestAuditEvent


class FakeAuditBuffer:
    enabled = True

    def __init__(self, succeeds: bool = True):
        self.succeeds = succeeds
        self.events: list[RequestAuditEvent] = []

    async def enqueue(self, event: RequestAuditEvent) -> bool:
        self.events.append(event)
        return self.succeeds

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def snapshot(self):
        return {"enabled": True, "available": self.succeeds}


def _event(
    *,
    event_id: str = "evt-1",
    session_id: str | None = None,
    action: str = "request_ok",
) -> RequestAuditEvent:
    return RequestAuditEvent(
        event_id=event_id,
        actor="admin",
        action=action,
        payload=json.dumps({"status": 200}),
        occurred_at=datetime.now(UTC).isoformat(),
        session_id=session_id,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands: list[tuple[str, dict[str, str], dict]] = []

    def xadd(self, stream: str, fields: dict[str, str], **kwargs):
        self.commands.append((stream, fields, kwargs))
        return self

    async def execute(self):
        self.redis.execute_calls += 1
        self.redis.execute_started.set()
        if self.redis.release_execute is not None:
            await self.redis.release_execute.wait()
        if self.redis.failures_remaining:
            self.redis.failures_remaining -= 1
            raise ConnectionError("temporary redis failure")
        self.redis.messages.extend(self.commands)
        return [f"1-{index}" for index, _ in enumerate(self.commands)]


class _FakeRedis:
    def __init__(self, *, failures: int = 0, blocked: bool = False):
        self.failures_remaining = failures
        self.release_execute = asyncio.Event() if blocked else None
        self.execute_started = asyncio.Event()
        self.execute_calls = 0
        self.messages: list[tuple[str, dict[str, str], dict]] = []
        self.closed = False

    async def ping(self):
        return True

    async def xlen(self, _stream):
        return len(self.messages)

    async def xinfo_groups(self, _stream):
        return [{"name": "fdc-request-audit-writers", "pending": 0, "lag": 0}]

    async def get(self, _key):
        return str(time.time())

    def pipeline(self, *, transaction: bool):
        assert transaction is False
        return _FakePipeline(self)

    async def aclose(self):
        self.closed = True


def test_event_round_trip_and_rejects_non_request_action():
    event = _event()
    assert RequestAuditEvent.from_json(event.to_json()) == event
    invalid = json.loads(event.to_json())
    invalid["action"] = "employee_update"
    with pytest.raises(ValueError, match="request audit action"):
        RequestAuditEvent.from_json(json.dumps(invalid))


def test_memory_queue_batches_redis_writes_and_drains_on_close():
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=10,
            writer_count=1,
            redis_batch_size=3,
            redis_flush_interval_ms=10,
            shutdown_timeout_seconds=1,
        )
        fake = _FakeRedis()
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(_event(event_id="batch-1")) is True
        assert await buffer.enqueue(_event(event_id="batch-2")) is True
        assert await buffer.enqueue(_event(event_id="batch-3")) is True
        await buffer.close()

        snapshot = buffer.snapshot()
        assert fake.execute_calls == 1
        assert len(fake.messages) == 3
        assert [stream for stream, _, _ in fake.messages] == ["audit-stream"] * 3
        assert all(
            options == {"maxlen": 25000, "approximate": True}
            for _, _, options in fake.messages
        )
        assert snapshot["acceptedSinceStart"] == 3
        assert snapshot["enqueuedSinceStart"] == 3
        assert snapshot["failedSinceStart"] == 0
        assert snapshot["memoryQueue"]["pending"] == 0
        assert snapshot["memoryQueue"]["serializedBytes"] == 0
        assert fake.closed is True

    asyncio.run(run())


def test_redis_batch_failure_is_retried_without_losing_events():
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=10,
            writer_count=1,
            redis_batch_size=2,
            redis_flush_interval_ms=5,
            shutdown_timeout_seconds=2,
        )
        fake = _FakeRedis(failures=1)
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(_event(event_id="retry-1")) is True
        assert await buffer.enqueue(_event(event_id="retry-2")) is True
        await buffer.close()

        snapshot = buffer.snapshot()
        assert fake.execute_calls == 2
        assert len(fake.messages) == 2
        assert snapshot["enqueuedSinceStart"] == 2
        assert snapshot["failedSinceStart"] == 0
        assert snapshot["redisWriteFailuresSinceStart"] == 1
        assert snapshot["redisRetryEventsSinceStart"] == 2
        assert snapshot["memoryQueue"]["pending"] == 0

    asyncio.run(run())


def test_buffer_can_restart_in_a_new_event_loop():
    buffer = RequestAuditBuffer(
        "redis://fake/0",
        "audit-stream",
        memory_queue_size=10,
        writer_count=1,
        redis_batch_size=1,
    )

    async def one_cycle(event_id: str):
        fake = _FakeRedis()
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(_event(event_id=event_id)) is True
        await buffer.close()
        assert len(fake.messages) == 1

    asyncio.run(one_cycle("loop-1"))
    asyncio.run(one_cycle("loop-2"))


def test_writer_collect_failure_is_visible_and_recovers():
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=10,
            writer_count=1,
            redis_batch_size=1,
        )
        fake = _FakeRedis()
        buffer._client = fake
        original = buffer._collect_batch
        calls = 0

        async def flaky_collect():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("wrong loop")
            return await original()

        buffer._collect_batch = flaky_collect
        await buffer.start()
        await asyncio.sleep(0.1)
        assert await buffer.enqueue(_event(event_id="recovered")) is True
        await buffer.close()
        assert len(fake.messages) == 1
        assert buffer.snapshot()["redisWriteFailuresSinceStart"] == 0

    asyncio.run(run())


def test_admin_diagnostics_include_stream_and_worker_health():
    async def run():
        buffer = RequestAuditBuffer("redis://fake/0", "audit-stream")
        fake = _FakeRedis()
        buffer._client = fake
        await buffer.start()
        buffer._available = False
        details = await buffer.diagnostics()
        assert details["available"] is True
        assert details["streamHealth"]["length"] == 0
        assert details["streamHealth"]["pending"] == 0
        assert details["streamHealth"]["consumerLag"] == 0
        assert details["streamHealth"]["workerHealthy"] is True
        await buffer.close()

    asyncio.run(run())


def test_bounded_memory_queue_overflow_is_visible_and_never_grows_unbounded():
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=2,
            writer_count=1,
            redis_batch_size=1,
            redis_flush_interval_ms=5,
            shutdown_timeout_seconds=1,
        )
        fake = _FakeRedis(blocked=True)
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(_event(event_id="held-by-writer")) is True
        await asyncio.wait_for(fake.execute_started.wait(), timeout=1)
        assert await buffer.enqueue(_event(event_id="queued-1")) is True
        assert await buffer.enqueue(_event(event_id="queued-2")) is True
        assert await buffer.enqueue(_event(event_id="overflow")) is False

        snapshot = buffer.snapshot()
        assert snapshot["acceptedSinceStart"] == 3
        assert snapshot["failedSinceStart"] == 1
        assert snapshot["overflowedSinceStart"] == 1
        assert snapshot["memoryQueue"]["waiting"] == 2
        assert snapshot["memoryQueue"]["inFlight"] == 1
        assert snapshot["memoryQueue"]["pending"] == 3
        assert snapshot["memoryQueue"]["utilizationPercent"] == 100.0

        fake.release_execute.set()
        await buffer.close()
        assert buffer.snapshot()["memoryQueue"]["pending"] == 0

    asyncio.run(run())


def test_byte_limit_rejects_new_event_before_process_memory_grows():
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=10,
            memory_queue_bytes=1024,
            writer_count=1,
            redis_batch_size=1,
        )
        fake = _FakeRedis(blocked=True)
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(_event(event_id="held")) is True
        await asyncio.wait_for(fake.execute_started.wait(), timeout=1)
        oversized = RequestAuditEvent(
            event_id="oversized",
            actor="admin",
            action="request_ok",
            payload=json.dumps({"body": "x" * 2000}),
            occurred_at=datetime.now(UTC).isoformat(),
        )
        assert await buffer.enqueue(oversized) is False
        snapshot = buffer.snapshot()
        assert snapshot["byteOverflowedSinceStart"] == 1
        assert snapshot["memoryQueue"]["serializedBytes"] <= 1024
        fake.release_execute.set()
        await buffer.close()

    asyncio.run(run())


def test_redis_unavailable_sends_security_event_to_sync_fallback():
    async def run():
        buffer = RequestAuditBuffer("redis://fake/0", "audit-stream")
        fake = _FakeRedis(blocked=True)
        buffer._client = fake
        await buffer.start()
        buffer._available = False

        assert await buffer.enqueue(_event(event_id="ok")) is True
        assert await buffer.enqueue(
            _event(event_id="forbidden", action="request_forbidden")
        ) is False
        snapshot = buffer.snapshot()
        assert snapshot["redisUnavailableFallbacksSinceStart"] == 1
        assert snapshot["failedSinceStart"] == 1

        fake.release_execute.set()
        await buffer.close()

    asyncio.run(run())


def test_unhealthy_worker_sends_security_event_to_sync_fallback():
    async def run():
        buffer = RequestAuditBuffer("redis://fake/0", "audit-stream")
        fake = _FakeRedis(blocked=True)
        buffer._client = fake
        await buffer.start()
        buffer._stream_health = {"workerHealthy": False, "consumerLag": 100}

        assert await buffer.enqueue(
            _event(event_id="worker-down-403", action="request_forbidden")
        ) is False
        assert await buffer.enqueue(_event(event_id="worker-down-ok")) is True
        assert buffer.snapshot()["redisUnavailableFallbacksSinceStart"] == 1

        fake.release_execute.set()
        await buffer.close()

    asyncio.run(run())


def test_shutdown_persists_security_event_still_in_memory(db_session, monkeypatch):
    import app.db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    async def run():
        buffer = RequestAuditBuffer(
            "redis://fake/0",
            "audit-stream",
            memory_queue_size=10,
            writer_count=1,
            redis_batch_size=1,
            shutdown_timeout_seconds=0.01,
        )
        fake = _FakeRedis(blocked=True)
        buffer._client = fake
        await buffer.start()
        assert await buffer.enqueue(
            _event(event_id="shutdown-403", action="request_forbidden")
        ) is True
        await asyncio.wait_for(fake.execute_started.wait(), timeout=1)
        await buffer.close()
        snapshot = buffer.snapshot()
        assert snapshot["shutdownFallbackPersistedSinceStart"] == 1
        assert snapshot["shutdownDroppedSinceStart"] == 0

    asyncio.run(run())
    db_session.expire_all()
    assert db_session.query(AuditLog).filter_by(event_id="shutdown-403").count() == 1


def test_shutdown_duplicate_does_not_rollback_other_security_events(db_session, monkeypatch):
    import app.db as db_module

    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    db_session.add(
        AuditLog(
            event_id="already-written",
            actor="admin",
            action="request_forbidden",
            payload="{}",
        )
    )
    db_session.commit()
    buffer = RequestAuditBuffer("redis://fake/0", "audit-stream")
    items = []
    for event_id in ("already-written", "new-security-event"):
        event = _event(event_id=event_id, action="request_forbidden")
        raw = event.to_json()
        from app.services.request_audit_queue import _BufferedEvent

        items.append(_BufferedEvent(event_id, raw, len(raw), time.monotonic()))

    inserted, secured = buffer._persist_shutdown_security_events(items)
    assert (inserted, secured) == (1, 2)
    db_session.expire_all()
    assert db_session.query(AuditLog).filter_by(event_id="already-written").count() == 1
    assert db_session.query(AuditLog).filter_by(event_id="new-security-event").count() == 1


def test_worker_batch_is_idempotent_and_renews_valid_session(db_session):
    now = datetime.now(UTC)
    original_expiry = now + timedelta(minutes=5)
    db_session.add(
        AppSession(
            session_id="sid-1",
            userid="admin",
            role="admin",
            expires_at=original_expiry,
        )
    )
    db_session.commit()

    event = _event(session_id="sid-1")
    assert persist_events(db_session, [event]) == 1
    db_session.commit()
    first_expiry = db_session.get(AppSession, "sid-1").expires_at
    assert _aware(first_expiry) > original_expiry

    assert persist_events(db_session, [event]) == 0
    db_session.commit()
    assert db_session.query(AuditLog).filter_by(event_id="evt-1").count() == 1
    assert _aware(db_session.get(AppSession, "sid-1").expires_at) > _aware(first_expiry)


def test_worker_does_not_renew_expired_session(db_session):
    now = datetime.now(UTC)
    original_expiry = now - timedelta(minutes=5)
    original_updated = now - timedelta(hours=1)
    db_session.add(
        AppSession(
            session_id="sid-expired",
            userid="admin",
            role="admin",
            updated_at=original_updated,
            expires_at=original_expiry,
        )
    )
    db_session.commit()

    assert persist_events(db_session, [_event(session_id="sid-expired")]) == 1
    db_session.commit()
    row = db_session.get(AppSession, "sid-expired")
    assert _aware(row.expires_at) == original_expiry
    assert _aware(row.updated_at) == original_updated


def test_enabled_buffer_queues_request_without_request_log_db_commit(
    client, db_session, monkeypatch
):
    buffer = FakeAuditBuffer()
    monkeypatch.setattr(main_module, "request_audit_buffer", buffer)

    response = client.get(
        "/api/v1/demo-context",
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert response.status_code == 200
    assert len(buffer.events) == 1
    assert buffer.events[0].actor == "admin"
    assert buffer.events[0].action == "request_ok"
    assert db_session.query(AuditLog).filter_by(action="request_ok").count() == 0


def test_queue_failure_sheds_ok_but_sync_fallback_keeps_error(client, db_session, monkeypatch):
    buffer = FakeAuditBuffer(succeeds=False)
    monkeypatch.setattr(main_module, "request_audit_buffer", buffer)
    headers = {"x-test-user": "admin", "x-test-role": "admin"}

    assert client.get("/api/v1/demo-context", headers=headers).status_code == 200
    assert db_session.query(AuditLog).filter_by(action="request_ok").count() == 0

    assert client.get("/api/v1/not-found", headers=headers).status_code == 404
    assert db_session.query(AuditLog).filter_by(action="request_error").count() == 1


def test_sync_audit_session_failure_does_not_replace_404(client, monkeypatch):
    assert client.post(
        "/api/v1/auth/login", json={"userid": "L0001", "role": "chief"}
    ).status_code == 200
    buffer = FakeAuditBuffer(succeeds=False)
    monkeypatch.setattr(main_module, "request_audit_buffer", buffer)
    original = main_module.get_session

    def fail_only_sync_fallback(session_id, *, db=None, touch=True):
        if db is not None and touch:
            raise TimeoutError("pool exhausted")
        return original(session_id, db=db, touch=touch)

    monkeypatch.setattr(main_module, "get_session", fail_only_sync_fallback)
    assert client.get("/api/v1/not-found").status_code == 404


def test_health_and_pool_monitor_do_not_generate_request_audit(client, monkeypatch):
    buffer = FakeAuditBuffer()
    monkeypatch.setattr(main_module, "request_audit_buffer", buffer)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    public_queue = health.json()["requestAuditQueue"]
    assert "lastError" not in public_queue
    assert "stream" not in public_queue
    assert "acceptedSinceStart" not in public_queue

    admin = client.get(
        "/api/v1/admin/connection-pool",
        headers={"x-test-user": "admin", "x-test-role": "admin"},
    )
    assert admin.status_code == 200
    assert "memoryQueue" in admin.json()["requestAuditQueue"]
    assert buffer.events == []
