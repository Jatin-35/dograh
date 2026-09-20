"""add per-call billing mode to workflows

Revision ID: cda8c936fd9d
Revises: 7c2e4f9a1b3e
Create Date: 2026-08-03 00:00:00.000000

Adds a second per-agent pricing model (flat price_per_call) alongside the
existing price_per_minute, plus a billing_mode column selecting which one
actually applies. Existing agents default to per_minute (their current
behavior is unchanged); price_per_call is nullable until a superadmin
configures it.

Also adds pulse_seconds, the pulse size per_minute billing rounds talk time
up to (0/15/30/45/60). It defaults to 0 — exact per-second billing — so
existing agents bill exactly as before until a superadmin picks a pulse.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cda8c936fd9d"
down_revision: Union[str, None] = "7c2e4f9a1b3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

workflow_billing_mode = sa.Enum(
    "per_minute", "per_call", name="workflow_billing_mode"
)


def upgrade() -> None:
    workflow_billing_mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "workflows",
        sa.Column(
            "billing_mode",
            workflow_billing_mode,
            nullable=False,
            server_default="per_minute",
        ),
    )
    op.add_column(
        "workflows",
        sa.Column("price_per_call", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "workflows",
        sa.Column(
            "pulse_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workflows", "pulse_seconds")
    op.drop_column("workflows", "price_per_call")
    op.drop_column("workflows", "billing_mode")
    workflow_billing_mode.drop(op.get_bind(), checkfirst=True)
