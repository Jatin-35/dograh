"""move price_per_minute from organizations to workflows (per-agent rate)

Revision ID: 7c2e4f9a1b3e
Revises: f9509a6ef393
Create Date: 2026-07-30 10:00:00.000000

The wallet rate is per-agent, not per-org, mirroring
avg_call_duration_minutes — different agents on the same org can have very
different costs. No org currently has price_per_minute set (wallet billing
hasn't gone live for any client yet), so there's nothing to backfill.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e4f9a1b3e"
down_revision: Union[str, None] = "f9509a6ef393"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("price_per_minute", sa.Numeric(10, 4), nullable=True),
    )
    op.drop_column("organizations", "price_per_minute")


def downgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("price_per_minute", sa.Numeric(10, 4), nullable=True),
    )
    op.drop_column("workflows", "price_per_minute")
