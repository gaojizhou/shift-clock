from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.models import AuditLog
from app.services.connection_pool_service import (
    _overall_status,
    get_connection_pool_status,
)


ADMIN = {"x-test-user": "admin", "x-test-role": "admin"}
HR = {"x-test-user": "HR01", "x-test-role": "hr"}


def test_connection_pool_monitor_is_admin_only_and_supports_sqlite(client):
    assert client.get("/api/v1/admin/connection-pool").status_code == 401
    assert client.get("/api/v1/admin/connection-pool", headers=HR).status_code == 403

    response = client.get("/api/v1/admin/connection-pool", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["process"]["scope"] == "current_process"
    assert body["process"]["databaseBackend"] == "sqlite"
    assert body["pool"]["poolClass"] == "StaticPool"
    assert body["pool"]["supported"] is False
    assert body["database"]["supported"] is False
    assert body["requestAuditQueue"]["enabled"] is False
    assert body["status"] == "unavailable"


def test_connection_pool_monitor_does_not_write_request_audit(client, db_session):
    before = db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.like("request_%"))
    )
    assert client.get("/api/v1/admin/connection-pool", headers=ADMIN).status_code == 200
    after = db_session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.like("request_%"))
    )
    assert after == before


def test_queue_pool_snapshot_reports_capacity_and_checked_out_connection():
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=2,
        max_overflow=1,
        pool_timeout=3,
    )
    held_connection = engine.connect()
    db = Session(engine)
    try:
        body = get_connection_pool_status(db)
    finally:
        db.close()
        held_connection.close()
        engine.dispose()

    pool = body["pool"]
    assert pool["supported"] is True
    assert pool["poolClass"] == "QueuePool"
    assert pool["poolSize"] == 2
    assert pool["maxOverflow"] == 1
    assert pool["maxCapacity"] == 3
    assert pool["checkedIn"] == 0
    assert pool["checkedOut"] == 1
    assert pool["currentOverflow"] == 0
    assert pool["establishedConnections"] == 1
    assert pool["utilizationPercent"] == 33.3
    assert pool["timeoutSeconds"] == 3.0
    assert "Current Checked out connections: 1" in pool["statusText"]
    assert pool["includesObserverRequest"] is True


def test_connection_pool_status_thresholds_include_waiting_and_capacity():
    assert _overall_status(
        {"supported": True, "utilizationPercent": 10},
        {"supported": True, "utilizationPercent": 20, "waiting": 0},
    ) == "healthy"
    assert _overall_status(
        {"supported": True, "utilizationPercent": 10},
        {"supported": True, "utilizationPercent": 20, "waiting": 1},
    ) == "healthy"
    assert _overall_status(
        {"supported": True, "utilizationPercent": 10},
        {"supported": True, "utilizationPercent": 20, "lockWaiting": 1},
    ) == "warning"
    assert _overall_status(
        {"supported": True, "utilizationPercent": 10},
        {"supported": True, "utilizationPercent": 20, "idleInTransaction": 1},
    ) == "healthy"
    assert _overall_status(
        {"supported": True, "utilizationPercent": 90},
        {"supported": True, "utilizationPercent": 20, "waiting": 0},
    ) == "critical"
