"""add cancelled campaign state and cancelled_at timestamp

Revision ID: 9f3a1c7d2b4e
Revises: 7feed9f7c2fd
Create Date: 2026-07-29 21:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from alembic_postgresql_enum import TableReference

# revision identifiers, used by Alembic.
revision: str = "9f3a1c7d2b4e"
down_revision: Union[str, None] = "7feed9f7c2fd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # "cancelled" is a deliberate, permanent stop — distinct from "paused"
    # (temporary, resumable). Needed so a client can abandon a campaign for
    # good and have it reach a real terminal state, rather than sitting
    # paused forever with no way to reconcile/refund its reserved balance.
    op.sync_enum_values(
        enum_schema="public",
        enum_name="campaign_state",
        new_values=[
            "created",
            "syncing",
            "running",
            "paused",
            "completed",
            "failed",
            "cancelled",
        ],
        affected_columns=[
            TableReference(
                table_schema="public", table_name="campaigns", column_name="state"
            )
        ],
        enum_values_to_rename=[],
    )

    op.add_column(
        "campaigns",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "cancelled_at")

    op.sync_enum_values(
        enum_schema="public",
        enum_name="campaign_state",
        new_values=[
            "created",
            "syncing",
            "running",
            "paused",
            "completed",
            "failed",
        ],
        affected_columns=[
            TableReference(
                table_schema="public", table_name="campaigns", column_name="state"
            )
        ],
        enum_values_to_rename=[],
    )
