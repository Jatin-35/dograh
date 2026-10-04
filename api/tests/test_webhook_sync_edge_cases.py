# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.
# ruff: noqa: F811
"""Webhook Sync edge cases: every way a call can fail to go out, the sweep's
limits, calling-hour boundaries, repeated and out-of-order call events,
settings changing mid-flight, deleted endpoints, callback URL tricks,
timezone-correct "today" counts, and awkward CRM data.

Runs on the real test Postgres and Redis with the production code; only the
telephony provider's REST call is faked.
"""

import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from api.db import db_client
from api.db.models import WorkflowModel
from api.db.webhook_sync_models import WebhookLeadModel
from api.schemas.webhook_sync import CallSettings
from api.services.campaign.rate_limiter import rate_limiter
from api.schemas.organization_preferences import OrganizationPreferences
from api.services.organization_preferences import upsert_organization_preferences
from api.services.telephony import status_processor
from api.services.webhook_sync import calling, callback
from api.services.webhook_sync.management import start_of_today
from api.tasks import campaign_tasks
from api.tasks.campaign_tasks import process_campaign_batch
from api.tests.test_webhook_sync_calling import _open_hours_endpoint, _queued_runs
from api.tests.test_webhook_sync_engine import (  # noqa: F401
    _add_numbers,
    _fake_provider,
    fresh_redis_clients,
)
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def no_kick():
    with patch.object(calling, "_kick", AsyncMock()):
        yield


async def _run_batch(
    endpoint, provider, *, quota_ok=True, workflow_missing=False, provider_error=None
):
    """Run the real batch task for the endpoint's campaign; returns log lines."""
    from loguru import logger

    dispatcher = campaign_tasks.campaign_call_dispatcher
    globs = type(dispatcher).dispatch_call.__globals__
    quota = SimpleNamespace(
        has_quota=quota_ok,
        error_message=None if quota_ok else "Insufficient wallet balance",
    )
    provider_patch = (
        AsyncMock(side_effect=provider_error)
        if provider_error
        else AsyncMock(return_value=provider)
    )
    overrides = {
        "authorize_workflow_run_start": AsyncMock(return_value=quota),
        # Another test module leaves a MagicMock here; use the real one.
        "rate_limiter": rate_limiter,
    }
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}")
    try:
        with (
            patch.object(dispatcher, "get_provider_for_campaign", provider_patch),
            patch.dict(globs, overrides),
            patch.object(
                globs["db_client"],
                "get_workflow_by_id",
                AsyncMock(return_value=None),
            )
            if workflow_missing
            else nullcontext(),
        ):
            await process_campaign_batch({}, endpoint.campaign_id, 10)
    finally:
        logger.remove(sink)
    return lines


async def _endpoint_with_lead(env, body=None, **overrides):
    await _add_numbers(env, overrides.pop("org_key", "a"), "+918065607348")
    endpoint = await _open_hours_endpoint(env, **overrides)
    await post(
        endpoint,
        body or {"name": "Asha", "mobile": "9876543210"},
        key_headers(endpoint),
    )
    return await db_client.get_webhook_endpoint(endpoint.id, endpoint.organization_id)


# ---------------------------------------------------------------------------
# Every way a call can fail to go out ends with the lead showing why
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_wallet_fails_the_lead_with_the_reason(env, no_kick):
    endpoint = await _endpoint_with_lead(env)
    provider, calls = _fake_provider(["+918065607348"])
    await _run_batch(endpoint, provider, quota_ok=False)
    assert calls == []
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason) == (
        "failed",
        "Insufficient wallet balance",
    )
    (run,) = await _queued_runs(env, endpoint.campaign_id)
    assert run.state == "failed"


@pytest.mark.asyncio
async def test_a_deleted_agent_fails_the_lead_instead_of_leaving_it_queued(
    env, no_kick
):
    endpoint = await _endpoint_with_lead(env)
    provider, calls = _fake_provider(["+918065607348"])
    await _run_batch(endpoint, provider, workflow_missing=True)
    assert calls == []
    (lead,) = await env.leads(endpoint)
    assert lead.status == "failed"
    assert "not found" in lead.status_reason


