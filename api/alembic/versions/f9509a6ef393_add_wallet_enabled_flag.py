"""add wallet enabled flag

Revision ID: f9509a6ef393
Revises: 7c2e4f9a1b3d
Create Date: 2026-07-30 02:20:02.385849

Adds an explicit master on/off switch for an org's wallet, independent of
whether a rate (price_per_minute) has been configured — a superadmin can
stage a rate and balance ahead of time without it taking effect until they
flip this on. Defaults false, so this changes zero runtime behavior on its
own.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f9509a6ef393'
down_revision: Union[str, None] = '7c2e4f9a1b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column(
            'wallet_enabled',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'wallet_enabled')
