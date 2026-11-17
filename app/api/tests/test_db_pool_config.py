from app.config import settings
from app.db import _driver_url, _engine_options


def test_postgres_engine_uses_explicit_100_connection_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "fdc_db_pool_size", 50)
    monkeypatch.setattr(settings, "fdc_db_max_overflow", 50)
    monkeypatch.setattr(settings, "fdc_db_pool_timeout_seconds", 5.0)

    options = _engine_options("postgresql://fdc:fdc@db:5432/fdc")

    assert options == {
        "pool_pre_ping": True,
        "pool_size": 50,
        "max_overflow": 50,
        "pool_timeout": 5.0,
        "pool_use_lifo": True,
        "pool_recycle": 1800,
    }


def test_sqlite_engine_does_not_receive_queue_pool_only_options():
    assert _engine_options("sqlite://") == {"pool_pre_ping": True}


def test_generic_postgres_url_uses_installed_driver_without_connecting():
    from sqlalchemy import create_engine

    url = _driver_url("postgresql://fdc:p%40ss@db:5432/fdc?application_name=report")
    engine = create_engine(url)
    try:
        assert engine.url.drivername == "postgresql+psycopg2"
        assert engine.dialect.driver == "psycopg2"
        assert engine.url.password == "p@ss"
        assert engine.url.query["application_name"] == "report"
    finally:
        engine.dispose()


def test_explicit_driver_and_sqlite_urls_are_preserved():
    assert _driver_url("postgresql+psycopg://fdc@db/fdc").drivername == "postgresql+psycopg"
    assert str(_driver_url("sqlite://")) == "sqlite://"
