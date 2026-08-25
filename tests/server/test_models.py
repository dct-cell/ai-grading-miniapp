from __future__ import annotations

import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, String, UniqueConstraint, create_engine, event, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import CreateTable

import server.models
from server.models.base import Base, UTCDateTime


REQUIRED_TABLES = {
    "admin_sessions",
    "admin_users",
    "appeals",
    "audit_logs",
    "file_objects",
    "grading_jobs",
    "grading_rounds",
    "miniapp_sessions",
    "operational_settings",
    "orders",
    "payments",
    "price_rules",
    "quote_sessions",
    "refunds",
    "users",
    "worker_events",
    "workers",
}

EXPECTED_COLUMNS = {
    "admin_sessions": {
        "id",
        "admin_id",
        "token_hash",
        "expires_at",
        "last_seen_at",
        "revoked_at",
        "created_at",
        "updated_at",
    },
    "admin_users": {
        "id",
        "username",
        "password_hash",
        "disabled_at",
        "created_at",
        "updated_at",
    },
    "appeals": {"id", "order_id", "text", "created_at", "updated_at"},
    "audit_logs": {
        "id",
        "actor_type",
        "actor_id",
        "action",
        "target_type",
        "target_id",
        "details",
        "created_at",
        "updated_at",
    },
    "file_objects": {
        "id",
        "owner_user_id",
        "kind",
        "relative_path",
        "sha256",
        "size_bytes",
        "state",
        "expires_at",
        "created_at",
        "updated_at",
    },
    "grading_jobs": {
        "id",
        "order_id",
        "round_number",
        "state",
        "current_phase",
        "queued_at",
        "worker_id",
        "lease_version",
        "lease_expires_at",
        "ack_deadline",
        "attempt_count",
        "bundle_download_tokens",
        "created_at",
        "updated_at",
    },
    "grading_rounds": {
        "id",
        "order_id",
        "round_number",
        "service_tier",
        "grading_standard",
        "league_scope",
        "note",
        "result_json_file_id",
        "result_pdf_file_id",
        "delivered_at",
        "created_at",
        "updated_at",
    },
    "miniapp_sessions": {
        "id",
        "user_id",
        "token_hash",
        "expires_at",
        "revoked_at",
        "created_at",
        "updated_at",
    },
    "operational_settings": {
        "id",
        "name",
        "value",
        "created_at",
        "updated_at",
    },
    "orders": {
        "id",
        "quote_session_id",
        "state",
        "paid_amount_cents",
        "current_round_number",
        "acceptance_deadline",
        "downloads_revoked_at",
        "created_at",
        "updated_at",
    },
    "payments": {
        "id",
        "quote_session_id",
        "merchant_order_id",
        "prepay_id",
        "external_transaction_id",
        "amount_cents",
        "state",
        "created_at",
        "updated_at",
    },
    "price_rules": {
        "id",
        "service_tier",
        "cents_per_page",
        "effective_from",
        "retired_at",
        "created_at",
        "updated_at",
    },
    "quote_sessions": {
        "id",
        "owner_user_id",
        "source_file_id",
        "reference_file_id",
        "price_rule_id",
        "service_tier",
        "grading_standard",
        "league_scope",
        "note",
        "page_count",
        "quoted_amount_cents",
        "expires_at",
        "consumed_at",
        "created_at",
        "updated_at",
    },
    "refunds": {
        "id",
        "payment_id",
        "external_refund_id",
        "source",
        "state",
        "amount_cents",
        "created_at",
        "updated_at",
    },
    "users": {"id", "openid", "public_id", "created_at", "updated_at"},
    "worker_events": {
        "id",
        "worker_id",
        "job_id",
        "event_type",
        "details",
        "created_at",
        "updated_at",
    },
    "workers": {
        "worker_id",
        "installation_id",
        "device_name",
        "platform",
        "architecture",
        "worker_version",
        "codex_version",
        "tex_version",
        "capabilities",
        "status",
        "current_job_id",
        "last_heartbeat_at",
        "created_at",
        "updated_at",
    },
}

