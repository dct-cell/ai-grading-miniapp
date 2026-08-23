import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import get_ident

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from server import db as db_module
from server.db import create_session_factory
from server.models import base as base_module
from server.models.base import Base, TimestampMixin


def test_session_factory_executes_query() -> None:
    factory = create_session_factory("sqlite+pysqlite:///:memory:")
    with factory() as session:
        assert session.scalar(text("select 1")) == 1


def test_sqlite_engine_disposes_worker_connection_without_pool_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = create_session_factory("sqlite+pysqlite:///:memory:")
    engine = factory.kw["bind"]
    assert isinstance(engine, Engine)
    main_thread_id = get_ident()

    def execute_query() -> int:
        with factory() as session:
            assert session.scalar(text("select 1")) == 1
        return get_ident()

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker_thread_id = executor.submit(execute_query).result()

    assert worker_thread_id != main_thread_id
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="sqlalchemy.pool"):
        engine.dispose()

    pool_errors = [
        record
        for record in caplog.records
        if record.name.startswith("sqlalchemy.pool")
        and record.levelno >= logging.ERROR
    ]
    programming_errors = [
        record
        for record in pool_errors
        if record.exc_info is not None
        and isinstance(record.exc_info[1], sqlite3.ProgrammingError)
    ]
    assert not pool_errors, [
        (
            record.getMessage(),
            repr(record.exc_info[1]) if record.exc_info is not None else None,
        )
        for record in pool_errors
    ]
    assert not programming_errors


def test_non_sqlite_factory_omits_sqlite_connect_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel_engine = object()

    def fake_create_engine(database_url: str, **kwargs: object) -> object:
        captured["database_url"] = database_url
        captured["kwargs"] = kwargs
        return sentinel_engine

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)

    factory = create_session_factory("mysql+pymysql://user:pass@db/grader")

    assert factory.kw["bind"] is sentinel_engine
    assert captured == {
        "database_url": "mysql+pymysql://user:pass@db/grader",
        "kwargs": {"pool_pre_ping": True},
    }


def test_timestamp_mixin_preserves_utc_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_time = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    updated_time = datetime(2026, 1, 2, 4, 5, 6, tzinfo=timezone.utc)

    class Clock:
        current = initial_time

        @classmethod
        def now(cls, tz: timezone) -> datetime:
            assert tz is timezone.utc
            return cls.current

    monkeypatch.setattr(base_module, "datetime", Clock)

    server_tables_before = set(Base.metadata.tables)

    class LocalBase(DeclarativeBase):
        pass

    class Record(TimestampMixin, LocalBase):
        __tablename__ = "timestamp_records"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str] = mapped_column()

    factory = create_session_factory("sqlite+pysqlite:///:memory:")

    with factory() as session:
        LocalBase.metadata.create_all(session.get_bind())
        record = Record(name="created")
        session.add(record)
        session.flush()
        record_id = record.id
        session.commit()
        assert not inspect(record).expired

    assert record.name == "created"
    assert record.created_at == initial_time
    assert record.updated_at == initial_time

    with factory() as session:
        reloaded = session.get(Record, record_id)
        assert reloaded is not None
        assert reloaded.created_at.tzinfo is timezone.utc
        assert reloaded.updated_at.tzinfo is timezone.utc

        Clock.current = updated_time
        reloaded.name = "updated"
        session.commit()

        assert reloaded.created_at == initial_time
        assert reloaded.updated_at == updated_time

    with factory() as session:
        updated = session.get(Record, record_id)
        assert updated is not None
        assert updated.created_at == initial_time
        assert updated.updated_at == updated_time
        assert updated.created_at.tzinfo is timezone.utc
        assert updated.updated_at.tzinfo is timezone.utc

    assert set(Base.metadata.tables) == server_tables_before


def test_utc_datetime_normalizes_and_validates_values() -> None:
    from server.models.base import UTCDateTime

    column_type = UTCDateTime()
    sqlite_dialect = sqlite.dialect()
    source = datetime(
        2026,
        1,
        2,
        11,
        4,
        5,
        tzinfo=timezone(timedelta(hours=8)),
    )

    stored = column_type.process_bind_param(source, sqlite_dialect)
    assert stored == datetime(2026, 1, 2, 3, 4, 5)
    assert stored is not None
    assert stored.tzinfo is None

    restored = column_type.process_result_value(stored, sqlite_dialect)
    assert restored == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert restored is not None
    assert restored.tzinfo is timezone.utc
    assert column_type.process_result_value(source, sqlite_dialect) == restored

    assert column_type.process_bind_param(None, sqlite_dialect) is None
    assert column_type.process_result_value(None, sqlite_dialect) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        column_type.process_bind_param(datetime(2026, 1, 2), sqlite_dialect)

    assert str(column_type.compile(dialect=sqlite_dialect)) == "DATETIME"
    assert str(column_type.compile(dialect=mysql.dialect())) == "DATETIME"
