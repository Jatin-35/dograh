"""Receive a CRM webhook: authenticate, parse, map, normalize, dedupe, store.

The route stays thin and hands the raw request here. Every request on a
known endpoint is logged (headers redacted) except rate-limited ones, which
would otherwise let a runaway sender fill the log table.

A paused endpoint still stores leads (a CRM that is refused may give up on
the webhook altogether, and the leads would be lost) as ``on_hold``: they are
not called until someone chooses to call them. Requests failing several times
in a row raise an alert.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from loguru import logger
from pydantic import ValidationError

from api.db import db_client
from api.schemas.webhook_sync import CallSettings
from api.services.webhook_sync.alerts import record_request_outcome
from api.services.webhook_sync.auth import auth_failure
from api.services.webhook_sync.calling import enqueue_new_leads
from api.services.webhook_sync.mapping import map_lead
from api.services.webhook_sync.payload import PayloadError, parse_leads
from api.services.webhook_sync.phone import normalize_indian_mobile
from api.services.webhook_sync.rate_limit import allow_request
from api.services.webhook_sync.request_log import (
    client_ip,
    loggable_body,
    redact_headers,
)

IDEMPOTENCY_HEADER = "idempotency-key"
ON_HOLD_REASON = "Received while the endpoint was paused"


@dataclass
class ReceiveResult:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def _call_settings(raw: Optional[dict]) -> CallSettings:
    try:
        return CallSettings.model_validate(raw or {})
    except ValidationError:
        logger.warning(
            "Webhook Sync endpoint has invalid call settings; using defaults"
        )
        return CallSettings()


def _clip(value: Optional[str], length: int) -> Optional[str]:
    return value[:length] if value else None


async def receive_webhook(
    endpoint_uuid: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    peer: Optional[str],
) -> ReceiveResult:
    started = time.perf_counter()
    endpoint = await db_client.get_webhook_endpoint_by_uuid(endpoint_uuid)
    if endpoint is None:
        return ReceiveResult(
            404, {"success": False, "error": "Unknown webhook endpoint"}
        )

    lowered = {k.lower(): v for k, v in headers.items()}
    body_text, truncated = loggable_body(raw_body)

    async def _log(code: int, error: Optional[str] = None, lead_ids=None) -> None:
        try:
            await db_client.create_webhook_request_log(
                organization_id=endpoint.organization_id,
                endpoint_id=endpoint.id,
                method="POST",
                content_type=_clip(lowered.get("content-type"), 255),
                headers=redact_headers(headers),
                raw_body=body_text,
                body_truncated=truncated,
                response_code=code,
                error=error,
                ip=client_ip(headers, peer),
                lead_ids=lead_ids,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as e:
            logger.warning(f"Webhook Sync: could not store request log: {e}")

    allowed, retry_after = await allow_request(
        endpoint.id, endpoint.rate_limit_per_minute
    )
    if not allowed:
        return ReceiveResult(
            429,
            {"success": False, "error": "Rate limit exceeded"},
            {"Retry-After": str(retry_after)},
        )

    if not raw_body.strip():
        # A CRM verifying the URL (LeadSquared requires 200 "even when no
        # payload is passed"). Nothing is created, so no credentials are
        # needed, and it isn't counted as a failure for alerts. Logged so the
        # check shows in the endpoint's request log.
        await _log(200, "Empty body: treated as a URL check, nothing created")
        return ReceiveResult(
            200,
            {"success": True, "received": 0, "message": "Endpoint is ready; no leads in an empty request."},
        )

    failure = auth_failure(
        endpoint.auth_type, endpoint.secret, headers, query, raw_body
    )
    if failure:
        # The reason goes to the request log only; the client gets a generic 401.
        await _log(401, failure)
        await record_request_outcome(endpoint, False, failure)
        return ReceiveResult(
            401, {"success": False, "error": "Invalid or missing credentials"}
        )

    try:
        items = parse_leads(raw_body, lowered.get("content-type"))
    except PayloadError as e:
        await _log(e.status_code, e.message)
        await record_request_outcome(endpoint, False, e.message)
        return ReceiveResult(e.status_code, {"success": False, "error": e.message})

    settings = _call_settings(endpoint.call_settings)
    idempotency_key = _clip((lowered.get(IDEMPOTENCY_HEADER) or "").strip(), 200)
    leads: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    try:
        for index, item in enumerate(items):
            mapped = map_lead(item, endpoint.field_mapping or {})
            if not mapped.phone_raw:
                errors.append({"index": index, "error": "phone is required"})
                continue
            phone = normalize_indian_mobile(mapped.phone_raw)
            if not phone:
                status, reason = "invalid_number", "Not a valid Indian mobile number"
            elif not endpoint.is_active:
                status, reason = "on_hold", ON_HOLD_REASON
            else:
                status, reason = "received", None
            # A bulk request's Idempotency-Key covers each lead by position.
            key = None
            if idempotency_key:
                key = (
                    idempotency_key if len(items) == 1 else f"{idempotency_key}#{index}"
                )
            lead, replayed = await db_client.insert_webhook_lead(
                endpoint=endpoint,
                phone=phone,
                dedupe_window_hours=settings.dedupe_window_hours,
                reenquiry_days=settings.reenquiry_days,
                status=status,
                status_reason=reason,
                idempotency_key=key,
                external_lead_id=_clip(mapped.external_lead_id, 255),
                name=_clip(mapped.name, 255),
                phone_raw=_clip(mapped.phone_raw, 64),
                email=_clip(mapped.email, 255),
                source=_clip(mapped.source, 255),
                variables=mapped.variables,
                raw_payload=item,
            )
            leads.append(
                {"lead_id": lead.id, "status": lead.status, "replayed": replayed}
            )
    except Exception as e:
        logger.exception(
            f"Webhook Sync: storing leads failed on endpoint {endpoint.id}"
        )
        stored = [lead["lead_id"] for lead in leads]
        reason = f"Internal error while storing leads: {type(e).__name__}"
        await _log(500, reason, stored or None)
        await record_request_outcome(endpoint, False, reason)
        # A CRM retry is safe: stored leads replay by Idempotency-Key/lead id.
        return ReceiveResult(
            500,
            {"success": False, "error": "Could not store the lead(s); please retry"},
        )

    lead_ids = [lead["lead_id"] for lead in leads]
    if not leads:
        message = (
            errors[0]["error"]
            if len(items) == 1
            else "No lead in the request has a phone number"
        )
        await _log(422, message)
        await record_request_outcome(endpoint, False, message)
        body: dict[str, Any] = {"success": False, "error": message}
        if len(items) > 1:
            body.update(_counts(items, leads, errors))
            body["errors"] = errors
        return ReceiveResult(422, body)

    notes = [f"#{e['index']}: {e['error']}" for e in errors]
    if not endpoint.is_active:
        notes.insert(0, "Endpoint is paused: stored on hold, not called")
    await _log(200, "; ".join(notes) or None, lead_ids)
    await record_request_outcome(endpoint, True)

    new_leads = [
        lead["lead_id"]
        for lead in leads
        if not lead["replayed"] and lead["status"] == "received"
    ]
    if new_leads and endpoint.is_active:
        try:
            await enqueue_new_leads(endpoint, new_leads)
        except Exception as e:
            # Stored is what the CRM needs to hear; the minute sweep queues it.
            logger.error(
                f"Webhook Sync: queueing new leads on endpoint {endpoint.id} "
                f"failed; the sweep will retry: {e}"
            )
    body = {
        "success": True,
        **_counts(items, leads, errors),
        "lead_ids": lead_ids,
        "leads": leads,
    }
    if not endpoint.is_active:
        body["paused"] = True
        body["message"] = "Endpoint is paused: leads are stored on hold and not called"
    if errors:
        body["errors"] = errors
    return ReceiveResult(200, body)


def _counts(items: list, leads: list[dict], errors: list[dict]) -> dict[str, int]:
    """What happened to the request's leads, for the CRM and the logs."""
    new = [lead for lead in leads if not lead["replayed"]]
    return {
        "received": len(items),
        "created": sum(1 for lead in new if lead["status"] == "received"),
        "duplicates": sum(1 for lead in new if lead["status"] == "duplicate"),
        "invalid": sum(1 for lead in new if lead["status"] == "invalid_number"),
        "opted_out": sum(1 for lead in new if lead["status"] == "do_not_call"),
        "on_hold": sum(1 for lead in new if lead["status"] == "on_hold"),
        "replayed": len(leads) - len(new),
        "rejected": len(errors),
    }