EXPECTED_UNIQUES = {
    "users": {("openid",), ("public_id",)},
    "miniapp_sessions": {("token_hash",)},
    "orders": {("quote_session_id",)},
    "grading_rounds": {("order_id", "round_number")},
    "appeals": {("order_id",)},
    "payments": {
        ("merchant_order_id",),
        ("prepay_id",),
        ("external_transaction_id",),
    },
    "refunds": {("external_refund_id",)},
    "grading_jobs": {("order_id", "round_number")},
    "admin_users": {("username",)},
    "admin_sessions": {("token_hash",)},
    "operational_settings": {("name",)},
    "workers": {("installation_id",)},
    "file_objects": {("relative_path",)},
}

EXPECTED_CHECKS = {
    "price_rules": {"cents_per_page > 0"},
    "quote_sessions": {"page_count > 0"},
    "grading_rounds": {"round_number IN (1, 2)"},
    "refunds": {"source IN ('user', 'admin_technical')"},
}

EXPECTED_NULLABLE_COLUMNS = {
    "admin_sessions": {"revoked_at"},
    "admin_users": {"disabled_at"},
    "appeals": set(),
    "audit_logs": set(),
    "file_objects": set(),
    "grading_jobs": {
        "worker_id",
        "current_phase",
        "lease_expires_at",
        "ack_deadline",
        "bundle_download_tokens",
    },
    "grading_rounds": {
        "league_scope",
        "result_json_file_id",
        "result_pdf_file_id",
        "delivered_at",
    },
    "miniapp_sessions": {"revoked_at"},
    "operational_settings": set(),
    "orders": {"acceptance_deadline", "downloads_revoked_at"},
    "payments": {"external_transaction_id"},
    "price_rules": {"retired_at"},
    "quote_sessions": {"reference_file_id", "league_scope", "consumed_at"},
    "refunds": set(),
    "users": set(),
    "worker_events": set(),
    "workers": {"codex_version", "tex_version", "current_job_id"},
}

EXPECTED_FOREIGN_KEYS = {
    ("admin_sessions", "admin_id", "admin_users", "id"),
    ("miniapp_sessions", "user_id", "users", "id"),
    ("file_objects", "owner_user_id", "users", "id"),
    ("quote_sessions", "owner_user_id", "users", "id"),
    ("quote_sessions", "source_file_id", "file_objects", "id"),
    ("quote_sessions", "reference_file_id", "file_objects", "id"),
    ("quote_sessions", "price_rule_id", "price_rules", "id"),
    ("orders", "quote_session_id", "quote_sessions", "id"),
    ("grading_rounds", "order_id", "orders", "id"),
    ("grading_rounds", "result_json_file_id", "file_objects", "id"),
    ("grading_rounds", "result_pdf_file_id", "file_objects", "id"),
    ("appeals", "order_id", "orders", "id"),
    ("payments", "quote_session_id", "quote_sessions", "id"),
    ("refunds", "payment_id", "payments", "id"),
    ("grading_jobs", "order_id", "orders", "id"),
    ("grading_jobs", "worker_id", "workers", "worker_id"),
    ("worker_events", "worker_id", "workers", "worker_id"),
    ("worker_events", "job_id", "grading_jobs", "id"),
}

