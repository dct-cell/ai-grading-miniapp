"""Add immutable service-tier and league-scope snapshots.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_rules",
        sa.Column(
            "service_tier",
            sa.String(length=32),
            nullable=False,
            server_default="annotated_review",
        ),
    )
    op.add_column(
        "quote_sessions",
        sa.Column(
            "service_tier",
            sa.String(length=32),
            nullable=False,
            server_default="annotated_review",
        ),
    )
    op.add_column(
        "quote_sessions",
        sa.Column("league_scope", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "grading_rounds",
        sa.Column(
            "service_tier",
            sa.String(length=32),
            nullable=False,
            server_default="annotated_review",
        ),
    )
    op.add_column(
        "grading_rounds",
        sa.Column("league_scope", sa.String(length=32), nullable=True),
    )

    op.execute(
        "UPDATE quote_sessions SET league_scope = 'auto' "
        "WHERE grading_standard = 'league_second_round'"
    )
    op.execute(
        "UPDATE grading_rounds SET league_scope = 'auto' "
        "WHERE grading_standard = 'league_second_round'"
    )
    op.create_index(
        "ix_price_rules_tier_active",
        "price_rules",
        ["service_tier", "retired_at", "effective_from"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_price_rules_tier_active", table_name="price_rules")
    op.drop_column("grading_rounds", "league_scope")
    op.drop_column("grading_rounds", "service_tier")
    op.drop_column("quote_sessions", "league_scope")
    op.drop_column("quote_sessions", "service_tier")
    op.drop_column("price_rules", "service_tier")
