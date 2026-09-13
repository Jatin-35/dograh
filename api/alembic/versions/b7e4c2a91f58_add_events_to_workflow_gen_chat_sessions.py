"""add events to workflow_gen_chat_sessions

Stores the SSE frames a session emitted so reopening a thread can rebuild
exactly what the user saw — the in-progress steps and the approval/result
cards — instead of only the plain text recoverable from `messages`.

Revision ID: b7e4c2a91f58
Revises: 049e5e1c8489
Create Date: 2026-09-13

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4c2a91f58"
down_revision: Union[str, None] = "049e5e1c8489"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workflow_gen_chat_sessions",
        sa.Column(
            "events",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("workflow_gen_chat_sessions", "events")