EXPECTED_DATETIME_COLUMNS = {
    "admin_sessions": {
        "expires_at",
        "last_seen_at",
        "revoked_at",
        "created_at",
        "updated_at",
    },
    "admin_users": {"disabled_at", "created_at", "updated_at"},
    "appeals": {"created_at", "updated_at"},
    "audit_logs": {"created_at", "updated_at"},
    "file_objects": {"expires_at", "created_at", "updated_at"},
    "grading_jobs": {
        "queued_at",
        "lease_expires_at",
        "ack_deadline",
        "created_at",
        "updated_at",
    },
    "grading_rounds": {"delivered_at", "created_at", "updated_at"},
    "miniapp_sessions": {"expires_at", "revoked_at", "created_at", "updated_at"},
    "operational_settings": {"created_at", "updated_at"},
    "orders": {
        "acceptance_deadline",
        "downloads_revoked_at",
        "created_at",
        "updated_at",
    },
    "payments": {"created_at", "updated_at"},
    "price_rules": {"effective_from", "retired_at", "created_at", "updated_at"},
    "quote_sessions": {"expires_at", "consumed_at", "created_at", "updated_at"},
    "refunds": {"created_at", "updated_at"},
    "users": {"created_at", "updated_at"},
    "worker_events": {"created_at", "updated_at"},
    "workers": {"last_heartbeat_at", "created_at", "updated_at"},
}


def _table(name: str):
    assert name in Base.metadata.tables, f"model table {name!r} is not registered"
    return Base.metadata.tables[name]


def _uuid(number: int) -> str:
    return str(UUID(int=number))


def _valid_row(table_name: str, salt: int = 1) -> dict[str, object]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=salt)
    identifiers = {
        "user": _uuid(1000 + salt),
        "file": _uuid(2000 + salt),
        "price_rule": _uuid(2100 + salt),
        "quote": _uuid(3000 + salt),
        "order": _uuid(4000 + salt),
        "payment": _uuid(5000 + salt),
        "worker": _uuid(6000 + salt),
        "job": _uuid(7000 + salt),
    }
    rows: dict[str, dict[str, object]] = {
        "users": {
            "id": identifiers["user"],
            "openid": f"openid-{salt}",
            "public_id": f"public-{salt}",
        },
        "miniapp_sessions": {
            "id": _uuid(1100 + salt),
            "user_id": identifiers["user"],
            "token_hash": f"token-hash-{salt}",
            "expires_at": now,
            "revoked_at": None,
        },
        "file_objects": {
            "id": identifiers["file"],
            "owner_user_id": identifiers["user"],
            "kind": "source",
            "relative_path": f"uploads/{salt}.pdf",
            "sha256": f"{salt:064x}",
            "size_bytes": salt,
            "state": "stored",
            "expires_at": now,
        },
        "price_rules": {
            "id": identifiers["price_rule"],
            "service_tier": "annotated_review",
            "cents_per_page": 100 + salt,
            "effective_from": now,
            "retired_at": None,
        },
        "quote_sessions": {
            "id": identifiers["quote"],
            "owner_user_id": identifiers["user"],
            "source_file_id": identifiers["file"],
            "reference_file_id": None,
            "price_rule_id": identifiers["price_rule"],
            "service_tier": "annotated_review",
            "grading_standard": "imo",
            "league_scope": None,
            "note": "",
            "page_count": 1,
            "quoted_amount_cents": 100 + salt,
            "expires_at": now,
            "consumed_at": None,
        },
        "orders": {
            "id": identifiers["order"],
            "quote_session_id": identifiers["quote"],
            "state": "awaiting_payment",
            "paid_amount_cents": 0,
            "current_round_number": 1,
            "acceptance_deadline": None,
            "downloads_revoked_at": None,
        },
        "grading_rounds": {
            "id": _uuid(4100 + salt),
            "order_id": identifiers["order"],
            "round_number": 1,
            "service_tier": "annotated_review",
            "grading_standard": "imo",
            "league_scope": None,
            "note": "note",
            "result_json_file_id": None,
            "result_pdf_file_id": None,
            "delivered_at": None,
        },
        "appeals": {
            "id": _uuid(4200 + salt),
            "order_id": identifiers["order"],
            "text": f"appeal-{salt}",
        },
        "payments": {
            "id": identifiers["payment"],
            "quote_session_id": identifiers["quote"],
            "merchant_order_id": f"merchant-{salt}",
            "prepay_id": f"prepay-{salt}",
            "external_transaction_id": f"transaction-{salt}",
            "amount_cents": 100 + salt,
            "state": "pending",
        },
        "refunds": {
            "id": _uuid(5100 + salt),
            "payment_id": identifiers["payment"],
            "external_refund_id": f"refund-{salt}",
            "source": "user",
            "state": "pending",
            "amount_cents": 100 + salt,
        },
        "workers": {
            "worker_id": identifiers["worker"],
            "installation_id": f"install-{salt}",
            "device_name": f"device-{salt}",
            "platform": "linux",
            "architecture": "x86_64",
            "worker_version": f"1.0.{salt}",
            "codex_version": None,
            "tex_version": None,
            "capabilities": {"xelatex": True},
            "status": "online",
            "current_job_id": None,
            "last_heartbeat_at": now,
        },
        "grading_jobs": {
            "id": identifiers["job"],
            "order_id": identifiers["order"],
            "round_number": 1,
            "state": "queued",
            "queued_at": now,
            "worker_id": identifiers["worker"],
            "lease_version": 0,
            "lease_expires_at": None,
            "ack_deadline": None,
            "attempt_count": 0,
            "bundle_download_tokens": None,
        },
        "worker_events": {
            "id": _uuid(7100 + salt),
            "worker_id": identifiers["worker"],
            "job_id": identifiers["job"],
            "event_type": "heartbeat",
            "details": {"salt": salt},
        },
        "admin_users": {
            "id": _uuid(8000 + salt),
            "username": f"admin-{salt}",
            "password_hash": f"hash-{salt}",
            "disabled_at": None,
        },
        "admin_sessions": {
            "id": _uuid(8200 + salt),
            "admin_id": _uuid(8000 + salt),
            "token_hash": f"token-hash-{salt}",
            "expires_at": now + timedelta(hours=12),
            "last_seen_at": now,
            "revoked_at": None,
        },
        "operational_settings": {
            "id": _uuid(8300 + salt),
            "name": f"setting-{salt}",
            "value": str(salt),
        },
        "audit_logs": {
            "id": _uuid(8100 + salt),
            "actor_type": "admin",
            "actor_id": _uuid(8000 + salt),
            "action": "created",
            "target_type": "order",
            "target_id": identifiers["order"],
            "details": {"salt": salt},
        },
    }
    return rows[table_name]


