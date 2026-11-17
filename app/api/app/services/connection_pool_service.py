"""Runtime connection-pool diagnostics for the administrator monitor page.

The SQLAlchemy snapshot is process-local. PostgreSQL activity is server-wide,
which lets the UI show both the application's immediate pressure and the
database's remaining connection budget.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


POOL_WARNING_PERCENT = 70.0
POOL_CRITICAL_PERCENT = 90.0
DATABASE_WARNING_PERCENT = 75.0
DATABASE_CRITICAL_PERCENT = 90.0


def _method_int(target: object, name: str) -> int | None:
    method = getattr(target, name, None)
    if not callable(method):
        return None
    try:
        return int(method())
    except (TypeError, ValueError):
        return None


def _method_float(target: object, name: str) -> float | None:
    method = getattr(target, name, None)
    if not callable(method):
        return None
    try:
        return float(method())
    except (TypeError, ValueError):
        return None


def _percent(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(value * 100 / total, 1)


def _pool_snapshot(db: Session) -> dict[str, Any]:
    bind = db.get_bind()
    pool = bind.pool
    size = _method_int(pool, "size")
    checked_in = _method_int(pool, "checkedin")
    checked_out = _method_int(pool, "checkedout")
    raw_overflow = _method_int(pool, "overflow")
    timeout = _method_float(pool, "timeout")
    max_overflow_value = getattr(pool, "_max_overflow", None)
    try:
        max_overflow = int(max_overflow_value)
    except (TypeError, ValueError):
        max_overflow = None

    supported = all(value is not None for value in (size, checked_in, checked_out, max_overflow))
    if not supported:
        return {
            "supported": False,
            "poolClass": pool.__class__.__name__,
            "statusText": str(getattr(pool, "status", lambda: pool.__class__.__name__)()),
            "reason": "当前数据库驱动未使用可观测的 QueuePool",
        }

    assert size is not None
    assert checked_in is not None
    assert checked_out is not None
    assert max_overflow is not None
    max_capacity = max(0, size + max_overflow)
    current_overflow = max(0, raw_overflow or 0)
    established = max(0, checked_in + checked_out)
    return {
        "supported": True,
        "poolClass": pool.__class__.__name__,
        "poolSize": size,
        "maxOverflow": max_overflow,
        "maxCapacity": max_capacity,
        "checkedIn": checked_in,
        "checkedOut": checked_out,
        "currentOverflow": current_overflow,
        "establishedConnections": established,
        "utilizationPercent": _percent(checked_out, max_capacity),
        "timeoutSeconds": timeout,
        "statusText": str(pool.status()),
        # 鉴权通常已使用当前请求的 Session，因此 checkedOut 一般包含监控请求自身。
        "includesObserverRequest": True,
    }


def _database_snapshot(db: Session) -> dict[str, Any]:
    backend = db.get_bind().dialect.name
    if backend != "postgresql":
        return {
            "supported": False,
            "backend": backend,
            "reason": "PostgreSQL 会话指标仅在 PostgreSQL 环境可用",
        }

    try:
        # 监控查询不得在数据库异常时长时间占住连接或诊断线程。
        db.execute(text("SET LOCAL statement_timeout = '2s'"))
        row = db.execute(
            text(
                """
                SELECT
                    current_setting('max_connections')::int AS max_connections,
                    current_setting('superuser_reserved_connections')::int
                        AS reserved_connections,
                    (count(*) FILTER (WHERE backend_type = 'client backend'))::int
                        AS server_total,
                    (count(*) FILTER (WHERE datname = current_database()))::int
                        AS database_total,
                    (count(*) FILTER (
                        WHERE datname = current_database() AND state = 'active'
                    ))::int AS active,
                    (count(*) FILTER (
                        WHERE datname = current_database() AND state = 'idle'
                    ))::int AS idle,
                    (count(*) FILTER (
                        WHERE datname = current_database()
                            AND state LIKE 'idle in transaction%'
                    ))::int AS idle_in_transaction,
                    (count(*) FILTER (
                        WHERE datname = current_database()
                            AND state = 'active'
                            AND wait_event_type = 'Lock'
                    ))::int AS lock_waiting,
                    COALESCE(
                        max(EXTRACT(EPOCH FROM (clock_timestamp() - xact_start)))
                            FILTER (
                                WHERE datname = current_database()
                                    AND backend_type = 'client backend'
                                    AND xact_start IS NOT NULL
                            ),
                        0
                    )::float AS longest_transaction_seconds
                FROM pg_stat_activity
                """
            )
        ).mappings().one()
    except Exception as exc:  # noqa: BLE001 - monitor must still return the pool snapshot
        db.rollback()
        return {
            "supported": False,
            "backend": backend,
            "reason": f"读取 pg_stat_activity 失败：{exc.__class__.__name__}",
        }

    max_connections = int(row["max_connections"] or 0)
    reserved_connections = int(row["reserved_connections"] or 0)
    available_budget = max(1, max_connections - reserved_connections)
    server_total = int(row["server_total"] or 0)
    lock_waiting = int(row["lock_waiting"] or 0)
    return {
        "supported": True,
        "backend": backend,
        "maxConnections": max_connections,
        "reservedConnections": reserved_connections,
        "availableBudget": available_budget,
        "remainingConnections": max(0, available_budget - server_total),
        "serverTotal": server_total,
        "databaseTotal": int(row["database_total"] or 0),
        "active": int(row["active"] or 0),
        "idle": int(row["idle"] or 0),
        "idleInTransaction": int(row["idle_in_transaction"] or 0),
        # waiting 作为兼容字段保留，但其语义已收敛为真正的 PostgreSQL 锁争用，
        # 不再把 IO/WAL 等正常 wait_event 误报成连接池排队。
        "waiting": lock_waiting,
        "lockWaiting": lock_waiting,
        "longestTransactionSeconds": round(float(row["longest_transaction_seconds"] or 0), 1),
        "utilizationPercent": _percent(server_total, available_budget),
        "includesObserverRequest": True,
    }


def _overall_status(pool: dict[str, Any], database: dict[str, Any]) -> str:
    if not pool.get("supported") and not database.get("supported"):
        return "unavailable"

    pool_percent = float(pool.get("utilizationPercent") or 0)
    database_percent = float(database.get("utilizationPercent") or 0)
    if pool_percent >= POOL_CRITICAL_PERCENT or database_percent >= DATABASE_CRITICAL_PERCENT:
        return "critical"
    if (
        pool_percent >= POOL_WARNING_PERCENT
        or database_percent >= DATABASE_WARNING_PERCENT
        or int(database.get("lockWaiting") or 0) > 0
    ):
        return "warning"
    return "healthy"


def get_connection_pool_status(db: Session) -> dict[str, Any]:
    pool = _pool_snapshot(db)
    database = _database_snapshot(db)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "process": {
            "pid": os.getpid(),
            "databaseBackend": db.get_bind().dialect.name,
            "scope": "current_process",
        },
        "status": _overall_status(pool, database),
        "pool": pool,
        "database": database,
        "thresholds": {
            "poolWarningPercent": POOL_WARNING_PERCENT,
            "poolCriticalPercent": POOL_CRITICAL_PERCENT,
            "databaseWarningPercent": DATABASE_WARNING_PERCENT,
            "databaseCriticalPercent": DATABASE_CRITICAL_PERCENT,
        },
        "notes": [
            "SQLAlchemy 连接池指标仅代表处理本次请求的应用进程",
            "checkedOut 和 PostgreSQL active 通常包含当前监控请求自身",
            "PostgreSQL 锁等待仅统计 wait_event_type=Lock，不把 IO/WAL 等待误作连接排队",
            "数据库利用率按 max_connections 扣除超级用户预留连接后的可用额度计算",
            "本监控接口不写入请求审计日志，自动刷新不会产生 request_ok 记录",
        ],
    }
