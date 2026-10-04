"""Tables for Webhook Sync: CRM → our webhook → lead → outbound call.

Kept out of ``models.py`` so the feature stays self-contained for upstream
merges; ``models.py`` imports this module at its end so the tables register on
the shared ``Base`` (Alembic reads ``Base.metadata``).
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from api.db.models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class WebhookEndpointModel(Base):
    """A URL a client's CRM POSTs new leads to."""

    __tablename__ = "webhook_endpoints"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    # Public identifier in the receiver URL; unguessable.
    endpoint_uuid = Column(String(36), nullable=False, unique=True, index=True)
    # "api_key" (X-API-Key header), "hmac" (X-Botrix-Signature of the raw
    # body) or "url_token" (?token= in the URL, for CRMs without headers).
    auth_type = Column(String(16), nullable=False, default="api_key")
    secret = Column(String(128), nullable=False)
    workflow_id = Column(
        Integer, ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    # The always-on campaign that dials this endpoint's leads (Phase 3).
    campaign_id = Column(
        Integer, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    auto_call = Column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    field_mapping = Column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::json")
    )
    call_settings = Column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::json")
    )
    rate_limit_per_minute = Column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (Index("ix_webhook_endpoints_org", "organization_id"),)


class WebhookLeadModel(Base):
    """One lead received on a webhook endpoint."""

    __tablename__ = "webhook_leads"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id = Column(
        Integer,
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_lead_id = Column(String(255), nullable=True)
    idempotency_key = Column(String(255), nullable=True)
    name = Column(String(255), nullable=True)
    # E.164 (+91XXXXXXXXXX) when valid; the original value is kept alongside.
    phone = Column(String(32), nullable=True)
    phone_raw = Column(String(64), nullable=True)
    email = Column(String(255), nullable=True)
    source = Column(String(255), nullable=True)
    variables = Column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::json")
    )
    raw_payload = Column(JSON, nullable=True)
    # received, invalid_number, duplicate, queued, scheduled, calling,
    # completed, no_answer, busy, failed, do_not_call
    status = Column(String(32), nullable=False, default="received")
    status_reason = Column(String(255), nullable=True)
    duplicate_of_lead_id = Column(
        Integer, ForeignKey("webhook_leads.id", ondelete="SET NULL"), nullable=True
    )
    call_attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    queued_run_id = Column(
        Integer, ForeignKey("queued_runs.id", ondelete="SET NULL"), nullable=True
    )
    last_workflow_run_id = Column(
        Integer, ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    last_call_status = Column(String(32), nullable=True)
    disposition = Column(String(255), nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    __table_args__ = (
        Index("ix_webhook_leads_org_received", "organization_id", "received_at"),
        Index("ix_webhook_leads_endpoint_phone", "endpoint_id", "phone"),
        Index("ix_webhook_leads_status", "status"),
        # A CRM retry with the same Idempotency-Key must never create a
        # second lead, even when retries race each other.
        Index(
            "uq_webhook_leads_endpoint_idempotency",
            "endpoint_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Not unique: the same CRM lead comes back as a new lead when its
        # number was corrected or the customer enquires again weeks later.
        # Retries of one lead are serialized by an advisory lock instead.
        Index(
            "ix_webhook_leads_endpoint_external_id",
            "endpoint_id",
            "external_lead_id",
            postgresql_where=text("external_lead_id IS NOT NULL"),
        ),
        # Opted-out numbers are looked up org-wide on every new lead.
        Index("ix_webhook_leads_org_phone", "organization_id", "phone"),
    )


class WebhookRequestLogModel(Base):
    """A raw request received on an endpoint, for debugging a CRM
    integration. Auth headers are redacted; rows expire after 30 days."""

    __tablename__ = "webhook_request_logs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id = Column(
        Integer,
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    method = Column(String(8), nullable=False, default="POST")
    content_type = Column(String(255), nullable=True)
    headers = Column(JSON, nullable=False, default=dict)
    raw_body = Column(Text, nullable=True)
    body_truncated = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    response_code = Column(Integer, nullable=False)
    error = Column(Text, nullable=True)
    ip = Column(String(64), nullable=True)
    lead_ids = Column(JSON, nullable=True)
    # Time to handle the request, for monitoring the receiver.
    duration_ms = Column(Integer, nullable=True)
    received_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index(
            "ix_webhook_request_logs_endpoint_received", "endpoint_id", "received_at"
        ),
        Index("ix_webhook_request_logs_received", "received_at"),
    )


class WebhookEndpointAuditLogModel(Base):
    """Who changed a webhook endpoint, and how.

    Not a foreign key to the endpoint, so the history survives the endpoint's
    deletion (the name is kept alongside for that reason).
    """

    __tablename__ = "webhook_endpoint_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id = Column(Integer, nullable=True)
    endpoint_name = Column(String(255), nullable=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # created, updated, paused, resumed, secret_regenerated, deleted
    action = Column(String(32), nullable=False)
    # For "updated": {field: {"from": ..., "to": ...}}; never the secret.
    changes = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    __table_args__ = (
        Index("ix_webhook_endpoint_audit_org_created", "organization_id", "created_at"),
        Index("ix_webhook_endpoint_audit_endpoint", "endpoint_id"),
    )
