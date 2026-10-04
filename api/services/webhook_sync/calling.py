"""Calling Webhook Sync leads through the campaign engine.

Each endpoint owns one continuous campaign (``endpoint.campaign_id``). A new
lead becomes a queued run on it, so the existing engine does the dialing:
calling hours, concurrency, caller-id pool and channels, retries on no answer
and busy, the circuit breaker and per-call wallet checks.

The lead's own status follows its calls through a few hooks in that engine,
all of which land here:

- ``call_started``        the dispatcher created the run    → calling
- ``call_not_started``    quota denied / dialing failed      → failed
- ``call_not_connected``  telephony said no-answer/busy/...  → no_answer, busy, failed
- ``retry_scheduled``     the orchestrator booked a retry    → scheduled
- ``call_finished``       a connected call's run completed   → completed,
                          or do_not_call if the caller opted out

A lead is found from the run by ``webhook_lead_id`` in the run's context,
which this module sets last so a CRM field of the same name can't override it,
and every update is scoped to the run's organization. Hooks never raise: a
lead status is bookkeeping and must not break a call.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from api.db import db_client
from api.schemas.webhook_sync import CallSettings
from api.services.campaign.continuous import CONTINUOUS_KEY
from api.services.webhook_sync import alerts
from api.services.webhook_sync.callback import send_lead_result

LEAD_ID_KEY = "webhook_lead_id"
ENDPOINT_ID_KEY = "webhook_endpoint_id"
SOURCE_TYPE = "webhook"

# Telephony outcome (normalized status value) → lead status.
_NOT_CONNECTED_STATUS = {
    "no-answer": "no_answer",
    "no_answer": "no_answer",
    "busy": "busy",
}


def _settings(endpoint: Any) -> CallSettings:
    try:
        return CallSettings.model_validate(endpoint.call_settings or {})
    except Exception:
        return CallSettings()


# ======== Campaign settings from the endpoint ========


def schedule_config_for(settings: CallSettings) -> dict:
    hours = settings.calling_hours
    return {
        "enabled": True,
        "timezone": hours.timezone,
        "slots": [
            {"day_of_week": day, "start_time": hours.start, "end_time": hours.end}
            for day in hours.days
        ],
    }


def retry_config_for(settings: CallSettings) -> dict:
    retries = settings.retries
    return {
        "enabled": retries.max_attempts > 1,
        "max_retries": retries.max_attempts - 1,
        "retry_delay_seconds": retries.gap_minutes * 60,
        "retry_on_busy": "busy" in retries.on_statuses,
        "retry_on_no_answer": "no_answer" in retries.on_statuses,
        "retry_on_voicemail": False,
    }


async def _telephony_configuration_id(endpoint: Any, settings: CallSettings):
    if settings.telephony_configuration_id:
        cfg = await db_client.get_telephony_configuration_for_org(
            settings.telephony_configuration_id, endpoint.organization_id
        )
        if cfg:
            return cfg.id
    default = await db_client.get_default_telephony_configuration(
        endpoint.organization_id
    )
    return default.id if default else None


def next_call_time(settings: CallSettings, not_before: datetime) -> datetime:
    """The first moment at or after ``not_before`` inside the calling hours."""
    hours = settings.calling_hours
    tz = ZoneInfo(hours.timezone)
    local = not_before.astimezone(tz)
    start_h, start_m = map(int, hours.start.split(":"))
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        if day.weekday() not in hours.days:
            continue
        start = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=tz)
        end_h, end_m = map(int, hours.end.split(":"))
        end = datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=tz)
        if offset == 0 and start <= local < end:
            return not_before
        if local < start:
            return start.astimezone(UTC)
    return not_before


async def _live_campaign(campaign_id: Optional[int]):
    if not campaign_id:
        return None
    campaign = await db_client.get_campaign_by_id(campaign_id)
    if campaign and campaign.state not in ("cancelled", "completed", "failed"):
        return campaign
    return None


async def ensure_endpoint_campaign(endpoint: Any):
    """The endpoint's continuous campaign, created and started if missing."""
    campaign = await _live_campaign(endpoint.campaign_id)
    if campaign:
        return campaign
    # Simultaneous first leads: one request creates it, the others wait and
    # then find it (re-read under the lock).
    async with db_client.webhook_endpoint_campaign_lock(endpoint.id):
        fresh = await db_client.get_webhook_endpoint(
            endpoint.id, endpoint.organization_id
        )
        if fresh is not None:
            campaign = await _live_campaign(fresh.campaign_id)
            if campaign:
                endpoint.campaign_id = campaign.id
                return campaign
        return await _create_endpoint_campaign(endpoint)


