"""add channels (max concurrent calls) to telephony_phone_numbers

How many calls the telephony provider allows at once on a number. Campaign
dispatch places up to this many calls on the number concurrently. Existing rows
default to 1, which is exactly the behaviour before this column existed (one
call per number at a time).

Chains onto d7a4f1c9b8e2 (call_reports capability flags).

Revision ID: e3b9c7a41f06
Revises: d7a4f1c9b8e2
"""

import sqlalchemy as sa
from alembic import op

revision = "e3b9c7a41f06"
down_revision = "d7a4f1c9b8e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telephony_phone_numbers",
        sa.Column(
            "max_concurrent_calls",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_check_constraint(
        "ck_phone_numbers_max_concurrent_calls_positive",
        "telephony_phone_numbers",
        "max_concurrent_calls >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_phone_numbers_max_concurrent_calls_positive",
        "telephony_phone_numbers",
        type_="check",
    )
    op.drop_column("telephony_phone_numbers", "max_concurrent_calls")
