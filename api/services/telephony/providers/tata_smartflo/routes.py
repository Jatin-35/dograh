"""TATA SmartFlo HTTP routes.

Only the lifecycle webhook lives here. The inbound voice-streaming connect
request is handled by the shared ``/api/v1/telephony/inbound/run`` dispatcher —
``TataSmartfloProvider.can_handle_webhook`` claims it there — which is what
gives SmartFlo the DID lookup, signature check, concurrency slot, run creation
and quota gate without reimplementing any of them.

That reuse is the point. PR #731 upstream wrote its own connect endpoint and
omitted ``authorize_workflow_run_start``, the concurrency slot and the
``telephony_configuration_id`` binding, so inbound calls bypassed tenant quota
entirely. Nothing here can drift from the shared sequence because nothing here
duplicates it.

Mounted lazily by ``api/routes/telephony.py::_mount_provider_routers`` under
``/api/v1/telephony``.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from loguru import logger

from pipecat.utils.run_context import set_current_run_id

from api.db import db_client
from api.services.telephony.factory import get_telephony_provider_for_run
from api.services.telephony.status_processor import (
    StatusCallbackRequest,
    _process_status_update,
)

router = APIRouter()


@router.post("/tata-smartflo/events")
async def handle_tata_smartflo_events(request: Request):
    """Receive SmartFlo call lifecycle webhooks.

    SmartFlo fires 19-odd triggers across a call's life. The one that matters
    here is hangup, which carries what the run needs to close out properly:
    ``$duration``, ``$billsec``, ``$hangup_cause`` and ``$recording_url``.

    The run is found by ``$ref_id`` — the value Click-to-Call returned and the
    outbound path persisted. Inbound calls have no ref_id, so they fall back to
    the ``workflow_run_id`` echoed through ``custom_identifier``.

    Always answers 200. SmartFlo retries a non-response twice (30s then 10s),
    and a retry storm on our own parsing bug helps nobody — failures are logged
    and swallowed.
    """
    try:
        raw = (await request.body()).decode("utf-8")
        event_data = json.loads(raw) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(f"SmartFlo event body is not valid JSON: {exc}")
        return {"status": "error", "reason": "invalid_json"}

    if not isinstance(event_data, dict):
        # SmartFlo can be configured to send form-encoded rather than JSON;
        # a list or scalar here means the content-type is set wrong in their
        # portal, which is worth saying plainly rather than crashing on.
        logger.warning(
            "SmartFlo event payload was not a JSON object — check the webhook's "
            "Content-Type in the SmartFlo portal (expected application/json)."
        )
        return {"status": "error", "reason": "unexpected_payload"}

    workflow_run_id = await _resolve_run_id(event_data)
    if workflow_run_id is None:
        # Not an error: SmartFlo fires many triggers, and several arrive for
        # calls this deployment never placed.
        logger.debug("SmartFlo event did not match a known run; ignoring.")
        return {"status": "ignored", "reason": "run_not_found"}

    set_current_run_id(workflow_run_id)

    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(f"[run {workflow_run_id}] SmartFlo event: run not found")
        return {"status": "ignored", "reason": "workflow_run_not_found"}

    workflow = await db_client.get_workflow_by_id(workflow_run.workflow_id)
    if not workflow:
        logger.warning(f"[run {workflow_run_id}] SmartFlo event: workflow not found")
        return {"status": "ignored", "reason": "workflow_not_found"}

    provider = await get_telephony_provider_for_run(
        workflow_run, workflow.organization_id
    )

    # Verified with the org's own stored secret, not a value from the payload.
    if not await provider.verify_webhook_signature(str(request.url), event_data, ""):
        logger.warning(
            f"[run {workflow_run_id}] SmartFlo event failed secret verification"
        )
        return {"status": "error", "reason": "verification_failed"}

    normalized = provider.parse_status_callback(event_data)

    # Phone numbers are deliberately not logged: SmartFlo's payload carries the
    # caller and customer numbers, and PR #731 was flagged for logging them raw.
    logger.info(
        f"[run {workflow_run_id}] SmartFlo event status={normalized.get('status')} "
        f"duration={normalized.get('duration')} "
        f"hangup_cause={normalized.get('hangup_cause')}"
    )

    # Hand off to the shared processor, the same way every other provider does.
    # Without this the socket closing still ends the call, but the run never
    # receives the figures only SmartFlo knows — billed duration, why the call
    # dropped, and where the recording lives.
    status_update = StatusCallbackRequest(
        call_id=str(normalized.get("call_id") or ""),
        status=normalized.get("status") or "",
        duration=(
            str(normalized["duration"]) if normalized.get("duration") is not None else None
        ),
        direction=event_data.get("$direction") or event_data.get("direction"),
        extra={
            k: v
            for k, v in (
                ("billsec", normalized.get("billsec")),
                ("hangup_cause", normalized.get("hangup_cause")),
                ("recording_url", normalized.get("recording_url")),
            )
            if v is not None
        },
    )
    await _process_status_update(workflow_run_id, status_update)

    return {"status": "success"}


async def _resolve_run_id(event_data: dict) -> int | None:
    """Find the run this event belongs to.

    Two routes in, matching how the call started:

    - **Outbound** — ``$ref_id`` was returned by Click-to-Call and persisted on
      the run's ``gathered_context``.
    - **Inbound** — there was no initiation call and so no ref_id, but the run
      id travels in ``custom_identifier`` for outbound and can be echoed on
      inbound configurations.

    Only the run id is trusted from the payload after a lookup confirms the run
    exists; nothing here mints credentials or creates state from it.
    """
    custom = event_data.get("custom_identifier") or event_data.get("$custom_identifier")
    if isinstance(custom, dict):
        candidate = custom.get("workflow_run_id")
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError):
                logger.debug(f"SmartFlo custom_identifier not an int: {candidate!r}")

    ref_id = event_data.get("$ref_id") or event_data.get("ref_id")
    if not ref_id:
        return None

    # initiate_call stores ref_id as gathered_context.call_id precisely so this
    # lookup hits idx_workflow_runs_call_id rather than scanning.
    run = await db_client.get_workflow_run_by_call_id(str(ref_id))
    return run.id if run else None
