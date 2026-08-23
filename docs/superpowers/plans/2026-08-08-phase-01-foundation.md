# Phase 01 Server Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the FastAPI/MySQL foundation, staging/production separation, initial relational schema, and explicit domain state machines while leaving the legacy local grader operational.

**Architecture:** A new server/ package lives beside app/. SQLAlchemy models are grouped by business responsibility, Alembic owns schema changes, and state transitions are pure functions independent of FastAPI and database sessions.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, SQLAlchemy 2, Alembic, PyMySQL, SQLite unit tests, MySQL 8 staging tests, pytest

---

## File responsibilities

    pyproject.toml                 dependency and package metadata
    server/config.py              validated environment settings
    server/db.py                  engine and session factory
    server/domain/states.py       enums and allowed transitions
    server/models/*.py            relational schema
    server/migrations/            Alembic environment and revisions
    server/main.py                app factory and health routes
    tests/server/                 unit and API tests

### Task 1: Add project metadata

**Files:**
- Create: pyproject.toml
- Modify: .gitignore
- Test: tests/test_api.py
- Test: tests/test_codex_runner.py
- Test: tests/test_frontend.py
- Test: tests/test_internal_analysis.py
- Test: tests/test_pdf_builder.py
- Test: tests/test_pdf_utils.py

- [ ] **Step 1: Verify the old application first**

    .venv/bin/python -m pytest -q

Expected: 57 passed.

- [ ] **Step 2: Create pyproject.toml**

    [build-system]
    requires = ["setuptools>=75"]
    build-backend = "setuptools.build_meta"

    [project]
    name = "math-competition-grader"
    version = "3.0.0.dev0"
    requires-python = ">=3.12,<3.15"
    dependencies = [
      "fastapi>=0.116,<1",
      "uvicorn[standard]>=0.35,<1",
      "python-multipart>=0.0.20,<1",
      "pydantic-settings>=2.10,<3",
      "sqlalchemy>=2.0.41,<3",
      "alembic>=1.16,<2",
      "pymysql>=1.1,<2",
      "cryptography>=45,<46",
      "pypdf>=6,<7",
      "PyMuPDF>=1.26,<2",
      "httpx>=0.28,<1",
    ]

    [project.optional-dependencies]
    dev = ["pytest>=8.4,<9", "pytest-cov>=6.2,<7", "freezegun>=1.5,<2"]

    [tool.setuptools.packages.find]
    include = ["app*", "server*", "worker*"]

    [tool.pytest.ini_options]
    testpaths = ["tests"]

- [ ] **Step 3: Install and rerun tests**

    .venv/bin/python -m pip install -e '.[dev]'
    .venv/bin/python -m pytest -q

Expected: 57 passed.

- [ ] **Step 4: Commit**

    git add pyproject.toml .gitignore
    git commit -m "build: add service project metadata"

### Task 2: Implement validated environment settings

**Files:**
- Create: server/__init__.py
- Create: server/config.py
- Create: .env.example
- Test: tests/server/test_config.py

- [ ] **Step 1: Write the failing test**

    from pathlib import Path
    import pytest
    from pydantic import ValidationError
    from server.config import Environment, ServerSettings

    def test_test_environment_accepts_sqlite(tmp_path: Path) -> None:
        settings = ServerSettings(
            environment=Environment.TEST,
            database_url="sqlite+pysqlite:///:memory:",
            data_dir=tmp_path,
            session_secret="s" * 32,
            worker_shared_key="w" * 32,
        )
        assert settings.data_dir == tmp_path

    def test_production_rejects_sqlite(tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="production requires MySQL"):
            ServerSettings(
                environment=Environment.PRODUCTION,
                database_url="sqlite+pysqlite:///:memory:",
                data_dir=tmp_path,
                session_secret="s" * 32,
                worker_shared_key="w" * 32,
            )

- [ ] **Step 2: Confirm it fails**

    .venv/bin/python -m pytest tests/server/test_config.py -q

Expected: import failure for server.config.

- [ ] **Step 3: Implement server/config.py**

    from enum import StrEnum
    from pathlib import Path
    from pydantic import Field, model_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Environment(StrEnum):
        DEVELOPMENT = "development"
        TEST = "test"
        STAGING = "staging"
        PRODUCTION = "production"

    class ServerSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="GRADER_", env_file=".env")
        environment: Environment = Environment.DEVELOPMENT
        database_url: str
        data_dir: Path
        session_secret: str = Field(min_length=32)
        worker_shared_key: str = Field(min_length=32)
        price_cents_per_page: int = Field(default=1000, ge=1)
        max_pdf_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
        max_pdf_pages: int = Field(default=30, ge=1)
        quote_ttl_seconds: int = Field(default=86400, ge=60)
        acceptance_ttl_seconds: int = Field(default=259200, ge=60)

        @model_validator(mode="after")
        def validate_environment(self) -> "ServerSettings":
            if self.environment is Environment.PRODUCTION:
                if not self.database_url.startswith("mysql+pymysql://"):
                    raise ValueError("production requires MySQL")
            return self

- [ ] **Step 4: Create .env.example**

    GRADER_ENVIRONMENT=staging
    GRADER_DATABASE_URL=mysql+pymysql://grader:change-me@127.0.0.1/grader_staging
    GRADER_DATA_DIR=/srv/grader-data/staging
    GRADER_SESSION_SECRET=replace-with-32-or-more-random-characters
    GRADER_WORKER_SHARED_KEY=replace-with-32-or-more-random-characters
    GRADER_PRICE_CENTS_PER_PAGE=1000

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_config.py -q
    git add server .env.example tests/server/test_config.py
    git commit -m "feat: add validated server environments"

Expected: tests pass.

### Task 3: Add database session management

**Files:**
- Create: server/db.py
- Create: server/models/__init__.py
- Create: server/models/base.py
- Test: tests/server/test_db.py

- [ ] **Step 1: Write the failing session test**

    from sqlalchemy import text
    from server.db import create_session_factory

    def test_session_factory_executes_query() -> None:
        factory = create_session_factory("sqlite+pysqlite:///:memory:")
        with factory() as session:
            assert session.scalar(text("select 1")) == 1

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_db.py -q

Expected: import failure for server.db.

- [ ] **Step 3: Implement server/db.py**

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    def create_session_factory(database_url: str) -> sessionmaker[Session]:
        engine = create_engine(database_url, pool_pre_ping=True)
        return sessionmaker(bind=engine, expire_on_commit=False)

- [ ] **Step 4: Implement server/models/base.py**

    from datetime import datetime, timezone
    from sqlalchemy import DateTime
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class TimestampMixin:
        created_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
            nullable=False,
        )
        updated_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            default=lambda: datetime.now(timezone.utc),
            onupdate=lambda: datetime.now(timezone.utc),
            nullable=False,
        )

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_db.py -q
    git add server/db.py server/models tests/server/test_db.py
    git commit -m "feat: add database session foundation"

### Task 4: Define state transitions

**Files:**
- Create: server/domain/__init__.py
- Create: server/domain/states.py
- Test: tests/server/test_states.py

- [ ] **Step 1: Write failing tests**

    import pytest
    from server.domain.states import JobState, OrderState
    from server.domain.states import require_job_transition, require_order_transition

    def test_v1_can_enter_review_or_refund() -> None:
        require_order_transition(OrderState.V1_DELIVERED, OrderState.V2_QUEUED)
        require_order_transition(OrderState.V1_DELIVERED, OrderState.REFUND_PENDING)

    def test_v2_cannot_create_third_round() -> None:
        with pytest.raises(ValueError, match="invalid order transition"):
            require_order_transition(OrderState.V2_DELIVERED, OrderState.V2_QUEUED)

    def test_running_job_can_enter_worker_exception() -> None:
        require_job_transition(JobState.RUNNING, JobState.WORKER_EXCEPTION)

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_states.py -q

Expected: import failure.

- [ ] **Step 3: Implement the enums and tables**

    from enum import StrEnum

    class OrderState(StrEnum):
        AWAITING_PAYMENT = "awaiting_payment"
        V1_QUEUED = "v1_queued"
        V1_RUNNING = "v1_running"
        V1_DELIVERED = "v1_delivered"
        V2_QUEUED = "v2_queued"
        V2_RUNNING = "v2_running"
        V2_DELIVERED = "v2_delivered"
        REFUND_PENDING = "refund_pending"
        REFUNDED = "refunded"
        ACCEPTED = "accepted"

    class JobState(StrEnum):
        QUEUED = "queued"
        LEASED = "leased"
        RUNNING = "running"
        UPLOADING = "uploading"
        SUCCEEDED = "succeeded"
        WORKER_EXCEPTION = "worker_exception"
        CANCELLED = "cancelled"

    ORDER_TRANSITIONS = {
        OrderState.AWAITING_PAYMENT: {OrderState.V1_QUEUED},
        OrderState.V1_QUEUED: {OrderState.V1_RUNNING, OrderState.REFUND_PENDING},
        OrderState.V1_RUNNING: {OrderState.V1_DELIVERED, OrderState.REFUND_PENDING},
        OrderState.V1_DELIVERED: {
            OrderState.ACCEPTED, OrderState.V2_QUEUED, OrderState.REFUND_PENDING,
        },
        OrderState.V2_QUEUED: {OrderState.V2_RUNNING, OrderState.REFUND_PENDING},
        OrderState.V2_RUNNING: {OrderState.V2_DELIVERED, OrderState.REFUND_PENDING},
        OrderState.V2_DELIVERED: {OrderState.ACCEPTED, OrderState.REFUND_PENDING},
        OrderState.REFUND_PENDING: {OrderState.REFUNDED, OrderState.ACCEPTED},
    }

    JOB_TRANSITIONS = {
        JobState.QUEUED: {JobState.LEASED, JobState.CANCELLED},
        JobState.LEASED: {JobState.RUNNING, JobState.QUEUED, JobState.WORKER_EXCEPTION},
        JobState.RUNNING: {
            JobState.UPLOADING, JobState.WORKER_EXCEPTION, JobState.CANCELLED,
        },
        JobState.UPLOADING: {JobState.SUCCEEDED, JobState.WORKER_EXCEPTION},
    }

    def require_order_transition(current: OrderState, target: OrderState) -> None:
        if target not in ORDER_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid order transition: {current} -> {target}")

    def require_job_transition(current: JobState, target: JobState) -> None:
        if target not in JOB_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid job transition: {current} -> {target}")

- [ ] **Step 4: Run and commit**

    .venv/bin/python -m pytest tests/server/test_states.py -q
    git add server/domain tests/server/test_states.py
    git commit -m "feat: define service state machines"

### Task 5: Create the initial schema and migration

**Files:**
- Create: server/models/accounts.py
- Create: server/models/orders.py
- Create: server/models/payments.py
- Create: server/models/workers.py
- Create: server/models/audit.py
- Create: alembic.ini
- Create: server/migrations/env.py
- Create: server/migrations/versions/0001_initial_schema.py
- Test: tests/server/test_models.py

- [ ] **Step 1: Write the schema inventory test**

    from sqlalchemy import create_engine, inspect
    from server.models.base import Base
    import server.models

    def test_initial_schema_contains_required_tables() -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        assert set(inspect(engine).get_table_names()) == {
            "admin_users", "appeals", "audit_logs", "file_objects",
            "grading_jobs", "grading_rounds", "miniapp_sessions", "orders",
            "payments", "price_rules", "quote_sessions", "refunds", "users",
            "worker_events", "workers",
        }

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_models.py -q

Expected: required tables are missing.

- [ ] **Step 3: Implement model ownership**

Use UUID strings for primary keys, integer cents for money, UTC datetimes, and these database constraints:

    users: openid unique; public_id unique
    miniapp_sessions: token_hash unique; indexed expires_at; nullable revoked_at
    file_objects: owner_user_id; kind; relative_path; sha256; size_bytes; state; expires_at
    price_rules: cents_per_page positive; effective_from; nullable retired_at
    quote_sessions: source_file_id; optional reference_file_id; positive page_count;
                    quoted_amount_cents; expires_at; nullable consumed_at
    orders: quote_session_id unique; state; paid_amount_cents; current_round_number;
            nullable acceptance_deadline; nullable downloads_revoked_at
    grading_rounds: unique(order_id, round_number); round_number restricted to 1 or 2;
                    grading_standard; note; result JSON/PDF file IDs; delivered_at
    appeals: order_id unique; text
    payments: quote_session_id; merchant_order_id unique; prepay_id unique;
              nullable external_transaction_id unique; amount_cents; state
    refunds: external_refund_id unique; source user/admin_technical; state; amount_cents
    workers: worker_id primary key; platform; version; status; last_heartbeat_at
    grading_jobs: unique(order_id, round_number); state; queued_at; worker_id;
                  lease_version; lease_expires_at; ack_deadline; attempt_count
    worker_events: worker_id; job_id; event_type; JSON details
    admin_users: username unique; password_hash; disabled_at
    audit_logs: actor_type; actor_id; action; target_type; target_id; JSON details

- [ ] **Step 4: Generate and run the migration**

    .venv/bin/alembic revision --autogenerate -m "initial schema"
    .venv/bin/alembic upgrade head

Expected: the revision creates exactly the 15 asserted tables on a clean MySQL 8 staging database.

- [ ] **Step 5: Run and commit**

    .venv/bin/python -m pytest tests/server/test_models.py -q
    git add server/models server/migrations alembic.ini tests/server/test_models.py
    git commit -m "feat: add service relational schema"

### Task 6: Add app factory and health checks

**Files:**
- Create: server/main.py
- Test: tests/server/test_health.py

- [ ] **Step 1: Write the failing API test**

    from fastapi.testclient import TestClient
    from server.config import Environment, ServerSettings
    from server.main import create_app

    def test_liveness_and_readiness(tmp_path) -> None:
        settings = ServerSettings(
            environment=Environment.TEST,
            database_url="sqlite+pysqlite:///:memory:",
            data_dir=tmp_path,
            session_secret="s" * 32,
            worker_shared_key="w" * 32,
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/health/live").json() == {"status": "ok"}
            assert client.get("/health/ready").json() == {
                "database": "ok", "storage": "ok",
            }

- [ ] **Step 2: Confirm failure**

    .venv/bin/python -m pytest tests/server/test_health.py -q

Expected: import failure for server.main.

- [ ] **Step 3: Implement create_app**

    from fastapi import FastAPI
    from sqlalchemy import text
    from server.config import ServerSettings
    from server.db import create_session_factory

    def create_app(settings: ServerSettings) -> FastAPI:
        app = FastAPI(title="Competition Grader Service", version="3.0.0")
        app.state.settings = settings
        app.state.session_factory = create_session_factory(settings.database_url)
        settings.data_dir.mkdir(parents=True, exist_ok=True)

        @app.get("/health/live")
        def live() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/health/ready")
        def ready() -> dict[str, str]:
            with app.state.session_factory() as session:
                session.scalar(text("select 1"))
            probe = settings.data_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return {"database": "ok", "storage": "ok"}

        return app

- [ ] **Step 4: Run the phase gate**

    .venv/bin/python -m pytest tests/server -q
    .venv/bin/python -m pytest -q

Expected: all tests pass, including the original 57.

- [ ] **Step 5: Commit**

    git add server/main.py tests/server/test_health.py
    git commit -m "feat: add grading service app factory"
