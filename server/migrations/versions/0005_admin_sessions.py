"""Add admin_sessions for cookie-based Admin authentication.

Revision ID: 0005
Revises: 0004

Phase 07 replaces the Phase 05 shared-key seam with Argon2id passwords over
opaque server-side sessions, which needs somewhere to keep them. The shape
mirrors ``miniapp_sessions``: only the SHA-256 of the session token is stored,
uniquely, so a database reader cannot mint a session and a presented token can
still be looked up in one indexed read.

``admin_users.password_hash`` is unchanged. It is already ``String(255)``,
which comfortably holds the ~97 characters an Argon2id encoding takes, so
existing rows need no migration — only the placeholder values written before
Phase 07 have to be replaced with real hashes, which is an operational step
rather than a schema one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("admin_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admin_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_admin_sessions_expires_at",
        "admin_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_expires_at", table_name="admin_sessions")
    op.drop_table("admin_sessions")
