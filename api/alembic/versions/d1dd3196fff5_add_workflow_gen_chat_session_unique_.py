"""add workflow gen chat session unique index

Revision ID: d1dd3196fff5
Revises: 9cbc5e88ded6
Create Date: 2026-09-02 15:06:13.431633

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1dd3196fff5'
down_revision: Union[str, None] = '9cbc5e88ded6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ux_workflow_gen_chat_sessions_workflow_id', 'workflow_gen_chat_sessions', ['workflow_id'], unique=True, postgresql_where=sa.text('workflow_id IS NOT NULL'))


def downgrade() -> None:
    op.drop_index('ux_workflow_gen_chat_sessions_workflow_id', table_name='workflow_gen_chat_sessions', postgresql_where=sa.text('workflow_id IS NOT NULL'))