async def _create_endpoint_campaign(endpoint: Any):
    settings = _settings(endpoint)
    campaign = await db_client.create_campaign(
        name=f"Webhook Sync: {endpoint.name}"[:255],
        workflow_id=endpoint.workflow_id,
        source_type=SOURCE_TYPE,
        source_id=endpoint.endpoint_uuid,
        user_id=endpoint.created_by,
        organization_id=endpoint.organization_id,
        retry_config=retry_config_for(settings),
        schedule_config=schedule_config_for(settings),
        telephony_configuration_id=await _telephony_configuration_id(
            endpoint, settings
        ),
    )
    now = datetime.now(UTC)
    metadata = {
        **(campaign.orchestrator_metadata or {}),
        CONTINUOUS_KEY: True,
        ENDPOINT_ID_KEY: endpoint.id,
    }
    # Nothing to sync: leads arrive one by one, so it starts running at once.
    campaign = await db_client.update_campaign(
        campaign_id=campaign.id,
        orchestrator_metadata=metadata,
        state="running" if endpoint.is_active else "paused",
        started_at=now,
        source_sync_status="completed",
        source_last_synced_at=now,
    )
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, campaign_id=campaign.id
    )
    endpoint.campaign_id = campaign.id
    logger.info(
        f"Webhook Sync endpoint {endpoint.id}: created continuous campaign {campaign.id}"
    )
    return campaign


async def sync_campaign_settings(endpoint: Any) -> None:
    """Carry an endpoint's agent, calling rules and paused state to its
    campaign. A no-op until the endpoint has one."""
    if not endpoint.campaign_id:
        return
    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    if not campaign or campaign.state in ("cancelled", "completed", "failed"):
        return
    settings = _settings(endpoint)
    metadata = {
        **(campaign.orchestrator_metadata or {}),
        "schedule_config": schedule_config_for(settings),
    }
    fields: dict[str, Any] = {
        "workflow_id": endpoint.workflow_id,
        "retry_config": retry_config_for(settings),
        "orchestrator_metadata": metadata,
        "telephony_configuration_id": await _telephony_configuration_id(
            endpoint, settings
        ),
    }
    if endpoint.is_active and campaign.state == "paused":
        fields["state"] = "running"
    elif not endpoint.is_active and campaign.state == "running":
        fields["state"] = "paused"
    await db_client.update_campaign(campaign_id=campaign.id, **fields)
    if fields.get("state") == "running":
        from api.services.campaign.circuit_breaker import circuit_breaker

        await circuit_breaker.reset(campaign.id)
        await _kick(campaign)


async def stop_endpoint_campaign(endpoint: Any) -> None:
    """The endpoint is being deleted: its queued leads must not be called."""
    if endpoint.campaign_id:
        await db_client.update_campaign(
            campaign_id=endpoint.campaign_id,
            state="cancelled",
            cancelled_at=datetime.now(UTC),
        )


async def _kick(campaign: Any) -> None:
    """Ask the orchestrator to dispatch now (it applies calling hours and
    the circuit breaker; bursts are picked up by its 60s sweep)."""
    try:
        from api.services.campaign.campaign_event_publisher import (
            get_campaign_event_publisher,
        )

        publisher = await get_campaign_event_publisher()
        await publisher.publish_sync_completed(
            campaign_id=campaign.id,
            total_rows=0,
            source_type=SOURCE_TYPE,
            source_id=campaign.source_id,
        )
    except Exception as e:
        logger.warning(f"Webhook Sync: could not notify the orchestrator: {e}")


# ======== Queueing leads ========


