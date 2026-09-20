"""add call_reports

One normalized report row per call, so dashboards read indexed typed columns
instead of scanning JSON inside workflow_runs. New table only: nothing existing
is touched and nothing is backfilled — reports are written as calls complete.

Chains onto b4f8e21c6a97, the last committed revision.

Revision ID: a91d3c5e7f20
Revises: b4f8e21c6a97
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a91d3c5e7f20"
down_revision = "b4f8e21c6a97"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_type", sa.String(16), nullable=True),
        sa.Column(
            "is_telephony",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("disconnect_category", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "is_successful",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("sentiment", sa.String(16), nullable=True),
        sa.Column("satisfied", sa.Boolean(), nullable=True),
        sa.Column("reason_for_call", sa.String(64), nullable=True),
        sa.Column(
            "ticket_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "ticket_created",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "ticket_closed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("qa_status", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workflow_run_id", name="uq_call_reports_workflow_run"),
    )
    op.create_index("ix_call_reports_id", "call_reports", ["id"])
    op.create_index(
        "ix_call_reports_org_started",
        "call_reports",
        ["organization_id", "call_started_at"],
    )
    op.create_index(
        "ix_call_reports_org_workflow_started",
        "call_reports",
        ["organization_id", "workflow_id", "call_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_call_reports_org_workflow_started", table_name="call_reports")
    op.drop_index("ix_call_reports_org_started", table_name="call_reports")
    op.drop_index("ix_call_reports_id", table_name="call_reports")
    op.drop_table("call_reports")
