"""Add quote ownership, price snapshot and frozen grading intent.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("quote_sessions") as batch:
        batch.add_column(
            sa.Column("owner_user_id", sa.String(length=36), nullable=False)
        )
        batch.add_column(
            sa.Column("price_rule_id", sa.String(length=36), nullable=False)
        )
        batch.add_column(sa.Column("grading_standard", sa.Text(), nullable=False))
        batch.add_column(sa.Column("note", sa.Text(), nullable=False))
        batch.create_foreign_key(
            "fk_quote_sessions_owner_user_id_users",
            "users",
            ["owner_user_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_quote_sessions_price_rule_id_price_rules",
            "price_rules",
            ["price_rule_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("quote_sessions") as batch:
        batch.drop_constraint(
            "fk_quote_sessions_price_rule_id_price_rules",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "fk_quote_sessions_owner_user_id_users",
            type_="foreignkey",
        )
        batch.drop_column("note")
        batch.drop_column("grading_standard")
        batch.drop_column("price_rule_id")
        batch.drop_column("owner_user_id")
