from datetime import datetime
from uuid import uuid4

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, UTCDateTime


def _uuid_string() -> str:
    return str(uuid4())


class Worker(TimestampMixin, Base):
    __tablename__ = "workers"
    __table_args__ = (
        UniqueConstraint(
            "installation_id",
            name="uq_workers_installation_id",
        ),
    )

    worker_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    installation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    codex_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tex_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capabilities: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Deliberately carries no database-level foreign key to grading_jobs.id.
    # Pairing it with grading_jobs.worker_id -> workers.worker_id would create a
    # circular reference that neither SQLite nor MySQL can create or drop in a
    # single ordered pass. The lease service is the only writer and clears this
    # column in the same transaction that releases the job.
    current_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class GradingJob(TimestampMixin, Base):
    __tablename__ = "grading_jobs"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "round_number",
            name="uq_grading_jobs_order_round",
        ),
        # Matches the queue claim's WHERE plus ORDER BY. Without it MySQL 8
        # resolves the claim with a full scan and filesort, and a filesorted
        # FOR UPDATE SKIP LOCKED returns no row at all instead of the next
        # unlocked one, so concurrent Workers starve while jobs stay queued.
        Index(
            "ix_grading_jobs_claim",
            "state",
            "queued_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    order_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("orders.id"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workers.worker_id"), nullable=True
    )
    lease_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    ack_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_download_tokens: Mapped[dict[str, str] | None] = mapped_column(
        JSON, nullable=True
    )


class WorkerEvent(TimestampMixin, Base):
    __tablename__ = "worker_events"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_string
    )
    worker_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workers.worker_id"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("grading_jobs.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