def test_initial_schema_contains_required_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    assert set(inspect(engine).get_table_names()) == REQUIRED_TABLES


def test_models_expose_only_the_required_contract_columns() -> None:
    assert {name: set(_table(name).columns.keys()) for name in REQUIRED_TABLES} == (
        EXPECTED_COLUMNS
    )


def test_all_primary_keys_are_portable_uuid_strings_with_python_defaults() -> None:
    for table_name in REQUIRED_TABLES:
        table = _table(table_name)
        primary_key = list(table.primary_key.columns)
        assert len(primary_key) == 1
        column = primary_key[0]
        assert isinstance(column.type, String)
        assert column.type.length is not None
        assert column.default is not None
        generated = column.default.arg(None)
        assert isinstance(generated, str)
        assert str(UUID(generated)) == generated


def test_all_string_columns_have_explicit_lengths() -> None:
    for table_name in REQUIRED_TABLES:
        for column in _table(table_name).columns:
            if type(column.type) is String:
                assert column.type.length is not None, f"{table_name}.{column.name}"


def test_money_columns_use_integer_cents() -> None:
    money_columns = {
        ("price_rules", "cents_per_page"),
        ("quote_sessions", "quoted_amount_cents"),
        ("orders", "paid_amount_cents"),
        ("payments", "amount_cents"),
        ("refunds", "amount_cents"),
    }
    for table_name, column_name in money_columns:
        assert _table(table_name).c[column_name].type.python_type is int


def test_nullability_matches_the_complete_contract() -> None:
    actual = {
        table_name: {
            column.name for column in _table(table_name).columns if column.nullable
        }
        for table_name in REQUIRED_TABLES
    }
    assert actual == EXPECTED_NULLABLE_COLUMNS


