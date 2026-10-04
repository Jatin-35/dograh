# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.
# ruff: noqa: F811
"""Webhook Sync Phase 3: leads are called through a continuous campaign.

Covers the endpoint's campaign (created running, flagged continuous, carrying
the calling hours and retry rules), queueing a lead, the lead following its
call through the engine's hooks, and the engine changes a continuous campaign
needs (never auto-completes, survives a failed batch, bills per call).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from api.db import db_client
from api.db.models import QueuedRunModel
from api.schemas.webhook_sync import CallSettings
from api.services.campaign.continuous import (
    campaign_bills_per_call,
    is_continuous_campaign,
)
from api.services.webhook_sync import calling
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401

IST = "Asia/Kolkata"


def _settings(**overrides) -> CallSettings:
    return CallSettings.model_validate(overrides)


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def test_calling_hours_become_one_schedule_slot_per_day():
    settings = _settings(
        calling_hours={
            "start": "10:00",
            "end": "19:30",
            "timezone": IST,
            "days": [0, 2],
        }
    )
    assert calling.schedule_config_for(settings) == {
        "enabled": True,
        "timezone": IST,
        "slots": [
            {"day_of_week": 0, "start_time": "10:00", "end_time": "19:30"},
            {"day_of_week": 2, "start_time": "10:00", "end_time": "19:30"},
        ],
    }


def test_retry_rules_map_onto_the_campaign_retry_config():
    settings = _settings(
        retries={"max_attempts": 3, "gap_minutes": 45, "on_statuses": ["busy"]}
    )
    assert calling.retry_config_for(settings) == {
        "enabled": True,
        "max_retries": 2,
        "retry_delay_seconds": 2700,
        "retry_on_busy": True,
        "retry_on_no_answer": False,
        "retry_on_voicemail": False,
    }
    assert (
        calling.retry_config_for(_settings(retries={"max_attempts": 1}))["enabled"]
        is False
    )


@pytest.mark.parametrize(
    "local_now, expected_local",
    [
        # Inside the window: now.
        ("2026-09-28 14:00", "2026-09-28 14:00"),
        # Before it opens: today's start.
        ("2026-09-28 07:15", "2026-09-28 09:00"),
        # After it closes: tomorrow's start.
        ("2026-09-28 21:30", "2026-09-29 09:00"),
    ],
    ids=["inside", "before", "after"],
)
def test_next_call_time_respects_calling_hours(local_now, expected_local):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(IST)
    now = datetime.strptime(local_now, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    expected = datetime.strptime(expected_local, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    assert calling.next_call_time(_settings(), now) == expected


def test_next_call_time_skips_days_off():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(IST)
    # 2026-09-26 is a Saturday; weekdays only → Monday 09:00.
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=tz)
    weekdays = _settings(calling_hours={"days": [0, 1, 2, 3, 4]})
    assert calling.next_call_time(weekdays, saturday) == datetime(
        2026, 9, 28, 9, 0, tzinfo=tz
    )


def test_a_crm_field_cannot_redirect_the_call_to_another_lead():
    lead = SimpleNamespace(
        id=7,
        phone="+919876543210",
        variables={
            "webhook_lead_id": 999,
            "phone_number": "+910000000000",
            "city": "Patna",
        },
    )
    context = calling.call_context(lead, SimpleNamespace(id=3))
    assert context["webhook_lead_id"] == 7
    assert context["phone_number"] == "+919876543210"
    assert context["webhook_endpoint_id"] == 3
    assert context["city"] == "Patna"


# ---------------------------------------------------------------------------
# On a real database
# ---------------------------------------------------------------------------


@pytest.fixture
def no_kick():
    with patch.object(calling, "_kick", AsyncMock()) as kick:
        yield kick


async def _queued_runs(env, campaign_id):
    async with env.factory() as session:
        result = await session.execute(
            select(QueuedRunModel)
            .where(QueuedRunModel.campaign_id == campaign_id)
            .order_by(QueuedRunModel.id)
        )
        return list(result.scalars().all())


async def _open_hours_endpoint(env, **overrides):
    # Calling hours covering the whole day, so tests don't depend on the clock.
    call_settings = {
        "calling_hours": {"start": "00:00", "end": "23:59", "timezone": IST},
        "retries": {
            "max_attempts": 2,
            "gap_minutes": 10,
            "on_statuses": ["no_answer", "busy"],
        },
    }
    return await env.make_endpoint(
        call_settings=call_settings, **{"auto_call": True, **overrides}
    )


@pytest.mark.asyncio
async def test_a_new_lead_is_queued_on_the_endpoints_continuous_campaign(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    result = await post(
        endpoint,
        {"name": "Asha", "mobile": "9876543210", "city": "Patna"},
        key_headers(endpoint),
    )
    assert result.status_code == 200

    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    assert is_continuous_campaign(campaign)
    assert campaign.state == "running"
    assert (campaign.source_type, campaign.source_id) == (
        "webhook",
        endpoint.endpoint_uuid,
    )
    assert campaign.workflow_id == endpoint.workflow_id
    assert campaign.retry_config["max_retries"] == 1
    assert campaign.orchestrator_metadata["schedule_config"]["timezone"] == IST

    (run,) = await _queued_runs(env, campaign.id)
    (lead,) = await env.leads(endpoint)
    assert run.source_uuid == f"lead-{lead.id}"
    assert run.scheduled_for is None
    assert run.context_variables["phone_number"] == "+919876543210"
    assert run.context_variables["webhook_lead_id"] == lead.id
    assert run.context_variables["city"] == "Patna"
    assert (lead.status, lead.queued_run_id) == ("queued", run.id)
    no_kick.assert_awaited()


@pytest.mark.asyncio
async def test_duplicates_invalid_numbers_and_replays_are_not_queued(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    headers = key_headers(endpoint, **{"Idempotency-Key": "k1"})
    await post(endpoint, {"mobile": "9876543210"}, headers)
    await post(endpoint, {"mobile": "9876543210"}, headers)  # replay
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))  # duplicate
    await post(endpoint, {"mobile": "12345"}, key_headers(endpoint))  # invalid

    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert len(await _queued_runs(env, endpoint.campaign_id)) == 1


@pytest.mark.asyncio
async def test_auto_call_off_or_paused_stores_without_queueing(env, no_kick):
    endpoint = await _open_hours_endpoint(env, auto_call=False)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.queued_run_id) == ("received", None)
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert endpoint.campaign_id is None


@pytest.mark.asyncio
async def test_a_delay_or_closed_hours_schedules_the_lead(env, no_kick):
    endpoint = await env.make_endpoint(
        call_settings={"delay_minutes": 30}, auto_call=True
    )
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (run,) = await _queued_runs(env, endpoint.campaign_id)
    assert run.scheduled_for is not None
    assert (
        timedelta(minutes=29)
        < run.scheduled_for - datetime.now(UTC)
        <= timedelta(minutes=30)
    )
    assert lead.status == "scheduled" and lead.next_retry_at is not None


@pytest.mark.asyncio
async def test_the_lead_follows_its_calls_through_to_completion(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    org = endpoint.organization_id

    async def real_run(qr, gathered=None):
        return await db_client.create_workflow_run(
            name=f"WR-CAMPAIGN-{endpoint.campaign_id}-{qr.id}",
            workflow_id=endpoint.workflow_id,
            mode="twilio",
            user_id=endpoint.created_by,
            initial_context=qr.context_variables,
            gathered_context=gathered,
            campaign_id=endpoint.campaign_id,
            queued_run_id=qr.id,
            organization_id=org,
        )

    # 1. The dispatcher starts the first call.
    run1 = await real_run(queued_run)
    await calling.call_started(queued_run, run1, org)
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.call_attempts, lead.last_workflow_run_id) == (
        "calling",
        1,
        run1.id,
    )

    # 2. Nobody answers.
    await calling.call_not_connected(run1, "no-answer")
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.last_call_status) == ("no_answer", "no_answer")

    # 3. The orchestrator books a retry.
    retry = await db_client.create_queued_run(
        campaign_id=endpoint.campaign_id,
        source_uuid=f"{queued_run.source_uuid}_retry_1",
        context_variables={**queued_run.context_variables, "is_retry": True},
        retry_count=1,
        parent_queued_run_id=queued_run.id,
        scheduled_for=datetime.now(UTC) + timedelta(minutes=10),
        retry_reason="no_answer",
    )
    await calling.retry_scheduled(retry, org)
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.queued_run_id) == ("scheduled", retry.id)
    assert lead.next_retry_at is not None

    # 4. The retry connects and the call finishes.
    run2 = await real_run(retry, gathered={"mapped_call_disposition": "Interested"})
    await calling.call_started(retry, run2, org)
    await calling.call_finished(run2.id)
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.call_attempts, lead.disposition) == (
        "completed",
        2,
        "Interested",
    )
    assert (lead.last_workflow_run_id, lead.next_retry_at) == (run2.id, None)


@pytest.mark.asyncio
async def test_a_call_that_could_not_be_placed_fails_the_lead_with_the_reason(
    env, no_kick
):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    await calling.call_not_started(
        queued_run, endpoint.organization_id, "Insufficient wallet balance"
    )
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason) == (
        "failed",
        "Insufficient wallet balance",
    )


@pytest.mark.asyncio
async def test_hooks_never_touch_another_organizations_lead(env, no_kick):
    endpoint = await _open_hours_endpoint(env, key="a")
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    # A run on org b's campaign that (somehow) names org a's lead.
    other_campaign = await calling.ensure_endpoint_campaign(
        await _open_hours_endpoint(env, key="b")
    )
    other_org_run = SimpleNamespace(
        id=900,
        campaign_id=other_campaign.id,
        initial_context={"webhook_lead_id": lead.id},
    )
    await calling.call_not_connected(other_org_run, "busy")
    (after,) = await env.leads(endpoint)
    assert after.status == lead.status == "queued"


@pytest.mark.asyncio
async def test_pausing_resuming_and_deleting_the_endpoint_drive_its_campaign(
    env, no_kick
):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointUpdateRequest

    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    a = env.orgs["a"].user
    backend = patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    )
    with backend:
        await routes.update_endpoint(
            endpoint.id, WebhookEndpointUpdateRequest(is_active=False), user=a
        )
        endpoint = await db_client.get_webhook_endpoint(
            endpoint.id, endpoint.organization_id
        )
        assert (
            await db_client.get_campaign_by_id(endpoint.campaign_id)
        ).state == "paused"

        await routes.update_endpoint(
            endpoint.id,
            WebhookEndpointUpdateRequest(
                is_active=True,
                call_settings=CallSettings(
                    retries={"max_attempts": 4, "gap_minutes": 20}
                ),
            ),
            user=a,
        )
        campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
        assert campaign.state == "running"
        assert campaign.retry_config["max_retries"] == 3
        assert campaign.retry_config["retry_delay_seconds"] == 1200

        await routes.delete_endpoint(endpoint.id, user=a)
    assert (await db_client.get_campaign_by_id(campaign.id)).state == "cancelled"


@pytest.mark.asyncio
async def test_the_sweep_queues_a_lead_whose_queueing_failed(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    with patch(
        "api.services.webhook_sync.receiver.enqueue_new_leads",
        AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        result = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    # The CRM still hears the lead was stored.
    assert result.status_code == 200
    (lead,) = await env.leads(endpoint)
    assert lead.status == "received"

    assert await calling.enqueue_stragglers(older_than_seconds=-5) >= 1
    (lead,) = await env.leads(endpoint)
    assert lead.status == "queued" and lead.queued_run_id is not None
    # Running it again queues nothing new.
    before = len(
        await _queued_runs(
            env,
            (
                await db_client.get_webhook_endpoint(
                    endpoint.id, endpoint.organization_id
                )
            ).campaign_id,
        )
    )
    await calling.enqueue_stragglers(older_than_seconds=-5)
    after = len(
        await _queued_runs(
            env,
            (
                await db_client.get_webhook_endpoint(
                    endpoint.id, endpoint.organization_id
                )
            ).campaign_id,
        )
    )
    assert before == after == 1


# ---------------------------------------------------------------------------
# The campaign engine's continuous-campaign behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_continuous_campaign_never_auto_completes(env, no_kick):
    from api.services.campaign.campaign_orchestrator import CampaignOrchestrator

    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    await db_client.update_campaign(
        campaign_id=campaign.id,
        last_activity_at=datetime.now(UTC) - timedelta(days=3),
    )
    campaign = await db_client.get_campaign_by_id(campaign.id)
    orchestrator = CampaignOrchestrator.__new__(CampaignOrchestrator)
    orchestrator._batch_in_progress = set()
    orchestrator._last_activity = {}
    orchestrator.completion_timeout = 3600
    assert await orchestrator._should_mark_complete(campaign) is False


@pytest.mark.asyncio
async def test_a_failed_batch_does_not_end_a_continuous_campaign(env, no_kick):
    from api.tasks.campaign_tasks import _fail_campaign

    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    await _fail_campaign(campaign.id)
    assert (await db_client.get_campaign_by_id(campaign.id)).state == "running"


@pytest.mark.asyncio
async def test_continuous_campaign_calls_bill_per_call(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    assert await campaign_bills_per_call(campaign.id) is True
    assert await campaign_bills_per_call(None) is False
    # An ordinary campaign keeps its up-front reservation.
    ordinary = await db_client.create_campaign(
        name="csv",
        workflow_id=endpoint.workflow_id,
        source_type="csv",
        source_id="x",
        user_id=endpoint.created_by,
        organization_id=endpoint.organization_id,
    )
    assert await campaign_bills_per_call(ordinary.id) is False


# ---------------------------------------------------------------------------
# The real dispatcher and status processor, with only telephony faked
# ---------------------------------------------------------------------------


def _dispatcher_patches(dispatcher_module, provider, quota_ok=True):
    # The dispatcher's own references are patched (not the shared singletons),
    # so a fake left behind by another test module can't leak in.
    fake_rate_limiter = SimpleNamespace(
        pool_slot_address=lambda slot: slot,
        store_workflow_from_number_mapping=AsyncMock(),
        release_from_number=AsyncMock(),
    )
    fake_concurrency = SimpleNamespace(
        bind_workflow_run=AsyncMock(),
        release_workflow_run_slot=AsyncMock(),
        release_slot=AsyncMock(),
    )
    quota = SimpleNamespace(
        has_quota=quota_ok, error_message="Insufficient wallet balance"
    )
    return [
        patch.object(
            dispatcher_module.CampaignCallDispatcher,
            "get_provider_for_campaign",
            AsyncMock(return_value=provider),
        ),
        patch.object(
            dispatcher_module.CampaignCallDispatcher,
            "acquire_from_number",
            AsyncMock(return_value="+918065607348"),
        ),
        patch.object(
            dispatcher_module.CampaignCallDispatcher,
            "release_call_slot",
            AsyncMock(return_value=True),
        ),
        patch.object(dispatcher_module, "rate_limiter", fake_rate_limiter),
        patch.object(dispatcher_module, "call_concurrency", fake_concurrency),
        patch.object(
            dispatcher_module,
            "circuit_breaker",
            SimpleNamespace(record_and_evaluate=AsyncMock()),
        ),
        patch.object(
            dispatcher_module,
            "authorize_workflow_run_start",
            AsyncMock(return_value=quota),
        ),
        patch.object(
            dispatcher_module,
            "get_backend_endpoints",
            AsyncMock(return_value=("https://x", "wss://x")),
        ),
    ]


@pytest.mark.asyncio
async def test_the_real_dispatcher_marks_the_lead_calling(env, no_kick):
    from api.services.campaign import campaign_call_dispatcher as dispatcher_module

    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    (queued_run,) = await _queued_runs(env, campaign.id)

    provider = SimpleNamespace(
        PROVIDER_NAME="twilio",
        WEBHOOK_ENDPOINT="twiml",
        initiate_call=AsyncMock(
            return_value=SimpleNamespace(call_id="CA1", provider_metadata={})
        ),
    )
    patches = _dispatcher_patches(dispatcher_module, provider)
    for p in patches:
        p.start()
    try:
        run = await dispatcher_module.campaign_call_dispatcher.dispatch_call(
            queued_run, campaign, "slot"
        )
    finally:
        for p in patches:
            p.stop()

    assert (
        run.initial_context["webhook_lead_id"]
        == queued_run.context_variables["webhook_lead_id"]
    )
    assert provider.initiate_call.await_args.kwargs["to_number"] == "+919876543210"
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.call_attempts, lead.last_workflow_run_id) == (
        "calling",
        1,
        run.id,
    )

    # The telephony provider then reports no answer, through the real processor.
    from api.services.telephony import status_processor

    with (
        patch.object(
            status_processor.campaign_call_dispatcher, "release_call_slot", AsyncMock()
        ),
        patch.object(
            status_processor.circuit_breaker, "record_and_evaluate", AsyncMock()
        ),
        patch.object(
            status_processor.rate_limiter,
            "claim_not_connected_report",
            AsyncMock(return_value=True),
        ),
        patch.object(
            status_processor,
            "get_campaign_event_publisher",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch.object(
            status_processor, "_enqueue_integrations_for_unconnected_run", AsyncMock()
        ),
    ):
        await status_processor._process_status_update(
            run.id,
            status_processor.StatusCallbackRequest(call_id="CA1", status="no-answer"),
        )
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.last_call_status) == ("no_answer", "no_answer")


# (The empty-wallet case runs through the real batch task in
# test_webhook_sync_edge_cases.py.)


@pytest.mark.asyncio
async def test_calling_paused_by_the_circuit_breaker_can_be_resumed(env, no_kick):
    from api.routes import webhook_sync as routes

    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    # The breaker trips: the campaign pauses while the endpoint stays active.
    await db_client.update_campaign(campaign_id=campaign.id, state="paused")
    a = env.orgs["a"].user
    backend = patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    )
    with backend:
        shown = await routes.get_endpoint(endpoint.id, user=a)
        assert shown.calling_state == "paused" and shown.is_active is True
        resumed = await routes.resume_calling(endpoint.id, user=a)
    assert resumed.calling_state == "running"
    no_kick.assert_awaited()
    audit = await routes.list_audit_log(
        endpoint_id=endpoint.id, limit=10, offset=0, user=a
    )
    assert audit.entries[0].action == "calling_resumed"


# ---------------------------------------------------------------------------
# Phase 4: the call result goes back to the CRM
# ---------------------------------------------------------------------------

from api.services.webhook_sync import callback  # noqa: E402

CALLBACK = "https://crm.example.com/botrix/result"


@pytest.fixture
def public_dns():
    """crm.example.com resolves to a public address."""

    async def resolve(host, port, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    loop = SimpleNamespace(getaddrinfo=resolve)
    with patch.object(callback.asyncio, "get_running_loop", return_value=loop):
        yield


@pytest.mark.parametrize(
    "status, attempts, final",
    [
        ("completed", 1, True),
        ("failed", 1, True),
        ("no_answer", 1, False),  # a retry follows
        ("no_answer", 2, True),  # the last of 2 attempts
        ("busy", 1, False),
        ("calling", 1, False),
        ("queued", 0, False),
    ],
    ids=[
        "completed",
        "failed",
        "no-answer-retry",
        "no-answer-last",
        "busy-retry",
        "calling",
        "queued",
    ],
)
def test_only_a_final_outcome_is_sent(status, attempts, final):
    settings = _settings(
        retries={"max_attempts": 2, "on_statuses": ["no_answer", "busy"]}
    )
    assert callback.is_final(status, attempts, settings) is final


def test_busy_is_final_when_busy_is_not_retried():
    settings = _settings(retries={"max_attempts": 3, "on_statuses": ["no_answer"]})
    assert callback.is_final("busy", 1, settings) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://crm.example.com/hook",
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/hook",
    ],
    ids=["http", "loopback", "private", "metadata", "localhost"],
)
async def test_callback_urls_must_be_public_https(url):
    with pytest.raises(callback.UnsafeCallbackUrl):
        await callback.ensure_public_callback_url(url)


@pytest.mark.asyncio
async def test_a_hostname_resolving_to_a_private_address_is_refused():
    async def resolve(host, port, **kw):
        return [(2, 1, 6, "", ("10.1.2.3", port))]

    loop = SimpleNamespace(getaddrinfo=resolve)
    with patch.object(callback.asyncio, "get_running_loop", return_value=loop):
        with pytest.raises(callback.UnsafeCallbackUrl):
            await callback.ensure_public_callback_url("https://internal.example.com/x")


@pytest.mark.asyncio
async def test_a_public_callback_url_passes(public_dns):
    await callback.ensure_public_callback_url(CALLBACK)


async def _called_lead(env, steps, gathered=None):
    """An endpoint with a callback URL and one lead taken through ``steps``
    (each "completed", or a telephony status such as "no-answer")."""
    endpoint = await _open_hours_endpoint(env)
    settings = {**endpoint.call_settings, "callback_url": CALLBACK}
    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    body = {"name": "Asha", "mobile": "9876543210", "lead_id": "LSQ-9"}
    await post(endpoint, body, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    org = endpoint.organization_id
    run = None
    for step in steps:
        run = await db_client.create_workflow_run(
            name="WR",
            workflow_id=endpoint.workflow_id,
            mode="twilio",
            user_id=endpoint.created_by,
            initial_context=queued_run.context_variables,
            gathered_context=gathered if step == "completed" else None,
            campaign_id=endpoint.campaign_id,
            queued_run_id=queued_run.id,
            organization_id=org,
        )
        await calling.call_started(queued_run, run, org)
        if step == "completed":
            await calling.call_finished(run.id)
        else:
            await calling.call_not_connected(run, step)
    return endpoint, run


@pytest.mark.asyncio
async def test_a_connected_call_sends_its_result_to_the_crm(env, no_kick, public_dns):
    enqueue = AsyncMock()
    gathered = {
        "mapped_call_disposition": "Interested",
        "extracted_variables": {"budget": "50L"},
    }
    with patch("api.tasks.arq.enqueue_job", enqueue):
        endpoint, run = await _called_lead(env, ["completed"], gathered=gathered)
    (lead,) = await env.leads(endpoint)
    delivery = await db_client.get_webhook_lead_callback(
        lead.id, endpoint.organization_id
    )
    assert delivery.endpoint_url == CALLBACK
    assert delivery.workflow_run_id == run.id
    assert delivery.custom_headers == [
        {"key": "X-Botrix-Secret", "value": endpoint.secret}
    ]
    payload = delivery.payload
    assert payload["event"] == "lead.call_result"
    assert payload["lead"] == {
        "id": lead.id,
        "external_lead_id": "LSQ-9",
        "name": "Asha",
        "phone": "+919876543210",
        "email": None,
        "source": None,
    }
    assert payload["result"]["status"] == "completed"
    assert payload["result"]["disposition"] == "Interested"
    assert payload["result"]["extracted"] == {"budget": "50L"}
    assert payload["call"]["id"] == run.id
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_retry_left_means_no_callback_until_the_last_attempt(
    env, no_kick, public_dns
):
    enqueue = AsyncMock()
    with patch("api.tasks.arq.enqueue_job", enqueue):
        endpoint, _ = await _called_lead(env, ["no-answer"])
        (lead,) = await env.leads(endpoint)
        org = endpoint.organization_id
        assert await db_client.get_webhook_lead_callback(lead.id, org) is None
        enqueue.assert_not_awaited()

        # Two attempts, both unanswered: the second is the last, so it's final.
        endpoint, run = await _called_lead(env, ["no-answer", "no-answer"])
    (lead,) = await env.leads(endpoint)
    delivery = await db_client.get_webhook_lead_callback(
        lead.id, endpoint.organization_id
    )
    assert delivery.payload["result"]["status"] == "no_answer"
    assert delivery.payload["result"]["call_attempts"] == 2
    assert delivery.workflow_run_id == run.id


@pytest.mark.asyncio
async def test_no_callback_url_means_no_callback(env, no_kick, public_dns):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert await callback.send_lead_result(lead.id, endpoint.organization_id) is None


@pytest.mark.asyncio
async def test_the_callback_is_sent_once_even_if_the_hook_repeats(
    env, no_kick, public_dns
):
    enqueue = AsyncMock()
    with patch("api.tasks.arq.enqueue_job", enqueue):
        endpoint, _ = await _called_lead(env, ["completed"])
        (lead,) = await env.leads(endpoint)
        again = await callback.send_lead_result(lead.id, endpoint.organization_id)
    assert again is None
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_saving_a_private_callback_url_is_refused(env, no_kick):
    from fastapi import HTTPException

    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointUpdateRequest

    endpoint = await _open_hours_endpoint(env)
    request = WebhookEndpointUpdateRequest(
        call_settings=CallSettings(callback_url="https://10.0.0.5/x")
    )
    with pytest.raises(HTTPException) as exc:
        await routes.update_endpoint(endpoint.id, request, user=env.orgs["a"].user)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_the_lead_page_shows_its_callback(env, no_kick, public_dns):
    from api.routes import webhook_sync as routes

    with patch("api.tasks.arq.enqueue_job", AsyncMock()):
        endpoint, _ = await _called_lead(env, ["completed"])
    (lead,) = await env.leads(endpoint)
    with patch(
        "api.routes.webhook_sync.should_mask_phone_numbers",
        AsyncMock(return_value=False),
    ):
        shown = await routes.get_lead(lead.id, user=env.orgs["a"].user)
    assert shown.callback is not None
    assert (shown.callback.status, shown.callback.attempts) == ("pending", 0)


# ---------------------------------------------------------------------------
# Phase 4: stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_count_leads_calls_connects_speed_errors_and_callbacks(
    env, no_kick, public_dns
):
    from api.routes import webhook_sync as routes

    with patch("api.tasks.arq.enqueue_job", AsyncMock()):
        endpoint, _ = await _called_lead(env, ["completed"])
        org = endpoint.organization_id
        # A second lead, called and busy (a retry is left, so no callback).
        await post(endpoint, {"mobile": "9123456780"}, key_headers(endpoint))
        second = [
            lead for lead in await env.leads(endpoint) if lead.phone == "+919123456780"
        ][0]
        run = await db_client.create_workflow_run(
            name="WR",
            workflow_id=endpoint.workflow_id,
            mode="twilio",
            user_id=endpoint.created_by,
            initial_context={"webhook_lead_id": second.id},
            campaign_id=endpoint.campaign_id,
            organization_id=org,
        )
        queued = SimpleNamespace(context_variables={"webhook_lead_id": second.id})
        await calling.call_started(queued, run, org)
        await calling.call_not_connected(run, "busy")
    await post(endpoint, {"mobile": "9123456780"}, key_headers(endpoint))  # duplicate
    await post(endpoint, {"mobile": "12345"}, key_headers(endpoint))  # invalid
    await post(endpoint, {"mobile": "9000000001"}, {"X-API-Key": "wrong"})  # 401

    user = env.orgs["a"].user
    stats = await routes.sync_stats(endpoint_id=endpoint.id, days=30, user=user)
    assert (stats.leads, stats.callable, stats.called, stats.connected) == (4, 2, 2, 1)
    assert stats.connect_rate == 0.5
    assert stats.median_seconds_to_first_call is not None
    assert stats.median_seconds_to_first_call >= 0
    assert (stats.requests, stats.request_errors) == (5, 1)
    assert stats.callbacks == {"pending": 1}

    # Scoped: another organization sees none of it.
    theirs = await routes.sync_stats(endpoint_id=None, days=30, user=env.orgs["b"].user)
    assert theirs.leads == 0 and theirs.requests == 0
