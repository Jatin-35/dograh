"""Webhook Sync endpoint management and read models for the dashboard."""

import re
from typing import Any, Optional

from api.db import db_client
from api.db.webhook_sync_models import WebhookEndpointModel, WebhookLeadModel
from api.schemas.webhook_sync import (
    CallSettings,
    FieldMapping,
    WebhookEndpointResponse,
    WebhookLeadResponse,
)
from api.services.phone_masking import mask_phone_number
from api.services.webhook_sync.auth import TOKEN_PARAM
from api.utils.common import get_backend_endpoints

RECEIVER_PATH = "/api/v1/webhooks/inbound"
# A value with this many digits in a row is treated as a phone number.
_LONG_DIGIT_RUN = re.compile(r"\d[\d\s\-().]{8,}\d")


class WorkflowNotInOrganizationError(Exception):
    """The agent chosen for an endpoint doesn't belong to the caller's org."""


async def ensure_workflow_in_organization(workflow_id: int, organization_id: int):
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if workflow is None:
        raise WorkflowNotInOrganizationError(workflow_id)
    return workflow


async def webhook_url(endpoint: WebhookEndpointModel) -> str:
    """The URL a client pastes into their CRM."""
    backend, _ = await get_backend_endpoints()
    url = f"{backend.rstrip('/')}{RECEIVER_PATH}/{endpoint.endpoint_uuid}"
    if endpoint.auth_type == "url_token":
        url += f"?{TOKEN_PARAM}={endpoint.secret}"
    return url


def _settings(raw: Optional[dict]) -> CallSettings:
    try:
        return CallSettings.model_validate(raw or {})
    except Exception:
        return CallSettings()


def _mapping(raw: Optional[dict]) -> FieldMapping:
    try:
        return FieldMapping.model_validate(raw or {})
    except Exception:
        return FieldMapping()


async def endpoint_response(
    endpoint: WebhookEndpointModel,
    *,
    workflow_name: Optional[str] = None,
    leads_today: int = 0,
    leads_total: int = 0,
) -> WebhookEndpointResponse:
    return WebhookEndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        endpoint_uuid=endpoint.endpoint_uuid,
        webhook_url=await webhook_url(endpoint),
        auth_type=endpoint.auth_type,
        secret=endpoint.secret,
        workflow_id=endpoint.workflow_id,
        workflow_name=workflow_name,
        campaign_id=endpoint.campaign_id,
        is_active=endpoint.is_active,
        auto_call=endpoint.auto_call,
        field_mapping=_mapping(endpoint.field_mapping),
        call_settings=_settings(endpoint.call_settings),
        rate_limit_per_minute=endpoint.rate_limit_per_minute,
        leads_today=leads_today,
        leads_total=leads_total,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _mask_variables(variables: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in (variables or {}).items():
        if isinstance(value, str) and _LONG_DIGIT_RUN.search(value):
            masked[key] = mask_phone_number(value)
        else:
            masked[key] = value
    return masked


def lead_response(
    lead: WebhookLeadModel, *, mask: bool, include_payload: bool = False
) -> WebhookLeadResponse:
    """A lead for the dashboard. With masking on, numbers are masked and the
    raw payload (which holds the number verbatim) is withheld."""
    return WebhookLeadResponse(
        id=lead.id,
        endpoint_id=lead.endpoint_id,
        external_lead_id=lead.external_lead_id,
        name=lead.name,
        phone=mask_phone_number(lead.phone) if mask else lead.phone,
        phone_raw=mask_phone_number(lead.phone_raw) if mask else lead.phone_raw,
        email=lead.email,
        source=lead.source,
        variables=_mask_variables(lead.variables) if mask else (lead.variables or {}),
        status=lead.status,
        status_reason=lead.status_reason,
        duplicate_of_lead_id=lead.duplicate_of_lead_id,
        call_attempts=lead.call_attempts,
        last_call_status=lead.last_call_status,
        disposition=lead.disposition,
        last_workflow_run_id=lead.last_workflow_run_id,
        next_retry_at=lead.next_retry_at,
        received_at=lead.received_at,
        updated_at=lead.updated_at,
        raw_payload=(lead.raw_payload if include_payload and not mask else None),
    )


# ======== AUDIT TRAIL ========

# Endpoint fields whose changes are recorded (never the secret itself).
AUDITED_FIELDS = (
    "name",
    "workflow_id",
    "auth_type",
    "auto_call",
    "field_mapping",
    "call_settings",
    "rate_limit_per_minute",
)


def endpoint_snapshot(endpoint: WebhookEndpointModel) -> dict[str, Any]:
    """The audited fields of an endpoint, for diffing before and after."""
    snapshot = {name: getattr(endpoint, name) for name in AUDITED_FIELDS}
    snapshot["is_active"] = endpoint.is_active
    return snapshot


async def record_audit(
    endpoint: WebhookEndpointModel,
    user_id: Optional[int],
    action: str,
    changes: Optional[dict[str, Any]] = None,
) -> None:
    await db_client.create_webhook_audit_log(
        organization_id=endpoint.organization_id,
        endpoint_id=endpoint.id,
        endpoint_name=endpoint.name,
        user_id=user_id,
        action=action,
        changes=changes,
    )


async def record_update_audit(
    before: dict[str, Any], endpoint: WebhookEndpointModel, user_id: Optional[int]
) -> None:
    """Record a pause/resume and/or an "updated" entry with what changed."""
    after = endpoint_snapshot(endpoint)
    if before["is_active"] != after["is_active"]:
        await record_audit(
            endpoint, user_id, "resumed" if after["is_active"] else "paused"
        )
    changes = {
        name: {"from": before[name], "to": after[name]}
        for name in AUDITED_FIELDS
        if before[name] != after[name]
    }
    if changes:
        await record_audit(endpoint, user_id, "updated", changes)
