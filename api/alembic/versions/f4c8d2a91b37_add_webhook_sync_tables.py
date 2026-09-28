"""add webhook sync tables (endpoints, leads, request logs, audit log)

A client's CRM POSTs new leads to a webhook endpoint; each lead is stored and
(from a later phase) called with the endpoint's agent. Request logs keep the
raw incoming requests for debugging an integration and expire after 30 days.
The audit log records who created, changed or deleted an endpoint.

Chains onto e3b9c7a41f06 (phone number channels).

Revision ID: f4c8d2a91b37
Revises: e3b9c7a41f06
"""

import sqlalchemy as sa
from alembic import op

revision = "f4c8d2a91b37"
down_revision = "e3b9c7a41f06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("endpoint_uuid", sa.String(36), nullable=False),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "auto_call", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "field_mapping",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "call_settings",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "rate_limit_per_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_webhook_endpoints_id", "webhook_endpoints", ["id"])
    op.create_index(
        "ix_webhook_endpoints_endpoint_uuid",
        "webhook_endpoints",
        ["endpoint_uuid"],
        unique=True,
    )
    op.create_index(
        "ix_webhook_endpoints_org", "webhook_endpoints", ["organization_id"]
    )

    op.create_table(
        "webhook_leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            sa.Integer(),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_lead_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("phone_raw", sa.String(64), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column(
            "variables", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")
        ),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_reason", sa.String(255), nullable=True),
        sa.Column(
            "duplicate_of_lead_id",
            sa.Integer(),
            sa.ForeignKey("webhook_leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "call_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "queued_run_id",
            sa.Integer(),
            sa.ForeignKey("queued_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "last_workflow_run_id",
            sa.Integer(),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_call_status", sa.String(32), nullable=True),
        sa.Column("disposition", sa.String(255), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_webhook_leads_id", "webhook_leads", ["id"])
    op.create_index(
        "ix_webhook_leads_org_received",
        "webhook_leads",
        ["organization_id", "received_at"],
    )
    op.create_index(
        "ix_webhook_leads_endpoint_phone", "webhook_leads", ["endpoint_id", "phone"]
    )
    op.create_index("ix_webhook_leads_status", "webhook_leads", ["status"])
    op.create_index(
        "uq_webhook_leads_endpoint_idempotency",
        "webhook_leads",
        ["endpoint_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "uq_webhook_leads_endpoint_external_id",
        "webhook_leads",
        ["endpoint_id", "external_lead_id"],
        unique=True,
        postgresql_where=sa.text("external_lead_id IS NOT NULL"),
    )

    op.create_table(
        "webhook_request_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            sa.Integer(),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("raw_body", sa.Text(), nullable=True),
        sa.Column(
            "body_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("response_code", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("lead_ids", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_webhook_request_logs_id", "webhook_request_logs", ["id"])
    op.create_index(
        "ix_webhook_request_logs_endpoint_received",
        "webhook_request_logs",
        ["endpoint_id", "received_at"],
    )
    op.create_index(
        "ix_webhook_request_logs_received", "webhook_request_logs", ["received_at"]
    )

    op.create_table(
        "webhook_endpoint_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_name", sa.String(255), nullable=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_webhook_endpoint_audit_logs_id", "webhook_endpoint_audit_logs", ["id"]
    )
    op.create_index(
        "ix_webhook_endpoint_audit_org_created",
        "webhook_endpoint_audit_logs",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_endpoint_audit_endpoint",
        "webhook_endpoint_audit_logs",
        ["endpoint_id"],
    )


def downgrade() -> None:
    op.drop_table("webhook_endpoint_audit_logs")
    op.drop_table("webhook_request_logs")
    op.drop_table("webhook_leads")
    op.drop_table("webhook_endpoints")
