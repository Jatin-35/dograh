"""add surface to workflow_gen_chat_sessions

Records which part of the product a Scout session was opened from, so the
prompt can orient itself. Existing rows become 'standalone', which is what they
effectively were.

Revision ID: e6b2d4f70a13
Revises: d3a7f1c85b92
"""

import sqlalchemy as sa
from alembic import op

revision = "e6b2d4f70a13"
down_revision = "d3a7f1c85b92"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_gen_chat_sessions",
        sa.Column(
            "surface",
            sa.String(length=32),
            nullable=False,
            server_default="standalone",
        ),
    )
    # A session attached to a workflow was opened from that workflow's editor,
    # so backfill it rather than leaving history mislabelled.
    op.execute(
        "UPDATE workflow_gen_chat_sessions SET surface = 'workflow' "
        "WHERE workflow_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("workflow_gen_chat_sessions", "surface")
