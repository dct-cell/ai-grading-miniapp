"""Add worker registration identity, runtime facts and current job.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Phase 01 shipped workers as an empty placeholder table, so the new
    # NOT NULL columns need no backfill. current_job_id deliberately gets no
    # foreign key: grading_jobs.worker_id already points back at workers, and
    # the pair would form a cycle that blocks ordered create/drop on MySQL.
    with op.batch_alter_table("workers") as batch:
        batch.add_column(
            sa.Column("installation_id", sa.String(length=64), nullable=False)
        )
        batch.add_column(sa.Column("device_name", sa.String(length=128), nullable=False))
        batch.add_column(sa.Column("architecture", sa.String(length=32), nullable=False))
        batch.add_column(
            sa.Column("worker_version", sa.String(length=64), nullable=False)
        )
        batch.add_column(sa.Column("codex_version", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("tex_version", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("capabilities", sa.JSON(), nullable=False))
        batch.add_column(sa.Column("current_job_id", sa.String(length=36), nullable=True))
        batch.create_unique_constraint("uq_workers_installation_id", ["installation_id"])
        batch.drop_column("version")

    # The queue claim orders by (queued_at, id) under a state filter. Verified on
    # MySQL 8.4: without this index the claim falls back to a full scan plus
    # filesort, and a filesorted FOR UPDATE SKIP LOCKED returns no row at all
    # rather than the next unlocked one, starving concurrent Workers.
    op.create_index(
        "ix_grading_jobs_claim",
        "grading_jobs",
        ["state", "queued_at", "id"],
    )

    # One row per stored object. This is what actually makes a Worker upload
    # token single-use: the service's check-then-insert races on MySQL, so the
    # constraint has to be the authority.
    with op.batch_alter_table("file_objects") as batch:
        batch.create_unique_constraint(
            "uq_file_objects_relative_path", ["relative_path"]
        )


def downgrade() -> None:
    with op.batch_alter_table("file_objects") as batch:
        batch.drop_constraint("uq_file_objects_relative_path", type_="unique")
    op.drop_index("ix_grading_jobs_claim", table_name="grading_jobs")
    with op.batch_alter_table("workers") as batch:
        batch.add_column(sa.Column("version", sa.String(length=64), nullable=False))
        batch.drop_constraint("uq_workers_installation_id", type_="unique")
        batch.drop_column("current_job_id")
        batch.drop_column("capabilities")
        batch.drop_column("tex_version")
        batch.drop_column("codex_version")
        batch.drop_column("worker_version")
        batch.drop_column("architecture")
        batch.drop_column("device_name")
        batch.drop_column("installation_id")