@pytest.mark.asyncio
async def test_a_provider_error_fails_the_lead_with_the_providers_message(env, no_kick):
    endpoint = await _endpoint_with_lead(env)
    provider, _ = _fake_provider(["+918065607348"])

    async def refuse(**kwargs):
        raise RuntimeError("Carrier rejected the call: DID not allowed")

    provider.initiate_call = refuse
    await _run_batch(endpoint, provider)
    (lead,) = await env.leads(endpoint)
    assert lead.status == "failed"
    assert "DID not allowed" in lead.status_reason
    # It got as far as a call record, so the lead links to it.
    assert lead.last_workflow_run_id is not None and lead.call_attempts == 1


@pytest.mark.asyncio
async def test_missing_telephony_setup_fails_the_lead(env, no_kick):
    endpoint = await _endpoint_with_lead(env)
    await _run_batch(
        endpoint,
        None,
        provider_error=ValueError("No telephony configuration for this campaign"),
    )
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason) == (
        "failed",
        "No telephony configuration for this campaign",
    )


@pytest.mark.asyncio
async def test_one_bad_lead_does_not_stop_the_rest_of_the_batch(env, no_kick):
    # Two caller numbers, so the second call never waits on the first's
    # number being released (not what this test is about).
    await _add_numbers(env, "a", "+917921731184")
    endpoint = await _endpoint_with_lead(env)
    await post(
        endpoint, {"name": "Ravi", "mobile": "9123456780"}, key_headers(endpoint)
    )
    provider, calls = _fake_provider(["+918065607348", "+917921731184"])
    real_initiate = provider.initiate_call

    async def initiate(**kwargs):
        if kwargs["to_number"] == "+919876543210":
            raise RuntimeError("temporary carrier error")
        return await real_initiate(**kwargs)

    provider.initiate_call = initiate
    await _run_batch(endpoint, provider)
    leads = {lead.phone: lead for lead in await env.leads(endpoint)}
    assert leads["+919876543210"].status == "failed"
    assert leads["+919123456780"].status == "calling"
    assert [c["to_number"] for c in calls] == ["+919123456780"]


# ---------------------------------------------------------------------------
# The sweep only rescues fresh leads, and never queues one twice
# ---------------------------------------------------------------------------


async def _age_lead(env, lead_id, delta):
    async with env.factory() as session:
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.id == lead_id)
            .values(received_at=datetime.now(UTC) - delta)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_turning_auto_call_on_later_does_not_call_old_leads(env, no_kick):
    endpoint = await _open_hours_endpoint(env, auto_call=False)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))  # 3 days old
    await post(
        endpoint, {"mobile": "9123456780"}, key_headers(endpoint)
    )  # 5 minutes old
    old, fresh = await env.leads(endpoint)
    await _age_lead(env, old.id, timedelta(days=3))
    await _age_lead(env, fresh.id, timedelta(minutes=5))

    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, auto_call=True
    )
    await calling.enqueue_stragglers()

    old, fresh = await env.leads(endpoint)
    assert old.status == "received" and old.queued_run_id is None
    assert fresh.status == "queued"


