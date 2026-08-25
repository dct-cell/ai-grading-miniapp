"""Persist the latest Worker grading phase.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "grading_jobs",
        sa.Column("current_phase", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("grading_jobs", "current_phase")

