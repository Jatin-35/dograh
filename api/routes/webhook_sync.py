"""Webhook Sync management API: endpoints, their leads and request logs.

Every route is scoped to the caller's selected organization.
"""

from datetime import UTC, datetime, timedelta
from typing import List, Optional

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from api.db import db_client
from api.db.models import UserModel
from api.schemas.webhook_sync import (
    BulkLeadActionRequest,
    BulkLeadActionResponse,
    LeadActionSkipped,
    LeadCallbackResponse,
    MappingPreviewRequest,
    MappingPreviewResponse,
    OnHoldActionRequest,
    TestLeadRequest,
    TestLeadResponse,
    WebhookAuditLogListResponse,
    WebhookAuditLogResponse,
    WebhookEndpointCreateRequest,
    WebhookEndpointListResponse,
    WebhookEndpointResponse,
    WebhookEndpointUpdateRequest,
    WebhookLeadListResponse,
    WebhookLeadResponse,
    WebhookLeadStatsResponse,
    WebhookRequestLogListResponse,
    WebhookRequestLogResponse,
    WebhookSyncStatsResponse,
)
from api.services.auth.depends import get_user
from api.services.phone_masking import should_mask_phone_numbers
from api.services.webhook_sync.auth import generate_secret
from api.services.webhook_sync.callback import (
    UnsafeCallbackUrl,
    ensure_public_callback_url,
    resend_lead_result,
)
from api.services.webhook_sync.calling import (
    LeadActionError,
    act_on_leads,
    call_again,
    cancel_waiting_calls,
    stop_calling,
    stop_endpoint_campaign,
    sync_campaign_settings,
)
from api.services.webhook_sync.management import (
    WorkflowNotInOrganizationError,
    endpoint_response,
    endpoint_snapshot,
    ensure_workflow_in_organization,
    lead_response,
    record_audit,
    record_update_audit,
    start_of_today,
)
from api.services.webhook_sync.payload import PayloadError
from api.services.webhook_sync.preview import preview_mapping
from api.services.webhook_sync.test_lead import send_test_lead

router = APIRouter(prefix="/webhook-sync", tags=["webhook-sync"])

UTF8_BOM = chr(0xFEFF)
EXPORT_DISPOSITION = "attachment; filename=webhook-sync-leads.csv"

