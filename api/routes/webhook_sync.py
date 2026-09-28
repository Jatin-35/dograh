"""Webhook Sync management API: endpoints, their leads and request logs.

Every route is scoped to the caller's selected organization.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.db import db_client
from api.db.models import UserModel
from api.schemas.webhook_sync import (
    WebhookAuditLogListResponse,
    WebhookAuditLogResponse,
    WebhookEndpointCreateRequest,
    WebhookEndpointListResponse,
    WebhookEndpointResponse,
    WebhookEndpointUpdateRequest,
    WebhookLeadListResponse,
    WebhookLeadResponse,
    WebhookRequestLogListResponse,
    WebhookRequestLogResponse,
)
from api.services.auth.depends import get_user
from api.services.phone_masking import should_mask_phone_numbers
from api.services.webhook_sync.auth import generate_secret
from api.services.webhook_sync.management import (
    WorkflowNotInOrganizationError,
    endpoint_response,
    endpoint_snapshot,
    ensure_workflow_in_organization,
    lead_response,
    record_audit,
    record_update_audit,
)

router = APIRouter(prefix="/webhook-sync", tags=["webhook-sync"])

LEAD_STATUSES = {
    "received",
    "invalid_number",
    "duplicate",
    "queued",
    "scheduled",
    "calling",
    "completed",
    "no_answer",
    "busy",
    "failed",
    "do_not_call",
}


def _org_id(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected")
    return user.selected_organization_id


async def _endpoint_or_404(endpoint_id: int, organization_id: int):
    endpoint = await db_client.get_webhook_endpoint(endpoint_id, organization_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return endpoint


async def _with_details(endpoint, organization_id: int) -> WebhookEndpointResponse:
    workflow = await db_client.get_workflow(
        endpoint.workflow_id, organization_id=organization_id
    )
    today, total = await db_client.count_webhook_leads_for_endpoint(
        endpoint.id, organization_id
    )
    return await endpoint_response(
        endpoint,
        workflow_name=workflow.name if workflow else None,
        leads_today=today,
        leads_total=total,
    )


# ======== ENDPOINTS ========


@router.get("/endpoints", response_model=WebhookEndpointListResponse)
async def list_endpoints(user: UserModel = Depends(get_user)):
    org_id = _org_id(user)
    rows = await db_client.list_webhook_endpoints(org_id)
    return WebhookEndpointListResponse(
        endpoints=[
            await endpoint_response(
                endpoint, workflow_name=name, leads_today=today, leads_total=total
            )
            for endpoint, name, today, total in rows
        ]
    )


@router.post("/endpoints", response_model=WebhookEndpointResponse)
async def create_endpoint(
    request: WebhookEndpointCreateRequest, user: UserModel = Depends(get_user)
):
    org_id = _org_id(user)
    try:
        await ensure_workflow_in_organization(request.workflow_id, org_id)
    except WorkflowNotInOrganizationError:
        raise HTTPException(status_code=404, detail="Agent not found")
    endpoint = await db_client.create_webhook_endpoint(
        organization_id=org_id,
        name=request.name.strip(),
        workflow_id=request.workflow_id,
        auth_type=request.auth_type,
        secret=generate_secret(),
        is_active=request.is_active,
        auto_call=request.auto_call,
        field_mapping=request.field_mapping.model_dump(exclude_none=True),
        call_settings=request.call_settings.model_dump(),
        rate_limit_per_minute=request.rate_limit_per_minute,
        created_by=user.id,
    )
    await record_audit(endpoint, user.id, "created")
    return await _with_details(endpoint, org_id)


@router.get("/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def get_endpoint(endpoint_id: int, user: UserModel = Depends(get_user)):
    org_id = _org_id(user)
    return await _with_details(await _endpoint_or_404(endpoint_id, org_id), org_id)


@router.patch("/endpoints/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_endpoint(
    endpoint_id: int,
    request: WebhookEndpointUpdateRequest,
    user: UserModel = Depends(get_user),
):
    org_id = _org_id(user)
    before = endpoint_snapshot(await _endpoint_or_404(endpoint_id, org_id))
    fields = request.model_dump(exclude_unset=True)
    if fields.get("workflow_id") is not None:
        try:
            await ensure_workflow_in_organization(fields["workflow_id"], org_id)
        except WorkflowNotInOrganizationError:
            raise HTTPException(status_code=404, detail="Agent not found")
    if "name" in fields and fields["name"] is not None:
        fields["name"] = fields["name"].strip()
    if request.field_mapping is not None:
        fields["field_mapping"] = request.field_mapping.model_dump(exclude_none=True)
    if request.call_settings is not None:
        fields["call_settings"] = request.call_settings.model_dump()
    # Explicit nulls on non-nullable fields mean "leave it as is".
    fields = {k: v for k, v in fields.items() if v is not None}
    endpoint = await db_client.update_webhook_endpoint(endpoint_id, org_id, **fields)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    await record_update_audit(before, endpoint, user.id)
    return await _with_details(endpoint, org_id)


@router.post(
    "/endpoints/{endpoint_id}/regenerate-secret",
    response_model=WebhookEndpointResponse,
)
async def regenerate_secret(endpoint_id: int, user: UserModel = Depends(get_user)):
    """Issue a new secret; the old one stops working immediately."""
    org_id = _org_id(user)
    await _endpoint_or_404(endpoint_id, org_id)
    endpoint = await db_client.update_webhook_endpoint(
        endpoint_id, org_id, secret=generate_secret()
    )
    await record_audit(endpoint, user.id, "secret_regenerated")
    return await _with_details(endpoint, org_id)


@router.delete("/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: int, user: UserModel = Depends(get_user)):
    """Delete an endpoint together with its leads and request logs."""
    org_id = _org_id(user)
    endpoint = await _endpoint_or_404(endpoint_id, org_id)
    if not await db_client.delete_webhook_endpoint(endpoint_id, org_id):
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    # The audit log is kept after the endpoint is gone.
    await record_audit(endpoint, user.id, "deleted")
    return {"success": True}


@router.get("/audit", response_model=WebhookAuditLogListResponse)
async def list_audit_log(
    endpoint_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_user),
):
    """Who created, changed, paused, rotated or deleted endpoints, newest
    first; optionally for one endpoint (including a deleted one)."""
    org_id = _org_id(user)
    rows, total = await db_client.list_webhook_audit_logs(
        org_id, endpoint_id=endpoint_id, limit=limit, offset=offset
    )
    return WebhookAuditLogListResponse(
        entries=[WebhookAuditLogResponse.model_validate(row) for row in rows],
        total=total,
    )


# ======== LEADS ========


def _statuses(status: Optional[List[str]]) -> Optional[List[str]]:
    if not status:
        return None
    wanted = [s for part in status for s in part.split(",") if s]
    unknown = sorted(set(wanted) - LEAD_STATUSES)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"Unknown status: {', '.join(unknown)}"
        )
    return wanted


@router.get("/leads", response_model=WebhookLeadListResponse)
async def list_leads(
    endpoint_id: Optional[int] = Query(None),
    status: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    received_from: Optional[datetime] = Query(None),
    received_to: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_user),
):
    """All leads of the organization, optionally for one endpoint."""
    org_id = _org_id(user)
    if endpoint_id is not None:
        await _endpoint_or_404(endpoint_id, org_id)
    rows, total = await db_client.list_webhook_leads(
        org_id,
        endpoint_id=endpoint_id,
        statuses=_statuses(status),
        search=search,
        received_from=received_from,
        received_to=received_to,
        limit=limit,
        offset=offset,
    )
    mask = await should_mask_phone_numbers(user)
    return WebhookLeadListResponse(
        leads=[lead_response(lead, mask=mask) for lead in rows], total=total
    )


@router.get("/leads/{lead_id}", response_model=WebhookLeadResponse)
async def get_lead(lead_id: int, user: UserModel = Depends(get_user)):
    """One lead, with the payload the CRM sent (hidden when masking is on)."""
    org_id = _org_id(user)
    lead = await db_client.get_webhook_lead(lead_id, org_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    mask = await should_mask_phone_numbers(user)
    return lead_response(lead, mask=mask, include_payload=True)


# ======== REQUEST LOGS ========


@router.get(
    "/endpoints/{endpoint_id}/logs", response_model=WebhookRequestLogListResponse
)
async def list_request_logs(
    endpoint_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserModel = Depends(get_user),
):
    """Raw requests received on the endpoint (auth headers redacted)."""
    org_id = _org_id(user)
    await _endpoint_or_404(endpoint_id, org_id)
    rows, total = await db_client.list_webhook_request_logs(
        org_id, endpoint_id, limit=limit, offset=offset
    )
    mask = await should_mask_phone_numbers(user)
    logs = []
    for row in rows:
        log = WebhookRequestLogResponse.model_validate(row)
        if mask:
            # The raw body holds the number verbatim.
            log.raw_body = None
        logs.append(log)
    return WebhookRequestLogListResponse(logs=logs, total=total)