@pytest.mark.asyncio
async def test_the_sweep_and_the_receiver_racing_queue_a_lead_once(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    with patch(
        "api.services.webhook_sync.receiver.enqueue_new_leads",
        AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    results = await asyncio.gather(
        *[calling.enqueue_new_leads(endpoint, [lead.id]) for _ in range(8)],
        calling.enqueue_stragglers(older_than_seconds=-5),
    )
    assert sum(results) == 1
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert len(await _queued_runs(env, endpoint.campaign_id)) == 1


# ---------------------------------------------------------------------------
# Calling-hour boundaries
# ---------------------------------------------------------------------------


def _hours(start="09:00", end="21:00", days=(0, 1, 2, 3, 4, 5, 6), tz="Asia/Kolkata"):
    return CallSettings.model_validate(
        {
            "calling_hours": {
                "start": start,
                "end": end,
                "timezone": tz,
                "days": list(days),
            }
        }
    )


@pytest.mark.parametrize(
    "local, expected",
    [
        ("2026-09-28 09:00", "2026-09-28 09:00"),  # exactly at opening: now
        ("2026-09-28 08:59", "2026-09-28 09:00"),
        ("2026-09-28 20:59", "2026-09-28 20:59"),
        ("2026-09-28 21:00", "2026-09-29 09:00"),  # exactly at closing: tomorrow
        ("2026-09-28 23:59", "2026-09-29 09:00"),
    ],
    ids=["at-open", "before-open", "last-minute", "at-close", "midnight-ish"],
)
def test_calling_hour_boundaries(local, expected):
    now = datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    want = datetime.strptime(expected, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    assert calling.next_call_time(_hours(), now) == want


def test_sunday_only_after_closing_waits_a_week():
    sunday_night = datetime(2026, 9, 27, 22, 0, tzinfo=IST)  # a Sunday
    assert calling.next_call_time(_hours(days=[6]), sunday_night) == datetime(
        2026, 10, 4, 9, 0, tzinfo=IST
    )


def test_calling_hours_in_another_timezone():
    new_york = ZoneInfo("America/New_York")
    # 06:00 in New York; hours 09:00-17:00 there.
    now = datetime(2026, 9, 28, 6, 0, tzinfo=new_york)
    got = calling.next_call_time(_hours("09:00", "17:00", tz="America/New_York"), now)
    assert got == datetime(2026, 9, 28, 9, 0, tzinfo=new_york)


@pytest.mark.asyncio
async def test_a_delay_that_runs_past_closing_waits_for_the_next_day(env, no_kick):
    endpoint = await env.make_endpoint(
        auto_call=True,
        call_settings={
            "delay_minutes": 1440,  # a day later lands after tomorrow's 00:01
            "calling_hours": {
                "start": "00:00",
                "end": "00:01",
                "timezone": "Asia/Kolkata",
            },
        },
    )
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert lead.status == "scheduled"
    local = lead.next_retry_at.astimezone(IST)
    assert (local.hour, local.minute) == (0, 0)
    assert lead.next_retry_at > datetime.now(UTC) + timedelta(hours=23)


# ---------------------------------------------------------------------------
# Repeated, out-of-order and mid-flight changes
# ---------------------------------------------------------------------------


async def _called(env, endpoint, gathered=None):
    (queued_run,) = [
        r for r in await _queued_runs(env, endpoint.campaign_id) if r.retry_count == 0
    ]
    run = await db_client.create_workflow_run(
        name="WR",
        workflow_id=endpoint.workflow_id,
        mode="twilio",
        user_id=endpoint.created_by,
        initial_context=queued_run.context_variables,
        gathered_context=gathered,
        campaign_id=endpoint.campaign_id,
        queued_run_id=queued_run.id,
        organization_id=endpoint.organization_id,
    )
    await calling.call_started(queued_run, run, endpoint.organization_id)
    return run


def _public_dns():
    async def resolve(host, port, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    return patch.object(
        callback.asyncio,
        "get_running_loop",
        return_value=SimpleNamespace(getaddrinfo=resolve),
    )


@pytest.mark.asyncio
async def test_a_provider_reporting_the_same_no_answer_twice_counts_once(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    settings = {**endpoint.call_settings, "callback_url": "https://crm.example.com/r"}
    settings["retries"] = {
        "max_attempts": 1,
        "gap_minutes": 10,
        "on_statuses": ["no_answer"],
    }
    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    run = await _called(env, endpoint)
    enqueue = AsyncMock()
    with _public_dns(), patch("api.tasks.arq.enqueue_job", enqueue):
        for _ in range(2):
            await status_processor._process_status_update(
                run.id,
                status_processor.StatusCallbackRequest(call_id="x", status="no-answer"),
            )
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.call_attempts) == ("no_answer", 1)
    # One attempt allowed, so it's final: the CRM hears exactly once.
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_lowering_attempts_mid_flight_makes_the_current_outcome_final(
    env, no_kick
):
    endpoint = await _open_hours_endpoint(env)  # 2 attempts
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    run = await _called(env, endpoint)
    settings = {
        **endpoint.call_settings,
        "callback_url": "https://crm.example.com/r",
        "retries": {"max_attempts": 1, "gap_minutes": 10, "on_statuses": ["no_answer"]},
    }
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    with _public_dns(), patch("api.tasks.arq.enqueue_job", AsyncMock()):
        await calling.call_not_connected(run, "no-answer")
    (lead,) = await env.leads(endpoint)
    assert (
        await db_client.get_webhook_lead_callback(lead.id, endpoint.organization_id)
        is not None
    )


@pytest.mark.asyncio
async def test_events_for_a_deleted_endpoints_lead_are_ignored_quietly(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    run = await _called(env, endpoint)
    await calling.stop_endpoint_campaign(endpoint)
    await db_client.delete_webhook_endpoint(endpoint.id, endpoint.organization_id)
    # The call still ends; nothing to update, and nothing raises.
    await calling.call_not_connected(run, "busy")
    await calling.call_finished(run.id)
    assert await callback.send_lead_result(0, endpoint.organization_id) is None


@pytest.mark.asyncio
async def test_pausing_stops_dispatch_and_resuming_calls_the_waiting_leads(
    env, no_kick
):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointUpdateRequest

    endpoint = await _endpoint_with_lead(env)
    backend = patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    )
    user = env.orgs["a"].user
    provider, calls = _fake_provider(["+918065607348"])
    with backend:
        await routes.update_endpoint(
            endpoint.id, WebhookEndpointUpdateRequest(is_active=False), user=user
        )
        await _run_batch(endpoint, provider)
        assert calls == []
        (lead,) = await env.leads(endpoint)
        assert lead.status == "queued"

        await routes.update_endpoint(
            endpoint.id, WebhookEndpointUpdateRequest(is_active=True), user=user
        )
        await _run_batch(endpoint, provider)
    assert [c["to_number"] for c in calls] == ["+919876543210"]


@pytest.mark.asyncio
async def test_changing_the_agent_changes_who_calls_the_waiting_leads(env, no_kick):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointUpdateRequest

    endpoint = await _endpoint_with_lead(env)
    a = env.orgs["a"]
    async with env.factory() as session:
        other = WorkflowModel(
            name="agent-2",
            user_id=a.user.id,
            organization_id=a.org,
            workflow_definition={"nodes": [], "edges": []},
            template_context_variables={},
        )
        session.add(other)
        await session.flush()
        other_id = other.id
        await session.commit()
    with patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    ):
        await routes.update_endpoint(
            endpoint.id, WebhookEndpointUpdateRequest(workflow_id=other_id), user=a.user
        )
    provider, calls = _fake_provider(["+918065607348"])
    await _run_batch(endpoint, provider)
    (lead,) = await env.leads(endpoint)
    run = await db_client.get_workflow_run_by_id(lead.last_workflow_run_id)
    assert run.workflow_id == other_id
    assert calls[0]["workflow_id"] == other_id


# ---------------------------------------------------------------------------
# Callback URL tricks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved",
    [
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::1",
        "fd00::1",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
    ],
    ids=[
        "mapped-loopback",
        "mapped-metadata",
        "ipv6-loopback",
        "ipv6-private",
        "unspecified",
        "cgnat",
        "multicast",
    ],
)
async def test_hostnames_resolving_to_non_public_addresses_are_refused(resolved):
    async def resolve(host, port, **kw):
        return [(10 if ":" in resolved else 2, 1, 6, "", (resolved, port))]

    loop = SimpleNamespace(getaddrinfo=resolve)
    with patch.object(callback.asyncio, "get_running_loop", return_value=loop):
        with pytest.raises(callback.UnsafeCallbackUrl):
            await callback.ensure_public_callback_url("https://sneaky.example.com/hook")


@pytest.mark.asyncio
async def test_one_private_address_among_public_ones_is_refused():
    async def resolve(host, port, **kw):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("10.0.0.7", port)),
        ]

    loop = SimpleNamespace(getaddrinfo=resolve)
    with patch.object(callback.asyncio, "get_running_loop", return_value=loop):
        with pytest.raises(callback.UnsafeCallbackUrl):
            await callback.ensure_public_callback_url("https://split.example.com/hook")