def call_context(lead: Any, endpoint: Any) -> dict[str, Any]:
    """The call's template variables: every lead variable, then ours last."""
    return {
        **(lead.variables or {}),
        "phone_number": lead.phone,
        LEAD_ID_KEY: lead.id,
        ENDPOINT_ID_KEY: endpoint.id,
    }


def _queue_item(
    lead: Any, endpoint: Any, settings: CallSettings, now: datetime
) -> dict:
    """How a new lead goes into the queue: now, after the delay, or when the
    calling hours next open (the orchestrator enforces the hours; this sets
    what the lead shows while it waits)."""
    delayed = now + timedelta(minutes=settings.delay_minutes)
    call_at = next_call_time(settings, delayed)
    waiting = call_at > now + timedelta(seconds=30)
    return {
        "lead_id": lead.id,
        "context_variables": call_context(lead, endpoint),
        "scheduled_for": delayed if settings.delay_minutes else None,
        "status": "scheduled" if waiting else "queued",
        "status_reason": (
            "Waiting for calling hours"
            if call_at > delayed
            else ("Waiting before calling" if waiting else None)
        ),
        "next_retry_at": call_at if waiting else None,
    }


def _callable(lead: Any) -> bool:
    return lead.status == "received" and not lead.queued_run_id and bool(lead.phone)


async def enqueue_lead(endpoint: Any, lead: Any, campaign: Any = None) -> bool:
    """Queue one new lead for calling. Returns True when queued."""
    if not (endpoint.auto_call and endpoint.is_active) or not _callable(lead):
        return False
    campaign = campaign or await ensure_endpoint_campaign(endpoint)
    item = _queue_item(lead, endpoint, _settings(endpoint), datetime.now(UTC))
    queued = await db_client.queue_webhook_leads(
        organization_id=endpoint.organization_id,
        campaign_id=campaign.id,
        items=[item],
    )
    return queued == 1


async def enqueue_new_leads(endpoint: Any, lead_ids: list[int]) -> int:
    """Queue the leads a request just stored, in one transaction; returns
    how many were queued."""
    if not (endpoint.auto_call and endpoint.is_active) or not lead_ids:
        return 0
    leads = [
        lead
        for lead in await db_client.get_webhook_leads_by_ids(
            lead_ids, endpoint.organization_id
        )
        if _callable(lead)
    ]
    if not leads:
        return 0
    campaign = await ensure_endpoint_campaign(endpoint)
    settings = _settings(endpoint)
    now = datetime.now(UTC)
    queued = await db_client.queue_webhook_leads(
        organization_id=endpoint.organization_id,
        campaign_id=campaign.id,
        items=[_queue_item(lead, endpoint, settings, now) for lead in leads],
    )
    if queued:
        await _kick(campaign)
    return queued


# The sweep only rescues leads whose queueing just failed. A lead older than
# this was received while calling was off (auto-call off, or before the
# endpoint called at all); turning calling on must not suddenly call people
# who filled a form days ago.
STRAGGLER_MAX_AGE = timedelta(hours=1)


