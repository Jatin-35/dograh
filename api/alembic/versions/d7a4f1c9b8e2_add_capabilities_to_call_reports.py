"""add capability flags to call_reports

Whether the call's agent has a ticket tool / an active QA node, so a dashboard can
hide tiles and charts that could never have data for the agents in scope. Existing
rows default to false (only ever local test data: the table is new).

Chains onto a91d3c5e7f20 (add call_reports).

Revision ID: d7a4f1c9b8e2
Revises: a91d3c5e7f20
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a4f1c9b8e2"
down_revision = "a91d3c5e7f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_reports",
        sa.Column(
            "ticket_capable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "call_reports",
        sa.Column(
            "analysis_capable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("call_reports", "analysis_capable")
    op.drop_column("call_reports", "ticket_capable")
