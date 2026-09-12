"""add workflow gen chat sessions

Revision ID: 9cbc5e88ded6
Revises: cda8c936fd9d
Create Date: 2026-09-01 22:29:43.165694

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9cbc5e88ded6'
down_revision: Union[str, None] = '9f3a1c7d2b4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('workflow_gen_chat_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_uuid', sa.String(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('workflow_id', sa.Integer(), nullable=True),
    sa.Column('revision', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('messages', sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    sa.Column('pending_action', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(), server_default=sa.text("'idle'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_workflow_gen_chat_sessions_organization_id', 'workflow_gen_chat_sessions', ['organization_id'], unique=False)
    op.create_index(op.f('ix_workflow_gen_chat_sessions_session_uuid'), 'workflow_gen_chat_sessions', ['session_uuid'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_workflow_gen_chat_sessions_session_uuid'), table_name='workflow_gen_chat_sessions')
    op.drop_index('ix_workflow_gen_chat_sessions_organization_id', table_name='workflow_gen_chat_sessions')
    op.drop_table('workflow_gen_chat_sessions')