async def enqueue_stragglers(older_than_seconds: int = 60) -> int:
    """Queue leads the receiver stored but couldn't queue (e.g. Redis was
    down for the kick, or the process died between storing and queueing),
    received in the last hour."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=older_than_seconds)
    queued = 0
    by_endpoint: dict[int, list[int]] = {}
    endpoints: dict[int, Any] = {}
    for lead, endpoint in await db_client.list_leads_awaiting_call(
        older_than=cutoff, newer_than=now - STRAGGLER_MAX_AGE
    ):
        by_endpoint.setdefault(endpoint.id, []).append(lead.id)
        endpoints[endpoint.id] = endpoint
    for endpoint_id, lead_ids in by_endpoint.items():
        try:
            queued += await enqueue_new_leads(endpoints[endpoint_id], lead_ids)
        except Exception as e:
            logger.error(
                f"Webhook Sync: queueing leads of endpoint {endpoint_id} failed: {e}"
            )
    return queued


# ======== Opting out during a call ========

OPTED_OUT_ON_CALL = "Asked not to be called again (during the call)"

# Call dispositions / tags that mean the caller asked not to be called. Give
# the agent a disposition such as "do_not_call" (or an extracted variable
# do_not_call = true) and the number is opted out automatically.
_OPT_OUT_VALUES = {
    "do_not_call",
    "donotcall",
    "dont_call",
    "dnc",
    "do_not_disturb",
    "dnd",
    "opt_out",
    "optout",
    "opted_out",
    "stop_calling",
    "unsubscribe",
}
_OPT_OUT_VARIABLES = ("do_not_call", "opt_out", "opted_out", "dnc")
_TRUE = {"true", "yes", "y", "1"}


def _norm(value: Any) -> str:
    import re

    return re.sub(r"[^0-9a-z]+", "_", str(value).lower()).strip("_")


def asked_not_to_be_called(gathered: Optional[dict]) -> bool:
    """Whether the call's outcome says the caller opted out."""
    gathered = gathered or {}
    candidates = [
        gathered.get("mapped_call_disposition"),
        gathered.get("call_disposition"),
    ]
    tags = gathered.get("call_tags")
    if isinstance(tags, list):
        candidates.extend(tags)
    if any(c and _norm(c) in _OPT_OUT_VALUES for c in candidates):
        return True
    extracted = gathered.get("extracted_variables")
    if isinstance(extracted, dict):
        for key, value in extracted.items():
            if _norm(key) in _OPT_OUT_VARIABLES and (
                value is True or _norm(value) in _TRUE
            ):
                return True
    return False


# ======== Call hooks (called from the campaign engine; never raise) ========


def _lead_id(context: Optional[dict]) -> Optional[int]:
    value = (context or {}).get(LEAD_ID_KEY)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _update(context: Optional[dict], organization_id: int, **fields) -> Any:
    """Apply a call event to its lead; returns the lead, or None when there
    is none or it was stopped ("do not call"), which events never undo."""
    lead_id = _lead_id(context)
    if lead_id is None:
        return None
    try:
        return await db_client.update_webhook_lead(
            lead_id, organization_id, unless_do_not_call=True, **fields
        )
    except Exception as e:
        logger.error(f"Webhook Sync: could not update lead {lead_id}: {e}")
        return None


async def _send_result(context: Optional[dict], organization_id: int) -> None:
    """Tell the CRM, if this was the lead's final outcome (callback.py decides)."""
    lead_id = _lead_id(context)
    if lead_id is not None:
        await send_lead_result(lead_id, organization_id)


async def _run_organization_id(workflow_run: Any) -> Optional[int]:
    """A run has no organization of its own; its campaign's is the scope."""
    campaign_id = getattr(workflow_run, "campaign_id", None)
    if not campaign_id:
        return None
    campaign = await db_client.get_campaign_by_id(campaign_id)
    return campaign.organization_id if campaign else None


async def call_started(
    queued_run: Any, workflow_run: Any, organization_id: int
) -> None:
    try:
        await _update(
            queued_run.context_variables,
            organization_id,
            increment_attempts=True,
            status="calling",
            status_reason=None,
            next_retry_at=None,
            last_workflow_run_id=workflow_run.id,
        )
    except Exception as e:
        logger.error(f"Webhook Sync: call_started hook failed: {e}")


async def call_not_started(queued_run: Any, organization_id: int, reason: str) -> None:
    try:
        lead = await _update(
            queued_run.context_variables,
            organization_id,
            status="failed",
            status_reason=str(reason)[:255],
            last_call_status="failed",
        )
        await _send_result(queued_run.context_variables, organization_id)
        if lead is not None:
            endpoint = await db_client.get_webhook_endpoint(
                lead.endpoint_id, organization_id
            )
            if endpoint is not None:
                await alerts.calls_not_placed(endpoint, str(reason)[:255])
    except Exception as e:
        logger.error(f"Webhook Sync: call_not_started hook failed: {e}")