@pytest.mark.asyncio
async def test_an_unresolvable_callback_host_is_refused():
    with pytest.raises(callback.UnsafeCallbackUrl):
        await callback.ensure_public_callback_url(
            "https://no-such-host-for-botrix-tests.invalid/hook"
        )


@pytest.mark.asyncio
async def test_removing_the_callback_url_before_the_result_sends_nothing(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    org = endpoint.organization_id
    with_url = {**endpoint.call_settings, "callback_url": "https://crm.example.com/r"}
    await db_client.update_webhook_endpoint(endpoint.id, org, call_settings=with_url)
    endpoint = await db_client.get_webhook_endpoint(endpoint.id, org)
    run = await _called(
        env, endpoint, gathered={"mapped_call_disposition": "Interested"}
    )
    # The client removes the URL while the call is still going.
    without = {**with_url, "callback_url": None}
    await db_client.update_webhook_endpoint(endpoint.id, org, call_settings=without)
    enqueue = AsyncMock()
    with _public_dns(), patch("api.tasks.arq.enqueue_job", enqueue):
        await calling.call_finished(run.id)
    enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stats and "today"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_for_an_org_with_nothing_yet(env):
    from api.routes import webhook_sync as routes

    stats = await routes.sync_stats(endpoint_id=None, days=30, user=env.orgs["b"].user)
    assert (stats.leads, stats.called, stats.connected, stats.requests) == (0, 0, 0, 0)
    assert stats.connect_rate is None and stats.median_seconds_to_first_call is None
    assert stats.callbacks == {}


@pytest.mark.asyncio
async def test_today_starts_at_midnight_in_the_organizations_timezone(env, no_kick):
    from api.routes import webhook_sync as routes

    a = env.orgs["a"]
    start = await start_of_today(a.org)  # no preference: India time
    assert start.astimezone(IST).time().isoformat() == "00:00:00"

    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    await post(endpoint, {"mobile": "9123456780"}, key_headers(endpoint))
    yesterday, today = await env.leads(endpoint)
    # One lead a minute before midnight, India time.
    await _age_lead(
        env, yesterday.id, datetime.now(UTC) - (start - timedelta(minutes=1))
    )
    with patch(
        "api.routes.webhook_sync.should_mask_phone_numbers",
        AsyncMock(return_value=False),
    ):
        counts = await routes.lead_stats(endpoint_id=endpoint.id, user=a.user)
    assert (counts.today, counts.total) == (1, 2)

    await upsert_organization_preferences(
        a.org, OrganizationPreferences(timezone="America/Los_Angeles")
    )
    la = await start_of_today(a.org)
    assert (
        la.astimezone(ZoneInfo("America/Los_Angeles")).time().isoformat() == "00:00:00"
    )


# ---------------------------------------------------------------------------
# Awkward CRM data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hindi_and_emoji_survive_storage_calling_and_the_callback(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(
        endpoint,
        {"name": "राहुल कुमार 🙏", "mobile": "9876543210", "city": "पटना"},
        key_headers(endpoint),
    )
    (lead,) = await env.leads(endpoint)
    assert lead.name == "राहुल कुमार 🙏"
    assert lead.variables["city"] == "पटना"
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    assert queued_run.context_variables["name"] == "राहुल कुमार 🙏"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent",
    [9876543210, 9876543210.0, 919876543210],
    ids=["int", "float", "int-with-91"],
)
async def test_a_phone_sent_as_a_json_number_is_read(env, no_kick, sent):
    endpoint = await _open_hours_endpoint(env)
    result = await post(endpoint, {"mobile": sent}, key_headers(endpoint))
    assert result.status_code == 200
    (lead,) = await env.leads(endpoint)
    assert (lead.phone, lead.status) == ("+919876543210", "queued")


@pytest.mark.asyncio
async def test_non_indian_and_landline_numbers_are_stored_but_never_queued(
    env, no_kick
):
    endpoint = await _open_hours_endpoint(env)
    for number in ("+14155552671", "02212345678", "5876543210", "98765"):
        await post(endpoint, {"mobile": number}, key_headers(endpoint))
    leads = await env.leads(endpoint)
    assert {lead.status for lead in leads} == {"invalid_number"}
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert endpoint.campaign_id is None  # nothing callable, so no campaign yet