def test_unique_constraints_match_the_contract() -> None:
    for table_name in REQUIRED_TABLES:
        actual = {
            tuple(column.name for column in constraint.columns)
            for constraint in _table(table_name).constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert actual == EXPECTED_UNIQUES.get(table_name, set())


@pytest.mark.parametrize(
    ("table_name", "column_names"),
    [
        ("users", ("openid",)),
        ("users", ("public_id",)),
        ("miniapp_sessions", ("token_hash",)),
        ("orders", ("quote_session_id",)),
        ("grading_rounds", ("order_id", "round_number")),
        ("appeals", ("order_id",)),
        ("payments", ("merchant_order_id",)),
        ("payments", ("prepay_id",)),
        ("payments", ("external_transaction_id",)),
        ("refunds", ("external_refund_id",)),
        ("grading_jobs", ("order_id", "round_number")),
        ("admin_users", ("username",)),
        ("workers", ("installation_id",)),
        ("file_objects", ("relative_path",)),
    ],
)
def test_unique_constraints_are_enforced_by_sqlite(
    table_name: str,
    column_names: tuple[str, ...],
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    table = _table(table_name)
    first = _valid_row(table_name, 1)
    second = _valid_row(table_name, 2)
    second.update({name: first[name] for name in column_names})

    with engine.begin() as connection:
        connection.execute(table.insert().values(**first))
        with pytest.raises(IntegrityError):
            connection.execute(table.insert().values(**second))


def test_check_constraints_match_the_contract() -> None:
    for table_name in REQUIRED_TABLES:
        actual = {
            " ".join(str(constraint.sqltext).split())
            for constraint in _table(table_name).constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert actual == EXPECTED_CHECKS.get(table_name, set())


@pytest.mark.parametrize(
    ("table_name", "column_name", "invalid_value"),
    [
        ("price_rules", "cents_per_page", 0),
        ("price_rules", "cents_per_page", -1),
        ("quote_sessions", "page_count", 0),
        ("quote_sessions", "page_count", -1),
        ("grading_rounds", "round_number", 0),
        ("grading_rounds", "round_number", 3),
        ("refunds", "source", "operator"),
    ],
)
def test_check_constraints_are_enforced_by_sqlite(
    table_name: str,
    column_name: str,
    invalid_value: object,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    values = _valid_row(table_name)
    values[column_name] = invalid_value

    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(_table(table_name).insert().values(**values))


@pytest.mark.parametrize(
    ("table_name", "column_name", "valid_value"),
    [
        ("price_rules", "cents_per_page", 1),
        ("quote_sessions", "page_count", 1),
        ("grading_rounds", "round_number", 1),
        ("grading_rounds", "round_number", 2),
        ("refunds", "source", "user"),
        ("refunds", "source", "admin_technical"),
    ],
)
def test_check_constraint_boundary_values_are_accepted_by_sqlite(
    table_name: str,
    column_name: str,
    valid_value: object,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    values = _valid_row(table_name)
    values[column_name] = valid_value

    with engine.begin() as connection:
        connection.execute(_table(table_name).insert().values(**values))


def test_session_expiration_is_indexed() -> None:
    indexed_column_sets = {
        tuple(column.name for column in index.columns)
        for index in _table("miniapp_sessions").indexes
    }
    assert indexed_column_sets == {("expires_at",)}


def test_the_queue_claim_path_is_indexed() -> None:
    """Without this index MySQL filesorts and FOR UPDATE SKIP LOCKED returns nothing.

    Verified against MySQL 8.4: a full scan plus filesort makes the claim query
    yield no row at all once another transaction holds any candidate, so two
    Workers polling at once both come away empty while jobs sit queued. An index
    matching the WHERE plus ORDER BY keeps the claim a ranged index scan, which
    is what lets SKIP LOCKED hand out the next unlocked row.
    """
    indexed_column_sets = {
        tuple(column.name for column in index.columns)
        for index in _table("grading_jobs").indexes
    }
    assert ("state", "queued_at", "id") in indexed_column_sets


def test_foreign_keys_match_the_relational_ownership_contract() -> None:
    actual = {
        (
            table.name,
            foreign_key.parent.name,
            foreign_key.column.table.name,
            foreign_key.column.name,
        )
        for table in Base.metadata.tables.values()
        for foreign_key in table.foreign_keys
    }
    assert actual == EXPECTED_FOREIGN_KEYS


@pytest.mark.parametrize("foreign_key_contract", sorted(EXPECTED_FOREIGN_KEYS))
def test_each_foreign_key_is_enforced_by_sqlite(
    foreign_key_contract: tuple[str, str, str, str],
) -> None:
    table_name, column_name, _, _ = foreign_key_contract
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    target = _valid_row(table_name, 1)
    target[column_name] = _uuid(999_999)
    inserted: set[tuple[str, object]] = set()

    def seed_parent(connection, parent_table_name: str, primary_key_value: object) -> None:
        key = (parent_table_name, primary_key_value)
        if key in inserted:
            return
        parent_table = _table(parent_table_name)
        parent_values = _valid_row(parent_table_name, 1)
        parent_values[list(parent_table.primary_key.columns)[0].name] = primary_key_value
        for foreign_key in parent_table.foreign_keys:
            value = parent_values[foreign_key.parent.name]
            if value is not None:
                seed_parent(connection, foreign_key.column.table.name, value)
        connection.execute(parent_table.insert().values(**parent_values))
        inserted.add(key)

    with engine.begin() as connection:
        for foreign_key in _table(table_name).foreign_keys:
            if foreign_key.parent.name == column_name:
                continue
            value = target[foreign_key.parent.name]
            if value is not None:
                seed_parent(connection, foreign_key.column.table.name, value)
        with pytest.raises(IntegrityError):
            connection.execute(_table(table_name).insert().values(**target))


def test_all_model_datetime_columns_use_the_utc_type() -> None:
    actual = {
        table_name: {
            column.name
            for column in _table(table_name).columns
            if isinstance(column.type, UTCDateTime)
        }
        for table_name in REQUIRED_TABLES
    }
    assert actual == EXPECTED_DATETIME_COLUMNS


def test_model_datetime_values_round_trip_as_utc_and_reject_naive_values() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    table = _table("admin_users")
    aware = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))

    with engine.begin() as connection:
        aware_values = _valid_row("admin_users")
        aware_values["disabled_at"] = aware
        connection.execute(table.insert().values(**aware_values))
        stored = connection.execute(table.select()).mappings().one()
        assert stored["disabled_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)

        with pytest.raises(StatementError, match="timezone-aware"):
            naive_values = _valid_row("admin_users", 2)
            naive_values["disabled_at"] = datetime(2026, 1, 1)
            connection.execute(table.insert().values(**naive_values))


def test_every_table_compiles_for_mysql() -> None:
    dialect = mysql.dialect()
    for table_name in REQUIRED_TABLES:
        ddl = str(CreateTable(_table(table_name)).compile(dialect=dialect))
        assert f"CREATE TABLE {table_name}" in ddl


def _schema_signature(inspector, table_name: str) -> dict[str, object]:
    return {
        "columns": {
            column["name"]: (
                column["type"].compile(),
                column["nullable"],
            )
            for column in inspector.get_columns(table_name)
        },
        "primary_key": tuple(inspector.get_pk_constraint(table_name)["constrained_columns"]),
        "uniques": {
            tuple(unique["column_names"])
            for unique in inspector.get_unique_constraints(table_name)
        },
        "foreign_keys": {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        },
        "checks": {
            " ".join(check["sqltext"].split())
            for check in inspector.get_check_constraints(table_name)
        },
        "indexes": {
            tuple(index["column_names"])
            for index in inspector.get_indexes(table_name)
            if not index["unique"]
        },
    }


def test_alembic_upgrade_matches_models_and_downgrade_removes_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "migration%25.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("GRADER_DATABASE_URL", database_url)
    config = Config("alembic.ini")

    model_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(model_engine)
    migration_engine = create_engine(database_url)

    command.upgrade(config, "head")
    migration_inspector = inspect(migration_engine)
    assert set(migration_inspector.get_table_names()) - {"alembic_version"} == REQUIRED_TABLES
    model_inspector = inspect(model_engine)
    for table_name in REQUIRED_TABLES:
        assert _schema_signature(migration_inspector, table_name) == _schema_signature(
            model_inspector,
            table_name,
        )

    command.downgrade(config, "base")
    assert set(inspect(migration_engine).get_table_names()) - {"alembic_version"} == set()
    migration_engine.dispose()
    model_engine.dispose()
    assert os.environ["GRADER_DATABASE_URL"] == database_url


def test_alembic_migration_compiles_complete_mysql_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GRADER_DATABASE_URL",
        "mysql+pymysql://grader:placeholder@127.0.0.1:3306/grader",
    )

    upgrade_output = io.StringIO()
    command.upgrade(
        Config("alembic.ini", output_buffer=upgrade_output),
        "head",
        sql=True,
    )
    upgrade_sql = upgrade_output.getvalue()
    created_tables = re.findall(r"\bCREATE TABLE ([a-z_]+)", upgrade_sql)
    assert created_tables == [
        "alembic_version",
        "users",
        "admin_users",
        "workers",
        "price_rules",
        "audit_logs",
        "file_objects",
        "miniapp_sessions",
        "quote_sessions",
        "orders",
        "payments",
        "grading_rounds",
        "appeals",
        "refunds",
        "grading_jobs",
        "worker_events",
        # Phase 07 appends the Admin session store (0005) and the editable
        # operational settings (0006).
        "admin_sessions",
        "operational_settings",
    ]
    normalized_upgrade_sql = " ".join(upgrade_sql.split())
    assert "CHECK (cents_per_page > 0)" in normalized_upgrade_sql
    assert "CHECK (page_count > 0)" in normalized_upgrade_sql
    assert "CHECK (round_number IN (1, 2))" in normalized_upgrade_sql
    assert "CHECK (source IN ('user', 'admin_technical'))" in normalized_upgrade_sql
    assert "FOREIGN KEY(owner_user_id) REFERENCES users (id)" in normalized_upgrade_sql
    assert "FOREIGN KEY(job_id) REFERENCES grading_jobs (id)" in normalized_upgrade_sql
    assert (
        "CREATE INDEX ix_miniapp_sessions_expires_at "
        "ON miniapp_sessions (expires_at)"
    ) in normalized_upgrade_sql
    assert "PRAGMA" not in upgrade_sql
    assert "AUTOINCREMENT" not in upgrade_sql

    downgrade_output = io.StringIO()
    command.downgrade(
        Config("alembic.ini", output_buffer=downgrade_output),
        "head:base",
        sql=True,
    )
    downgrade_sql = downgrade_output.getvalue()
    dropped_tables = re.findall(r"\bDROP TABLE ([a-z_]+)", downgrade_sql)
    assert dropped_tables == [
        # Migrations downgrade newest first, so 0006 drops before 0005.
        "operational_settings",
        "admin_sessions",
        "worker_events",
        "grading_jobs",
        "refunds",
        "appeals",
        "grading_rounds",
        "payments",
        "orders",
        "quote_sessions",
        "miniapp_sessions",
        "file_objects",
        "audit_logs",
        "price_rules",
        "workers",
        "admin_users",
        "users",
    ]
    assert "DROP INDEX ix_miniapp_sessions_expires_at ON miniapp_sessions" in (
        " ".join(downgrade_sql.split())
    )
    assert "PRAGMA" not in downgrade_sql
