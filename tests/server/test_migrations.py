from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from server.models.base import Base


PHASE_02_QUOTE_COLUMNS = {
    "owner_user_id",
    "price_rule_id",
    "grading_standard",
    "note",
}

PHASE_03_WORKER_COLUMNS = {
    "installation_id",
    "device_name",
    "architecture",
    "worker_version",
    "codex_version",
    "tex_version",
    "capabilities",
    "current_job_id",
}


def _config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("GRADER_DATABASE_URL", database_url)
    return Config("alembic.ini")


def test_empty_database_upgrades_directly_to_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/fresh.sqlite3"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    columns = {column["name"] for column in inspect(engine).get_columns("quote_sessions")}
    assert PHASE_02_QUOTE_COLUMNS <= columns
    worker_columns = {
        column["name"] for column in inspect(engine).get_columns("workers")
    }
    job_columns = {
        column["name"] for column in inspect(engine).get_columns("grading_jobs")
    }
    assert PHASE_03_WORKER_COLUMNS <= worker_columns
    assert "current_phase" in job_columns
    assert "version" not in worker_columns
    engine.dispose()


def test_phase_02_database_upgrades_from_0002_to_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployed Phase 02 database must reach the Worker schema in place."""
    database_url = f"sqlite+pysqlite:///{tmp_path}/phase02.sqlite3"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0002")
    engine = create_engine(database_url)
    before = {column["name"] for column in inspect(engine).get_columns("workers")}
    assert PHASE_03_WORKER_COLUMNS.isdisjoint(before)
    assert "version" in before

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, openid, public_id, created_at, updated_at)"
                " VALUES ('user-1', 'fake:test-1', 'u-00000001',"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    command.upgrade(config, "head")

    inspector = inspect(engine)
    after = {column["name"] for column in inspector.get_columns("workers")}
    assert PHASE_03_WORKER_COLUMNS <= after
    assert "version" not in after
    nullable = {
        column["name"] for column in inspector.get_columns("workers") if column["nullable"]
    }
    assert nullable == {"codex_version", "tex_version", "current_job_id"}
    assert {
        tuple(unique["column_names"])
        for unique in inspector.get_unique_constraints("workers")
    } == {("installation_id",)}
    assert inspector.get_foreign_keys("workers") == []
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 1
    engine.dispose()


def test_downgrade_from_head_restores_the_phase_02_worker_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/worker-rollback.sqlite3"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "head")

    command.downgrade(config, "0002")

    engine = create_engine(database_url)
    columns = {column["name"] for column in inspect(engine).get_columns("workers")}
    assert PHASE_03_WORKER_COLUMNS.isdisjoint(columns)
    assert "version" in columns
    engine.dispose()


def test_phase_03_migration_compiles_for_mysql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GRADER_DATABASE_URL",
        "mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )
    output = io.StringIO()

    # Bounded at 0003 rather than head: the point of this test is that the
    # Phase 03 migration only ALTERs existing tables. Later phases legitimately
    # create new ones — 0005 adds admin_sessions — which would defeat the
    # "no CREATE TABLE" assertion below without saying anything about Phase 03.
    command.upgrade(Config("alembic.ini", output_buffer=output), "0002:0003", sql=True)

    sql = " ".join(output.getvalue().split())
    assert "ALTER TABLE workers ADD COLUMN installation_id VARCHAR(64) NOT NULL" in sql
    assert "ALTER TABLE workers ADD COLUMN current_job_id VARCHAR(36)" in sql
    assert "ADD CONSTRAINT uq_workers_installation_id UNIQUE (installation_id)" in sql
    assert "ALTER TABLE workers DROP COLUMN version" in sql
    # A database-level foreign key here would form a cycle with
    # grading_jobs.worker_id and break ordered create/drop on MySQL.
    assert "REFERENCES grading_jobs" not in sql
    assert "PRAGMA" not in sql
    assert re.search(r"\bCREATE TABLE\b", sql) is None


def test_phase_01_database_upgrades_from_0001_to_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/phase01.sqlite3"
    config = _config(database_url, monkeypatch)

    command.upgrade(config, "0001")
    engine = create_engine(database_url)
    before = {column["name"] for column in inspect(engine).get_columns("quote_sessions")}
    assert PHASE_02_QUOTE_COLUMNS.isdisjoint(before)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, openid, public_id, created_at, updated_at)"
                " VALUES ('user-1', 'fake:test-1', 'u-00000001',"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    command.upgrade(config, "head")

    inspector = inspect(engine)
    after = {column["name"] for column in inspector.get_columns("quote_sessions")}
    assert PHASE_02_QUOTE_COLUMNS <= after
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar() == 1
    engine.dispose()


def test_head_schema_matches_the_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/head.sqlite3"
    command.upgrade(_config(database_url, monkeypatch), "head")

    migration_engine = create_engine(database_url)
    model_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(model_engine)
    migrated = inspect(migration_engine)
    modeled = inspect(model_engine)

    def signature(inspector, table_name: str) -> dict[str, object]:
        return {
            "columns": {
                column["name"]: (column["type"].compile(), column["nullable"])
                for column in inspector.get_columns(table_name)
            },
            "foreign_keys": {
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                )
                for foreign_key in inspector.get_foreign_keys(table_name)
            },
        }

    for table_name in sorted(set(modeled.get_table_names())):
        assert signature(migrated, table_name) == signature(modeled, table_name)

    migration_engine.dispose()
    model_engine.dispose()


def test_downgrade_from_head_restores_the_phase_01_quote_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path}/rollback.sqlite3"
    config = _config(database_url, monkeypatch)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    upgraded = {column["name"] for column in inspect(engine).get_columns("quote_sessions")}
    assert PHASE_02_QUOTE_COLUMNS <= upgraded

    command.downgrade(config, "0001")

    columns = {column["name"] for column in inspect(engine).get_columns("quote_sessions")}
    assert PHASE_02_QUOTE_COLUMNS.isdisjoint(columns)
    engine.dispose()


def test_phase_02_migration_compiles_for_mysql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GRADER_DATABASE_URL",
        "mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )
    output = io.StringIO()

    # Bounded at 0002 for the same reason as the Phase 03 case above: this
    # asserts Phase 02 only ALTERs, and 0005 legitimately creates a table.
    command.upgrade(Config("alembic.ini", output_buffer=output), "0001:0002", sql=True)

    sql = " ".join(output.getvalue().split())
    assert "ALTER TABLE quote_sessions ADD COLUMN owner_user_id VARCHAR(36) NOT NULL" in sql
    assert "ALTER TABLE quote_sessions ADD COLUMN price_rule_id VARCHAR(36) NOT NULL" in sql
    assert "ADD COLUMN grading_standard TEXT NOT NULL" in sql
    assert "ADD COLUMN note TEXT NOT NULL" in sql
    assert (
        "FOREIGN KEY(owner_user_id) REFERENCES users (id)" in sql
        and "FOREIGN KEY(price_rule_id) REFERENCES price_rules (id)" in sql
    )
    assert "PRAGMA" not in sql
    assert re.search(r"\bCREATE TABLE\b", sql) is None


def test_initial_migration_file_is_unchanged_by_phase_02() -> None:
    initial = Path("server/migrations/versions/0001_initial_schema.py").read_text(
        encoding="utf-8"
    )

    assert 'revision: str = "0001"' in initial
    assert "down_revision: str | None = None" in initial
    assert "owner_user_id" in initial.split('"quote_sessions"')[0]
    assert "price_rule_id" not in initial