async def call_not_connected(workflow_run: Any, status_value: str) -> None:
    try:
        context = getattr(workflow_run, "initial_context", None)
        if _lead_id(context) is None:
            return
        organization_id = await _run_organization_id(workflow_run)
        if organization_id is None:
            return
        lead_status = _NOT_CONNECTED_STATUS.get(status_value, "failed")
        await _update(
            context,
            organization_id,
            status=lead_status,
            status_reason=None if lead_status != "failed" else f"Call {status_value}",
            last_call_status=lead_status,
            disposition=status_value,
            last_workflow_run_id=workflow_run.id,
        )
        await _send_result(context, organization_id)
    except Exception as e:
        logger.error(f"Webhook Sync: call_not_connected hook failed: {e}")


async def retry_scheduled(retry_run: Any, organization_id: int) -> None:
    try:
        lead = await _update(
            retry_run.context_variables,
            organization_id,
            status="scheduled",
            status_reason=(
                f"Retry {retry_run.retry_count} after "
                f"{(retry_run.retry_reason or 'no_answer').replace('_', ' ')}"
            ),
            queued_run_id=retry_run.id,
            next_retry_at=retry_run.scheduled_for,
        )
        if lead is None and _lead_id(retry_run.context_variables) is not None:
            # The lead was stopped while this call was under way: the engine
            # booked a retry anyway, so take it off the queue.
            await db_client.cancel_queued_runs(retry_run.campaign_id, [retry_run.id])
    except Exception as e:
        logger.error(f"Webhook Sync: retry_scheduled hook failed: {e}")


async def call_finished(workflow_run_id: int) -> None:
    """A run finished its pipeline; a connected call marks the lead completed."""
    try:
        run = await db_client.get_workflow_run_by_id(workflow_run_id)
        if run is None or _lead_id(getattr(run, "initial_context", None)) is None:
            return
        gathered = run.gathered_context or {}
        tags = gathered.get("call_tags") or []
        if isinstance(tags, list) and "not_connected" in tags:
            return  # call_not_connected already recorded it
        organization_id = await _run_organization_id(run)
        if organization_id is None:
            return
        opted_out = asked_not_to_be_called(gathered)
        lead = await _update(
            run.initial_context,
            organization_id,
            status="do_not_call" if opted_out else "completed",
            status_reason=OPTED_OUT_ON_CALL if opted_out else None,
            next_retry_at=None,
            last_call_status="completed",
            disposition=(
                gathered.get("mapped_call_disposition")
                or gathered.get("call_disposition")
            ),
            last_workflow_run_id=run.id,
        )
        if opted_out and lead is not None and lead.phone:
            # The number, not just this lead: its other waiting leads stop
            # too, and future leads with it are stored as do_not_call.
            await db_client.opt_out_webhook_phone(
                organization_id,
                lead.phone,
                f"Asked not to be called during the call on lead #{lead.id}",
            )
        await _send_result(run.initial_context, organization_id)
    except Exception as e:
        logger.error(
            f"Webhook Sync: call_finished hook failed for run {workflow_run_id}: {e}"
        )


# ======== Dashboard actions on one lead ========

# A lead can be called again once its calling is over (or never began).
CALLABLE_AGAIN = {
    "received",
    "on_hold",
    "skipped",
    "failed",
    "no_answer",
    "busy",
    "completed",
}


class LeadActionError(ValueError):
    """The action doesn't apply to the lead in its current state."""


async def call_again(endpoint: Any, lead: Any) -> Any:
    """Queue one more call to a lead, from the dashboard. Works with auto-call
    off too (it's an explicit request); the endpoint must be active."""
    if lead.status not in CALLABLE_AGAIN:
        raise LeadActionError(
            f"A {lead.status.replace('_', ' ')} lead can't be called again now"
        )
    if not lead.phone:
        raise LeadActionError("The lead has no valid phone number")
    if not endpoint.is_active:
        raise LeadActionError("Resume the endpoint first")
    if await db_client.is_webhook_phone_opted_out(lead.organization_id, lead.phone):
        raise LeadActionError("This number opted out of calls")
    campaign = await ensure_endpoint_campaign(endpoint)
    queued_run = await db_client.create_queued_run(
        campaign_id=campaign.id,
        source_uuid=f"lead-{lead.id}-again-{int(datetime.now(UTC).timestamp() * 1000)}",
        context_variables=call_context(lead, endpoint),
    )
    updated = await db_client.update_webhook_lead(
        lead.id,
        lead.organization_id,
        status="queued",
        status_reason="Called again from the dashboard",
        queued_run_id=queued_run.id,
        next_retry_at=None,
    )
    await _kick(campaign)
    return updated


