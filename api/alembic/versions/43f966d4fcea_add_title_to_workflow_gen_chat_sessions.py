"""add title to workflow gen chat sessions

Revision ID: 43f966d4fcea
Revises: d1dd3196fff5
Create Date: 2026-09-03 21:22:29.992579

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '43f966d4fcea'
down_revision: Union[str, None] = 'd1dd3196fff5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_gen_chat_sessions', sa.Column('title', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('workflow_gen_chat_sessions', 'title')