LEAD_STATUSES = {
    "received",
    "on_hold",
    "skipped",
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


async def _check_callback_url(call_settings) -> None:
    url = call_settings.callback_url if call_settings else None
    if url:
        try:
            await ensure_public_callback_url(url)
        except UnsafeCallbackUrl as e:
            raise HTTPException(status_code=400, detail=str(e))


async def _check_telephony_config(call_settings, organization_id: int) -> None:
    config_id = call_settings.telephony_configuration_id if call_settings else None
    if config_id and not await db_client.get_telephony_configuration_for_org(
        config_id, organization_id
    ):
        raise HTTPException(status_code=404, detail="Telephony configuration not found")


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
        endpoint.id, organization_id, today=await start_of_today(organization_id)
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
    rows = await db_client.list_webhook_endpoints(
        org_id, today=await start_of_today(org_id)
    )
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
    await _check_telephony_config(request.call_settings, org_id)
    await _check_callback_url(request.call_settings)
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
    current = await _endpoint_or_404(endpoint_id, org_id)
    before = endpoint_snapshot(current)
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
        await _check_telephony_config(request.call_settings, org_id)
        await _check_callback_url(request.call_settings)
        fields["call_settings"] = request.call_settings.model_dump()
    # Explicit nulls on non-nullable fields mean "leave it as is".
    fields = {k: v for k, v in fields.items() if v is not None}
    endpoint = await db_client.update_webhook_endpoint(endpoint_id, org_id, **fields)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    await record_update_audit(before, endpoint, user.id)
    # Calling rules, agent and paused state apply to the endpoint's campaign.
    await sync_campaign_settings(endpoint)
    if before["auto_call"] and not endpoint.auto_call:
        await cancel_waiting_calls(endpoint)
    # Leads stored while paused stay on hold: someone chooses which to call.
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


@router.post("/endpoints/{endpoint_id}/test-lead", response_model=TestLeadResponse)
async def send_test(
    endpoint_id: int, request: TestLeadRequest, user: UserModel = Depends(get_user)
):
    """Send a test lead through the endpoint's real receiver, authenticated
    the way the endpoint's CRM would be. With auto-call on, the number entered
    is really called."""
    org_id = _org_id(user)
    endpoint = await _endpoint_or_404(endpoint_id, org_id)
    result = await send_test_lead(endpoint, phone=request.phone, name=request.name)
    lead_ids = result.body.get("lead_ids") or []
    return TestLeadResponse(
        status_code=result.status_code,
        body=result.body,
        lead_id=lead_ids[0] if lead_ids else None,
    )


@router.post(
    "/endpoints/{endpoint_id}/resume-calling",
    response_model=WebhookEndpointResponse,
)
async def resume_calling(endpoint_id: int, user: UserModel = Depends(get_user)):
    """Resume calling after the circuit breaker paused it (too many failed
    calls in a row). The endpoint itself must be active."""
    org_id = _org_id(user)
    endpoint = await _endpoint_or_404(endpoint_id, org_id)
    if not endpoint.is_active:
        raise HTTPException(status_code=400, detail="Resume the endpoint first")
    await sync_campaign_settings(endpoint)
    await record_audit(endpoint, user.id, "calling_resumed")
    return await _with_details(endpoint, org_id)


@router.delete("/endpoints/{endpoint_id}")
async def delete_endpoint(endpoint_id: int, user: UserModel = Depends(get_user)):
    """Delete an endpoint together with its leads and request logs."""
    org_id = _org_id(user)
    endpoint = await _endpoint_or_404(endpoint_id, org_id)
    # Its queued leads must not be called once it is gone.
    await stop_endpoint_campaign(endpoint)
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
    emails = await db_client.get_webhook_user_emails(
        [row.user_id for row in rows if row.user_id]
    )
    entries = []
    for row in rows:
        entry = WebhookAuditLogResponse.model_validate(row)
        entry.user_email = emails.get(row.user_id) if row.user_id else None
        entries.append(entry)
    return WebhookAuditLogListResponse(entries=entries, total=total)


@router.post("/mapping-preview", response_model=MappingPreviewResponse)
async def mapping_preview(
    request: MappingPreviewRequest, user: UserModel = Depends(get_user)
):
    """Show how a sample CRM payload would be stored, without storing it."""
    _org_id(user)
    try:
        result = preview_mapping(
            request.sample,
            request.content_type,
            request.field_mapping.model_dump(exclude_none=True),
        )
    except PayloadError as e:
        raise HTTPException(status_code=400, detail=e.message)
    return MappingPreviewResponse(**result)


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


@router.get("/stats", response_model=WebhookSyncStatsResponse)
async def sync_stats(
    endpoint_id: Optional[int] = Query(None),
    days: int = Query(30, ge=1, le=365),
    user: UserModel = Depends(get_user),
):
    """Leads, calls, connect rate, speed to first call, request errors and
    CRM callbacks over the last ``days`` days."""
    org_id = _org_id(user)
    if endpoint_id is not None:
        await _endpoint_or_404(endpoint_id, org_id)
    stats = await db_client.webhook_sync_stats(
        org_id,
        endpoint_id=endpoint_id,
        since=datetime.now(UTC) - timedelta(days=days),
    )
    return WebhookSyncStatsResponse(days=days, **stats)


EXPORT_LIMIT = 10_000
EXPORT_COLUMNS = (
    "id",
    "received_at",
    "endpoint",
    "name",
    "phone",
    "email",
    "source",
    "city",
    "external_lead_id",
    "status",
    "status_reason",
    "call_attempts",
    "last_call_status",
    "disposition",
)


@router.get("/leads/export")
async def export_leads(
    endpoint_id: Optional[int] = Query(None),
    status: Optional[List[str]] = Query(None),
    search: Optional[str] = Query(None, max_length=100),
    received_from: Optional[datetime] = Query(None),
    received_to: Optional[datetime] = Query(None),
    user: UserModel = Depends(get_user),
):
    """The leads matching the dashboard's filters, as CSV (up to 10,000,
    newest first; numbers masked when the organization masks them)."""
    org_id = _org_id(user)
    if endpoint_id is not None:
        await _endpoint_or_404(endpoint_id, org_id)
    rows, _ = await db_client.list_webhook_leads(
        org_id,
        endpoint_id=endpoint_id,
        statuses=_statuses(status),
        search=search,
        received_from=received_from,
        received_to=received_to,
        limit=EXPORT_LIMIT,
        offset=0,
    )
    names = {e.id: e.name for e, *_ in await db_client.list_webhook_endpoints(org_id)}
    mask = await should_mask_phone_numbers(user)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    for lead in rows:
        shown = lead_response(lead, mask=mask)
        city = (shown.variables or {}).get("city")
        writer.writerow(
            [
                shown.id,
                shown.received_at.isoformat(),
                names.get(shown.endpoint_id, ""),
                shown.name or "",
                shown.phone or shown.phone_raw or "",
                shown.email or "",
                shown.source or "",
                city if isinstance(city, str) else "",
                shown.external_lead_id or "",
                shown.status,
                shown.status_reason or "",
                shown.call_attempts,
                shown.last_call_status or "",
                shown.disposition or "",
            ]
        )
    # A byte-order mark so Excel opens Hindi and emoji names correctly.
    return Response(
        content=UTF8_BOM + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": EXPORT_DISPOSITION},
    )


@router.get("/leads/stats", response_model=WebhookLeadStatsResponse)
async def lead_stats(
    endpoint_id: Optional[int] = Query(None), user: UserModel = Depends(get_user)
):
    """Lead counts by status, for the dashboard cards."""
    org_id = _org_id(user)
    if endpoint_id is not None:
        await _endpoint_or_404(endpoint_id, org_id)
    by_status, today = await db_client.count_webhook_leads_by_status(
        org_id, endpoint_id=endpoint_id, today=await start_of_today(org_id)
    )
    return WebhookLeadStatsResponse(
        total=sum(by_status.values()), today=today, by_status=by_status
    )


@router.get("/leads/{lead_id}", response_model=WebhookLeadResponse)
async def get_lead(lead_id: int, user: UserModel = Depends(get_user)):
    """One lead, with the payload the CRM sent (hidden when masking is on)."""
    org_id = _org_id(user)
    lead = await db_client.get_webhook_lead(lead_id, org_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    mask = await should_mask_phone_numbers(user)
    response = lead_response(lead, mask=mask, include_payload=True)
    delivery = await db_client.get_webhook_lead_callback(lead.id, org_id)
    if delivery is not None:
        response.callback = LeadCallbackResponse(
            status=delivery.status,
            attempts=delivery.attempt_count,
            last_status_code=delivery.last_status_code,
            last_error=delivery.last_error,
            updated_at=delivery.updated_at,
        )
    return response


async def _lead_with_endpoint(lead_id: int, organization_id: int):
    lead = await db_client.get_webhook_lead(lead_id, organization_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    endpoint = await db_client.get_webhook_endpoint(lead.endpoint_id, organization_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead, endpoint


def _bulk_response(action: str, result) -> BulkLeadActionResponse:
    return BulkLeadActionResponse(
        action=action,
        done=result.done,
        not_done=[LeadActionSkipped(**item) for item in result.not_done],
    )


@router.post("/leads/bulk-action", response_model=BulkLeadActionResponse)
async def bulk_lead_action(
    request: BulkLeadActionRequest, user: UserModel = Depends(get_user)
):
    """Call or skip several chosen leads (up to 500). Each lead is checked on
    its own; the reply says which were done and why the others weren't."""
    org_id = _org_id(user)
    result = await act_on_leads(org_id, request.lead_ids, request.action)
    return _bulk_response(request.action, result)


@router.post(
    "/endpoints/{endpoint_id}/on-hold-leads", response_model=BulkLeadActionResponse
)
async def on_hold_leads_action(
    endpoint_id: int, request: OnHoldActionRequest, user: UserModel = Depends(get_user)
):
    """Call or skip every lead the endpoint stored while it was paused."""
    org_id = _org_id(user)
    endpoint = await _endpoint_or_404(endpoint_id, org_id)
    lead_ids = await db_client.list_webhook_lead_ids_on_hold(endpoint.id, org_id)
    result = await act_on_leads(org_id, lead_ids, request.action)
    await record_audit(
        endpoint,
        user.id,
        "on_hold_called" if request.action == "call" else "on_hold_skipped",
        {"leads": {"from": len(lead_ids), "to": len(result.done)}},
    )
    return _bulk_response(request.action, result)


@router.post("/leads/{lead_id}/call-again", response_model=WebhookLeadResponse)
async def call_lead_again(lead_id: int, user: UserModel = Depends(get_user)):
    """Queue another call to the lead (it goes out within calling hours)."""
    org_id = _org_id(user)
    lead, endpoint = await _lead_with_endpoint(lead_id, org_id)
    try:
        updated = await call_again(endpoint, lead)
    except LeadActionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return lead_response(updated, mask=await should_mask_phone_numbers(user))


@router.post("/leads/{lead_id}/stop-calling", response_model=WebhookLeadResponse)
async def stop_calling_lead(lead_id: int, user: UserModel = Depends(get_user)):
    """Never call this lead again (cancels what's queued)."""
    org_id = _org_id(user)
    lead, endpoint = await _lead_with_endpoint(lead_id, org_id)
    updated = await stop_calling(endpoint, lead)
    return lead_response(updated, mask=await should_mask_phone_numbers(user))


@router.post("/leads/{lead_id}/resend-result")
async def resend_result(lead_id: int, user: UserModel = Depends(get_user)):
    """Send the lead's result to the CRM again, after a delivery gave up."""
    org_id = _org_id(user)
    await _lead_with_endpoint(lead_id, org_id)
    if await resend_lead_result(lead_id, org_id) is None:
        raise HTTPException(
            status_code=409, detail="There is no failed result to resend for this lead"
        )
    return {"success": True}


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