async def stop_calling(endpoint: Any, lead: Any) -> Any:
    """Never call this number again: cancel what's queued for this lead and
    every other waiting lead with the number in the organization, and mark
    them "do not call"; future leads with the number are stored that way
    too. A call already ringing isn't cut; nothing follows it."""
    if lead.status == "do_not_call":
        return lead
    if endpoint.campaign_id and lead.queued_run_id:
        await db_client.cancel_queued_runs(endpoint.campaign_id, [lead.queued_run_id])
    updated = await db_client.update_webhook_lead(
        lead.id,
        lead.organization_id,
        status="do_not_call",
        status_reason="Stopped from the dashboard",
        next_retry_at=None,
    )
    if lead.phone:
        await db_client.opt_out_webhook_phone(
            lead.organization_id,
            lead.phone,
            f"Number stopped from the dashboard on lead #{lead.id}",
        )
    return updated


# ======== Endpoint switches ========


async def cancel_waiting_calls(endpoint: Any) -> int:
    """Auto-call was turned off: take the endpoint's waiting calls (first
    calls and retries) off the queue. A call already ringing goes on."""
    count = await db_client.unqueue_waiting_webhook_leads(
        endpoint.id, endpoint.organization_id, "Auto-call turned off"
    )
    if count:
        logger.info(
            f"Webhook Sync endpoint {endpoint.id}: auto-call off, "
            f"unqueued {count} waiting lead(s)"
        )
    return count


# ======== Choosing which leads to call (bulk) ========

SKIPPED_REASON = "Not called (chosen on the dashboard)"


@dataclass
class LeadActionResult:
    done: list[int] = field(default_factory=list)
    not_done: list[dict[str, Any]] = field(default_factory=list)

    def refuse(self, lead_ids, reason: str) -> None:
        self.not_done.extend({"lead_id": i, "reason": reason} for i in lead_ids)


async def call_leads(organization_id: int, lead_ids: list[int]) -> LeadActionResult:
    """Queue a call to each chosen lead (on hold, skipped, or finished), from
    the dashboard. Works with auto-call off, like Call again; each call still
    goes out within its endpoint's calling hours, and a number that opted
    out is never called. Leads may span several endpoints."""
    result = LeadActionResult()
    wanted = list(dict.fromkeys(lead_ids))
    leads = await db_client.get_webhook_leads_by_ids(wanted, organization_id)
    found = {lead.id for lead in leads}
    result.refuse([i for i in wanted if i not in found], "Lead not found")

    opted_out = await db_client.opted_out_webhook_phones(
        organization_id, [lead.phone for lead in leads if lead.phone]
    )
    by_endpoint: dict[int, list[Any]] = {}
    for lead in leads:
        if lead.status not in CALLABLE_AGAIN:
            result.refuse(
                [lead.id],
                f"A {lead.status.replace('_', ' ')} lead can't be called now",
            )
        elif not lead.phone:
            result.refuse([lead.id], "The lead has no valid phone number")
        elif lead.phone in opted_out:
            result.refuse([lead.id], "This number opted out of calls")
        else:
            by_endpoint.setdefault(lead.endpoint_id, []).append(lead)

    for endpoint_id, group in by_endpoint.items():
        ids = [lead.id for lead in group]
        endpoint = await db_client.get_webhook_endpoint(endpoint_id, organization_id)
        if endpoint is None:
            result.refuse(ids, "Lead not found")
            continue
        if not endpoint.is_active:
            result.refuse(ids, "Resume the endpoint first")
            continue
        reset = await db_client.reset_webhook_leads_for_call(
            ids, organization_id, from_statuses=CALLABLE_AGAIN
        )
        result.refuse(
            [i for i in ids if i not in set(reset)], "The lead changed meanwhile"
        )
        if not reset:
            continue
        campaign = await ensure_endpoint_campaign(endpoint)
        # An explicit choice: no "wait before calling", only the calling hours.
        settings = _settings(endpoint).model_copy(update={"delay_minutes": 0})
        now = datetime.now(UTC)
        items = []
        for lead in await db_client.get_webhook_leads_by_ids(reset, organization_id):
            item = _queue_item(lead, endpoint, settings, now)
            item["status_reason"] = item["status_reason"] or "Called from the dashboard"
            items.append(item)
        await db_client.queue_webhook_leads(
            organization_id=organization_id, campaign_id=campaign.id, items=items
        )
        result.done.extend(reset)
        await _kick(campaign)
    return result


