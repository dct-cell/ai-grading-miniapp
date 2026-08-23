"""Add operational_settings for admin-editable runtime values.

Revision ID: 0006
Revises: 0005

Phase 07 lets an admin change operational knobs — PDF limits, acceptance window,
ETA rate, automatic-refund thresholds — without a redeploy. Those values were
environment variables, which only take effect on restart.

Key/value rather than a column per setting so that a new knob needs no
migration, and an absent key falls back to the environment default. That
fallback is what keeps a freshly-migrated deployment behaving exactly as it did
before this table existed.

Prices are *not* stored here. They already have a versioned home in
``price_rules``, and repricing has to preserve history so an existing quote keeps
the amount it showed the user.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operational_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("operational_settings")
