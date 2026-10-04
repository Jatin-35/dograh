"""Sending a lead's call result back to the CRM (the endpoint's callback URL).

A callback is sent once per lead, when its outcome is final: the call
connected, or it didn't and no retry is left, or it could not be placed. It
goes through the durable ``webhook_deliveries`` pipeline (retries with backoff,
a sweeper for lost jobs, dead-lettering), keyed by (last call run,
``webhook_sync_lead_<id>``) so a repeated hook can't send it twice.

The request carries the endpoint's secret in ``X-Botrix-Secret`` so the CRM
can tell it came from us, and ``X-Dograh-Delivery-Id`` (added by the delivery
pipeline) to dedupe retries.

Callback URLs must be HTTPS and must not point at a private, loopback or
link-local address (checked on save and again before each lead's callback is
queued), so an endpoint can't be used to make our servers call internal
services.
"""

import asyncio
import ipaddress
import socket
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from loguru import logger

from api.constants import DEFAULT_WEBHOOK_DELIVERY_CONFIG
from api.db import db_client
from api.schemas.webhook_sync import CallSettings
from api.utils.common import is_local_or_private_url

SECRET_HEADER = "X-Botrix-Secret"
EVENT = "lead.call_result"


def callback_node_id(lead_id: int) -> str:
    return f"webhook_sync_lead_{lead_id}"


class UnsafeCallbackUrl(ValueError):
    """The callback URL is not HTTPS or points at a non-public address."""


def _is_non_public(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
        or (
            isinstance(ip, ipaddress.IPv4Address)
            and ip in ipaddress.ip_network("100.64.0.0/10")
        )
    )


async def ensure_public_callback_url(url: str) -> None:
    """Raise UnsafeCallbackUrl unless ``url`` is HTTPS to a public host."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise UnsafeCallbackUrl("The callback URL must be an https:// URL")
    if is_local_or_private_url(url):
        raise UnsafeCallbackUrl("The callback URL must point at a public address")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise UnsafeCallbackUrl(f"Could not resolve {parts.hostname}")
    for info in infos:
        address = info[4][0].split("%", 1)[0]
        if _is_non_public(ipaddress.ip_address(address)):
            raise UnsafeCallbackUrl("The callback URL must point at a public address")


def _settings(endpoint: Any) -> CallSettings:
    try:
        return CallSettings.model_validate(endpoint.call_settings or {})
    except Exception:
        return CallSettings()


def is_final(lead_status: str, call_attempts: int, settings: CallSettings) -> bool:
    """Whether a lead's latest call outcome is its last (no retry follows).

    Mirrors the campaign engine's decision, which uses the same settings:
    only no-answer and busy are retried, up to ``max_attempts`` calls."""
    # do_not_call here is a connected call where the caller opted out.
    if lead_status in ("completed", "failed", "do_not_call"):
        return True
    if lead_status in ("no_answer", "busy"):
        retries = settings.retries
        return (
            lead_status not in retries.on_statuses
            or call_attempts >= retries.max_attempts
        )
    return False


def build_payload(lead: Any, endpoint: Any, run: Any) -> dict[str, Any]:
    gathered = (getattr(run, "gathered_context", None) or {}) if run else {}
    usage = (getattr(run, "usage_info", None) or {}) if run else {}
    extracted = gathered.get("extracted_variables")
    return {
        "event": EVENT,
        "lead": {
            "id": lead.id,
            "external_lead_id": lead.external_lead_id,
            "name": lead.name,
            "phone": lead.phone,
            "email": lead.email,
            "source": lead.source,
        },
        "endpoint": {"id": endpoint.id, "name": endpoint.name},
        "result": {
            "status": lead.status,
            "reason": lead.status_reason,
            "call_attempts": lead.call_attempts,
            "disposition": lead.disposition,
            "duration_seconds": usage.get("call_duration_seconds"),
            # What the agent collected during the call, if anything.
            "extracted": extracted if isinstance(extracted, dict) else {},
        },
        "call": {
            "id": run.id if run else None,
            "recording_url": getattr(run, "recording_url", None) if run else None,
            "transcript_url": getattr(run, "transcript_url", None) if run else None,
        },
    }


async def send_lead_result(lead_id: int, organization_id: int) -> Optional[int]:
    """Queue the lead's result for its CRM, if it has a final outcome and the
    endpoint has a callback URL. Returns the delivery id when one was queued.
    Never raises: a failed callback must not affect the call."""
    try:
        lead = await db_client.get_webhook_lead(lead_id, organization_id)
        if lead is None or not lead.last_workflow_run_id:
            return None
        endpoint = await db_client.get_webhook_endpoint(
            lead.endpoint_id, organization_id
        )
        if endpoint is None:
            return None
        settings = _settings(endpoint)
        url = settings.callback_url
        if not url or not is_final(lead.status, lead.call_attempts, settings):
            return None
        try:
            await ensure_public_callback_url(url)
        except UnsafeCallbackUrl as e:
            logger.warning(f"Webhook Sync: not sending lead {lead.id}'s result: {e}")
            return None
        run = await db_client.get_workflow_run_by_id(lead.last_workflow_run_id)
        delivery, created = await db_client.create_webhook_delivery(
            workflow_run_id=lead.last_workflow_run_id,
            organization_id=organization_id,
            endpoint_url=url,
            payload=build_payload(lead, endpoint, run),
            max_attempts=DEFAULT_WEBHOOK_DELIVERY_CONFIG["max_attempts"],
            webhook_name=f"Webhook Sync: {endpoint.name}"[:255],
            custom_headers=[{"key": SECRET_HEADER, "value": endpoint.secret}],
            webhook_node_id=callback_node_id(lead.id),
        )
        if not created:
            return None
        from api.tasks.arq import enqueue_job
        from api.tasks.function_names import FunctionNames

        await enqueue_job(
            FunctionNames.DELIVER_WEBHOOK,
            delivery.id,
            _job_id=f"webhook-delivery-{delivery.id}-0",
        )
        return delivery.id
    except Exception as e:
        logger.error(f"Webhook Sync: could not queue lead {lead_id}'s result: {e}")
        return None


async def resend_lead_result(lead_id: int, organization_id: int) -> Optional[int]:
    """Send a lead's result to its CRM again (after a delivery gave up), with
    the lead's current data. Returns the delivery id, or None if there was
    none to resend (no callback yet, or one still being sent)."""
    lead = await db_client.get_webhook_lead(lead_id, organization_id)
    if lead is None:
        return None
    delivery = await db_client.get_webhook_lead_callback(lead_id, organization_id)
    if delivery is None:
        return None
    endpoint = await db_client.get_webhook_endpoint(lead.endpoint_id, organization_id)
    run = await db_client.get_workflow_run_by_id(delivery.workflow_run_id)
    restarted = await db_client.restart_webhook_delivery(
        delivery.id, organization_id, build_payload(lead, endpoint, run)
    )
    if restarted is None:
        return None
    from api.tasks.arq import enqueue_job
    from api.tasks.function_names import FunctionNames

    await enqueue_job(
        FunctionNames.DELIVER_WEBHOOK,
        restarted.id,
        _job_id=f"webhook-delivery-{restarted.id}-resend-{int(time.time())}",
    )
    return restarted.id
