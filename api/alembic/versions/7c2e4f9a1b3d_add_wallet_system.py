"""add wallet system: org wallet columns, workflow avg call duration, wallet_transactions ledger

Revision ID: 7c2e4f9a1b3d
Revises: 9f3a1c7d2b4e
Create Date: 2026-07-30 10:00:00.000000

Purely additive. `price_per_minute` (org) and `avg_call_duration_minutes`
(workflow) are both nullable, and every wallet code path short-circuits to
"allowed" when either is unset — so this changes zero runtime behavior
until a superadmin explicitly sets a rate on a client.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2e4f9a1b3d"
down_revision: Union[str, None] = "b7e4c2a91f58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- organizations: wallet columns ---
    op.add_column(
        "organizations",
        sa.Column(
            "wallet_currency",
            sa.String(length=3),
            nullable=False,
            server_default="INR",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "wallet_balance",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "credit_limit",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("price_per_minute", sa.Numeric(10, 4), nullable=True),
    )

    # --- workflows: per-agent expected call duration ---
    op.add_column(
        "workflows",
        sa.Column("avg_call_duration_minutes", sa.Numeric(10, 2), nullable=True),
    )

    # --- wallet_transactions: append-only ledger ---
    wallet_transaction_type_enum = sa.Enum(
        "topup",
        "debit",
        "adjustment",
        "refund",
        "campaign_reserve",
        "campaign_cost",
        "campaign_reconcile",
        name="wallet_transaction_type",
    )
    wallet_transaction_status_enum = sa.Enum(
        "completed",
        name="wallet_transaction_status",
    )
    # No explicit .create() here — alembic_postgresql_enum's hooks (imported
    # in env.py) auto-create the Postgres enum type as part of create_table()
    # below when a new table has an Enum column; calling .create() first
    # collides with that ("type already exists").

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("type", wallet_transaction_type_enum, nullable=False),
        sa.Column(
            "status",
            wallet_transaction_status_enum,
            nullable=False,
            server_default="completed",
        ),
        sa.Column("balance_after", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("transaction_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_wallet_tx_org_created_at",
        "wallet_transactions",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "uq_wallet_tx_debit_per_run",
        "wallet_transactions",
        ["workflow_run_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'debit' AND workflow_run_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_wallet_tx_campaign_cost_per_run",
        "wallet_transactions",
        ["workflow_run_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'campaign_cost' AND workflow_run_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_wallet_tx_reserve_per_campaign",
        "wallet_transactions",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'campaign_reserve' AND campaign_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_wallet_tx_reconcile_per_campaign",
        "wallet_transactions",
        ["campaign_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'campaign_reconcile' AND campaign_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_table("wallet_transactions")

    sa.Enum(name="wallet_transaction_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="wallet_transaction_type").drop(op.get_bind(), checkfirst=True)

    op.drop_column("workflows", "avg_call_duration_minutes")

    op.drop_column("organizations", "price_per_minute")
    op.drop_column("organizations", "credit_limit")
    op.drop_column("organizations", "wallet_balance")
    op.drop_column("organizations", "wallet_currency")
