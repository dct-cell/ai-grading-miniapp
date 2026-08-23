"""Create the initial relational schema.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("openid", sa.String(length=128), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("openid"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_table(
        "price_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cents_per_page", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "cents_per_page > 0",
            name="ck_price_rules_cents_per_page_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "file_objects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "miniapp_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_miniapp_sessions_expires_at",
        "miniapp_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "quote_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("reference_file_id", sa.String(length=36), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("quoted_amount_cents", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "page_count > 0",
            name="ck_quote_sessions_page_count_positive",
        ),
        sa.ForeignKeyConstraint(["reference_file_id"], ["file_objects.id"]),
        sa.ForeignKeyConstraint(["source_file_id"], ["file_objects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quote_session_id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("paid_amount_cents", sa.Integer(), nullable=False),
        sa.Column("current_round_number", sa.Integer(), nullable=False),
        sa.Column("acceptance_deadline", sa.DateTime(), nullable=True),
        sa.Column("downloads_revoked_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["quote_session_id"], ["quote_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quote_session_id"),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quote_session_id", sa.String(length=36), nullable=False),
        sa.Column("merchant_order_id", sa.String(length=64), nullable=False),
        sa.Column("prepay_id", sa.String(length=128), nullable=False),
        sa.Column("external_transaction_id", sa.String(length=64), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["quote_session_id"], ["quote_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_transaction_id"),
        sa.UniqueConstraint("merchant_order_id"),
        sa.UniqueConstraint("prepay_id"),
    )
    op.create_table(
        "grading_rounds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("grading_standard", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("result_json_file_id", sa.String(length=36), nullable=True),
        sa.Column("result_pdf_file_id", sa.String(length=36), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "round_number IN (1, 2)",
            name="ck_grading_rounds_round_number",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["result_json_file_id"], ["file_objects.id"]),
        sa.ForeignKeyConstraint(["result_pdf_file_id"], ["file_objects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id",
            "round_number",
            name="uq_grading_rounds_order_round",
        ),
    )
    op.create_table(
        "appeals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("payment_id", sa.String(length=36), nullable=False),
        sa.Column("external_refund_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "source IN ('user', 'admin_technical')",
            name="ck_refunds_source",
        ),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_refund_id"),
    )
    op.create_table(
        "grading_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=True),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("ack_deadline", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_id",
            "round_number",
            name="uq_grading_jobs_order_round",
        ),
    )
    op.create_table(
        "worker_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("worker_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["job_id"], ["grading_jobs.id"]),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("worker_events")
    op.drop_table("grading_jobs")
    op.drop_table("refunds")
    op.drop_table("appeals")
    op.drop_table("grading_rounds")
    op.drop_table("payments")
    op.drop_table("orders")
    op.drop_table("quote_sessions")
    op.drop_index("ix_miniapp_sessions_expires_at", table_name="miniapp_sessions")
    op.drop_table("miniapp_sessions")
    op.drop_table("file_objects")
    op.drop_table("audit_logs")
    op.drop_table("price_rules")
    op.drop_table("workers")
    op.drop_table("admin_users")
    op.drop_table("users")
