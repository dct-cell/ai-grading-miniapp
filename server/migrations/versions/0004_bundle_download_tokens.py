"""Add bundle_download_tokens column to grading_jobs.

Revision ID: 0004
Revises: 0003

Phase 04 adds the one approved server-side exception: a GET endpoint
that streams source/reference PDFs to a worker holding an active lease.
The endpoint authorises by the worker credential plus a single-use
download token issued with the lease; the token binds the download to
one lease_version so a recycled lease immediately invalidates older
tokens. The tokens are stored as a JSON map ``{kind: token}`` so each
lease carries its own pair.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("grading_jobs") as batch:
        batch.add_column(
            sa.Column("bundle_download_tokens", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("grading_jobs") as batch:
        batch.drop_column("bundle_download_tokens")
