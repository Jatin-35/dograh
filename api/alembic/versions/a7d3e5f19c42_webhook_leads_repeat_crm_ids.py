"""let a CRM lead id repeat on webhook leads; index leads by org and phone

The same CRM lead can now come back as a new lead: when its number was
corrected after an invalid one, or when the customer enquires again after the
re-enquiry window. The unique (endpoint, external_lead_id) index becomes a
plain one; retries of one lead are serialized by an advisory lock in code.
(organization_id, phone) backs the org-wide opt-out lookup.

Revision ID: a7d3e5f19c42
Revises: f4c8d2a91b37
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d3e5f19c42"
down_revision = "f4c8d2a91b37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_webhook_leads_endpoint_external_id", table_name="webhook_leads")
    op.create_index(
        "ix_webhook_leads_endpoint_external_id",
        "webhook_leads",
        ["endpoint_id", "external_lead_id"],
        postgresql_where=sa.text("external_lead_id IS NOT NULL"),
    )
    op.create_index(
        "ix_webhook_leads_org_phone", "webhook_leads", ["organization_id", "phone"]
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_leads_org_phone", table_name="webhook_leads")
    op.drop_index("ix_webhook_leads_endpoint_external_id", table_name="webhook_leads")
    # Fails if a CRM lead id now repeats on an endpoint; remove those first.
    op.create_index(
        "uq_webhook_leads_endpoint_external_id",
        "webhook_leads",
        ["endpoint_id", "external_lead_id"],
        unique=True,
        postgresql_where=sa.text("external_lead_id IS NOT NULL"),
    )