async def skip_leads(organization_id: int, lead_ids: list[int]) -> LeadActionResult:
    """Don't call the chosen leads (on hold, or stored and never queued).
    Not an opt-out: the number isn't blocked, and a later enquiry from it is
    called as usual."""
    result = LeadActionResult()
    wanted = list(dict.fromkeys(lead_ids))
    skipped = await db_client.skip_webhook_leads(
        wanted, organization_id, reason=SKIPPED_REASON
    )
    result.done.extend(skipped)
    result.refuse(
        [i for i in wanted if i not in set(skipped)],
        "Only a lead on hold, or not yet queued, can be skipped",
    )
    return result


async def act_on_leads(
    organization_id: int, lead_ids: list[int], action: str
) -> LeadActionResult:
    if action == "call":
        return await call_leads(organization_id, lead_ids)
    return await skip_leads(organization_id, lead_ids)


# ======== Maintenance (the minute sweep) ========

# A lead "calling" for this long without a word from its call is stuck: the
# call's end never reached us (e.g. the API restarted mid-call).
STUCK_CALL_AFTER = timedelta(minutes=90)


async def close_stuck_calls() -> int:
    """Settle leads stuck in "calling": from their run if it did finish,
    else as failed with the reason. Returns how many were settled."""
    settled = 0
    cutoff = datetime.now(UTC) - STUCK_CALL_AFTER
    for lead in await db_client.list_stuck_calling_webhook_leads(
        not_updated_since=cutoff
    ):
        try:
            run = (
                await db_client.get_workflow_run_by_id(lead.last_workflow_run_id)
                if lead.last_workflow_run_id
                else None
            )
            gathered = (getattr(run, "gathered_context", None) or {}) if run else {}
            tags = gathered.get("call_tags") or []
            if run is not None and run.is_completed:
                if isinstance(tags, list) and "not_connected" in tags:
                    await call_not_connected(
                        run, str(gathered.get("call_disposition") or "failed")
                    )
                else:
                    await call_finished(run.id)
            else:
                context = {LEAD_ID_KEY: lead.id}
                await _update(
                    context,
                    lead.organization_id,
                    status="failed",
                    status_reason="The call never reported how it ended",
                    last_call_status="failed",
                    next_retry_at=None,
                )
                await _send_result(context, lead.organization_id)
            settled += 1
        except Exception as e:
            logger.error(f"Webhook Sync: could not settle stuck lead {lead.id}: {e}")
    if settled:
        logger.warning(f"Webhook Sync sweep: settled {settled} stuck call(s)")
    return settled


async def alert_paused_calling() -> int:
    """Alert on active endpoints whose calling the circuit breaker paused."""
    endpoints = await db_client.list_webhook_endpoints_with_paused_calling()
    for endpoint in endpoints:
        await alerts.calling_paused(endpoint)
    return len(endpoints)


async def alert_failed_callbacks(window: timedelta = timedelta(minutes=10)) -> int:
    """Alert on endpoints whose CRM callbacks recently gave up for good."""
    failed = await db_client.list_dead_webhook_sync_callbacks(
        since=datetime.now(UTC) - window
    )
    for (organization_id, endpoint_id), count in failed.items():
        endpoint = await db_client.get_webhook_endpoint(endpoint_id, organization_id)
        if endpoint is not None:
            await alerts.callbacks_failing(endpoint, count)
    return len(failed)
