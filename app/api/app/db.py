from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def _engine_options(database_url: str) -> dict[str, Any]:
    """Return backend-safe engine options.

    PostgreSQL uses an explicit bounded QueuePool for production bursts. SQLite
    tests intentionally keep their native pool because in-memory SQLite rejects
    QueuePool-only options such as ``max_overflow``.
    """
    options: dict[str, Any] = {"pool_pre_ping": True}
    if make_url(database_url).get_backend_name() == "postgresql":
        options.update(
            pool_size=settings.fdc_db_pool_size,
            max_overflow=settings.fdc_db_max_overflow,
            pool_timeout=settings.fdc_db_pool_timeout_seconds,
            pool_use_lifo=True,
            pool_recycle=1800,
        )
    return options


def _driver_url(database_url: str) -> URL:
    """Keep generic PostgreSQL URLs on the psycopg2 driver shipped in our image."""
    url = make_url(database_url)
    if url.drivername == "postgresql":
        return url.set(drivername="postgresql+psycopg2")
    return url


engine = create_engine(_driver_url(settings.database_url), **_engine_options(settings.database_url))


if engine.dialect.name == "postgresql":
    @event.listens_for(engine, "connect")
    def _set_postgres_timezone(dbapi_connection, _connection_record) -> None:
        """数据库会话统一使用上海时区，不依赖容器或服务端默认配置。"""
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'Asia/Shanghai'")
        finally:
            cursor.close()
            dbapi_connection.autocommit = previous_autocommit


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
