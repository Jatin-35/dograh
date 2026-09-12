"""add is_workflow_scoped to workflow_gen_chat_sessions

Revision ID: 049e5e1c8489
Revises: 43f966d4fcea
Create Date: 2026-09-03 22:20:39.590740

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '049e5e1c8489'
down_revision: Union[str, None] = '43f966d4fcea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('workflow_gen_chat_sessions', sa.Column('is_workflow_scoped', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('workflow_gen_chat_sessions', 'is_workflow_scoped')
