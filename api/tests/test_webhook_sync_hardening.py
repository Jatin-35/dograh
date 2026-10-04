# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.
# ruff: noqa: F811
"""Webhook Sync hardening (2026-10-02 review): leads are never lost and
"don't call me" is honoured.

- A corrected number or a returning customer (same CRM lead id) is called.
- A paused endpoint stores leads and calls them when resumed.
- Turning auto-call off takes waiting calls off the queue.
- "Stop calling", or opting out during the call, blocks the number in the
  whole organization.
- The endpoint's campaign can't be stopped from the Campaigns page.
- Leads stuck in "calling" are settled.
- Alerts are recorded (and emailed) when an endpoint needs attention.

Runs on the real test Postgres and Redis with the production code.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from api.db import db_client
from api.db.models import QueuedRunModel, WebhookDeliveryModel
from api.db.webhook_sync_models import (
    WebhookEndpointAuditLogModel,
    WebhookLeadModel,
)
from api.schemas.webhook_sync import CallSettings
from api.services.campaign.continuous import (
    is_managed_elsewhere,
    reject_if_managed_elsewhere,
)
from api.services.notifications import email as email_module
from api.services.webhook_sync import alerts, calling
from api.tests.test_webhook_sync_calling import _open_hours_endpoint, _queued_runs
from api.tests.test_webhook_sync_engine import fresh_redis_clients  # noqa: F401
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401


@pytest.fixture
def no_kick():
    with patch.object(calling, "_kick", AsyncMock()):
        yield


@pytest.fixture
def no_alert_email():
    """Alerts are recorded for real; the email itself is captured."""
    sent = []

    async def fake_send(recipients, subject, body):
        sent.append((list(recipients), subject, body))
        return True

    with patch.object(alerts, "send_email", fake_send):
        yield sent


async def _reload(endpoint):
    return await db_client.get_webhook_endpoint(endpoint.id, endpoint.organization_id)


async def _waiting_endpoint(env, **overrides):
    """Calls wait an hour, so a lead stays queued while the test acts."""
    endpoint = await _open_hours_endpoint(env, **overrides)
    settings = {**endpoint.call_settings, "delay_minutes": 60}
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    return await _reload(endpoint)


async def _run_state(env, run_id):
    async with env.factory() as session:
        return (
            await session.execute(
                select(QueuedRunModel.state).where(QueuedRunModel.id == run_id)
            )
        ).scalar_one()


async def _age_leads(env, endpoint, **delta):
    async with env.factory() as session:
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.endpoint_id == endpoint.id)
            .values(received_at=datetime.now(UTC) - timedelta(**delta))
        )
        await session.commit()


async def _alerts(env, endpoint):
    async with env.factory() as session:
        result = await session.execute(
            select(WebhookEndpointAuditLogModel)
            .where(
                WebhookEndpointAuditLogModel.endpoint_id == endpoint.id,
                WebhookEndpointAuditLogModel.action == "alert",
            )
            .order_by(WebhookEndpointAuditLogModel.id)
        )
        return list(result.scalars().all())


async def _clear_alert_state(endpoint):
    client = await alerts._get_redis()
    keys = [f"webhook_sync_failed_requests:{endpoint.id}"] + [
        f"webhook_sync_alert:{endpoint.id}:{kind}"
        for kind in (
            alerts.REQUESTS_FAILING,
            alerts.CALLS_NOT_PLACED,
            alerts.CALLING_PAUSED,
            alerts.CALLBACKS_FAILING,
        )
    ]
    await client.delete(*keys)


# ---------------------------------------------------------------------------
# The same CRM lead coming back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_corrected_number_replaces_the_waiting_lead(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(
        endpoint, {"ProspectID": "P-1", "Phone": "9876543210"}, key_headers(endpoint)
    )
    r = await post(
        endpoint, {"ProspectID": "P-1", "Phone": "9123456780"}, key_headers(endpoint)
    )
    assert r.body["created"] == 1 and r.body["replayed"] == 0

    old, new = await env.leads(endpoint)
    assert new.phone == "+919123456780" and new.status == "scheduled"
    assert old.status == "failed"
    assert old.status_reason == f"Replaced by lead #{new.id} with a corrected number"
    # The old number's call is off the queue; the new one's is on it.
    assert await _run_state(env, old.queued_run_id) == "failed"
    assert await _run_state(env, new.queued_run_id) == "queued"


@pytest.mark.asyncio
async def test_a_number_fixed_after_an_invalid_one_is_called(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"ProspectID": "P-2", "Phone": "12345"}, key_headers(endpoint))
    await post(
        endpoint, {"ProspectID": "P-2", "Phone": "9876543210"}, key_headers(endpoint)
    )
    invalid, fixed = await env.leads(endpoint)
    assert invalid.status == "invalid_number"  # kept as it was
    assert (fixed.phone, fixed.status) == ("+919876543210", "queued")
    assert fixed.external_lead_id == "P-2"


@pytest.mark.asyncio
async def test_a_repeat_without_a_new_number_is_still_a_replay(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    body = {"ProspectID": "P-3", "Phone": "9876543210"}
    await post(endpoint, body, key_headers(endpoint))
    r = await post(endpoint, body, key_headers(endpoint))
    # An invalid number in the repeat doesn't replace a good lead either.
    r2 = await post(
        endpoint, {"ProspectID": "P-3", "Phone": "123"}, key_headers(endpoint)
    )
    assert r.body["replayed"] == 1 and r2.body["replayed"] == 1
    assert len(await env.leads(endpoint)) == 1


@pytest.mark.asyncio
async def test_a_returning_customer_is_called_after_the_reenquiry_window(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    body = {"ProspectID": "P-4", "Phone": "9876543210"}
    await post(endpoint, body, key_headers(endpoint))
    await _age_leads(env, endpoint, days=31)
    r = await post(endpoint, body, key_headers(endpoint))
    assert r.body["created"] == 1 and r.body["replayed"] == 0
    old, new = await env.leads(endpoint)
    assert new.status == "queued" and old.id != new.id


@pytest.mark.asyncio
async def test_the_reenquiry_window_is_configurable(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    settings = {**endpoint.call_settings, "reenquiry_days": 7}
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    endpoint = await _reload(endpoint)
    body = {"ProspectID": "P-5", "Phone": "9876543210"}
    await post(endpoint, body, key_headers(endpoint))
    await _age_leads(env, endpoint, days=8)
    r = await post(endpoint, body, key_headers(endpoint))
    assert r.body["created"] == 1


@pytest.mark.asyncio
async def test_simultaneous_repeats_of_one_crm_lead_store_it_once(env, no_kick):
    import asyncio

    endpoint = await _open_hours_endpoint(env)
    body = {"ProspectID": "P-6", "Phone": "9876543210"}
    results = await asyncio.gather(
        *[post(endpoint, body, key_headers(endpoint)) for _ in range(5)]
    )
    assert all(r.status_code == 200 for r in results)
    assert len(await env.leads(endpoint)) == 1


# ---------------------------------------------------------------------------
# A paused endpoint keeps its leads on hold; someone chooses which to call
# ---------------------------------------------------------------------------


async def _paused_with_leads(env, *phones, **overrides):
    endpoint = await _open_hours_endpoint(env, is_active=False, **overrides)
    for phone in phones:
        await post(endpoint, {"mobile": phone}, key_headers(endpoint))
    return endpoint


async def _resume(endpoint):
    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, is_active=True
    )
    await calling.sync_campaign_settings(endpoint)
    return endpoint


@pytest.mark.asyncio
async def test_a_paused_endpoint_stores_leads_on_hold(env, no_kick):
    endpoint = await _open_hours_endpoint(env, is_active=False)
    r = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert r.status_code == 200 and r.body["paused"] is True
    assert (r.body["on_hold"], r.body["created"]) == (1, 0)
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason, lead.queued_run_id) == (
        "on_hold",
        "Received while the endpoint was paused",
        None,
    )
    (log,) = await env.logs(endpoint)
    assert log.response_code == 200 and "on hold" in log.error


@pytest.mark.asyncio
async def test_on_hold_still_honours_opt_outs_duplicates_and_bad_numbers(env, no_kick):
    endpoint = await _open_hours_endpoint(env, is_active=False)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    r = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert r.body["duplicates"] == 1
    r = await post(endpoint, {"mobile": "123"}, key_headers(endpoint))
    assert r.body["invalid"] == 1
    first = (await env.leads(endpoint))[0]
    await calling.stop_calling(endpoint, first)
    r = await post(
        endpoint, {"mobile": "9876543210", "lead_id": "x"}, key_headers(endpoint)
    )
    assert r.body["opted_out"] == 1


@pytest.mark.asyncio
async def test_resuming_calls_nobody_on_its_own(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210", "9123456780")
    endpoint = await _resume(endpoint)
    await calling.enqueue_stragglers(older_than_seconds=0)  # the minute sweep
    assert {lead.status for lead in await env.leads(endpoint)} == {"on_hold"}
    response = await __import__(
        "api.services.webhook_sync.management", fromlist=["endpoint_response"]
    ).endpoint_response(endpoint)
    assert response.leads_on_hold == 2


@pytest.mark.asyncio
async def test_a_paused_endpoint_still_rejects_bad_credentials(env, no_kick):
    endpoint = await _open_hours_endpoint(env, is_active=False)
    r = await post(endpoint, {"mobile": "9876543210"}, {"X-API-Key": "wrong"})
    assert r.status_code == 401 and await env.leads(endpoint) == []


@pytest.mark.asyncio
async def test_calling_chosen_leads_queues_only_those(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210", "9123456780", "9000000001")
    endpoint = await _resume(endpoint)
    a, b, c = await env.leads(endpoint)

    result = await calling.call_leads(endpoint.organization_id, [a.id, c.id])

    assert sorted(result.done) == sorted([a.id, c.id]) and result.not_done == []
    a, b, c = await env.leads(endpoint)
    assert (a.status, b.status, c.status) == ("queued", "on_hold", "queued")
    assert a.status_reason == "Called from the dashboard"
    endpoint = await _reload(endpoint)  # its campaign starts with the first call
    runs = await _queued_runs(env, endpoint.campaign_id)
    assert len(runs) == 2 and all(r.scheduled_for is None for r in runs)


@pytest.mark.asyncio
async def test_called_leads_ignore_the_delay_but_keep_calling_hours(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210")
    hours = {"start": "00:00", "end": "00:01", "timezone": "Asia/Kolkata"}
    settings = {**endpoint.call_settings, "delay_minutes": 60, "calling_hours": hours}
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    endpoint = await _resume(await _reload(endpoint))
    (lead,) = await env.leads(endpoint)
    await calling.call_leads(endpoint.organization_id, [lead.id])
    (lead,) = await env.leads(endpoint)
    endpoint = await _reload(endpoint)
    (run,) = await _queued_runs(env, endpoint.campaign_id)
    assert run.scheduled_for is None  # no "wait before calling"
    now = datetime.now(UTC).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    if not (now.hour == 0 and now.minute == 0):
        assert lead.status == "scheduled"
        assert lead.status_reason == "Waiting for calling hours"


@pytest.mark.asyncio
async def test_skipping_chosen_leads_doesnt_block_their_numbers(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210", "9123456780")
    endpoint = await _resume(endpoint)
    a, b = await env.leads(endpoint)

    result = await calling.skip_leads(endpoint.organization_id, [a.id])

    assert result.done == [a.id]
    a, b = await env.leads(endpoint)
    assert (a.status, a.status_reason, b.status) == (
        "skipped",
        calling.SKIPPED_REASON,
        "on_hold",
    )
    # Not an opt-out: a later enquiry from the number is called as usual.
    await _age_leads(env, endpoint, days=2)
    r = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert r.body["created"] == 1
    # And a skipped lead can still be called after all.
    assert (await calling.call_leads(endpoint.organization_id, [a.id])).done == [a.id]


@pytest.mark.asyncio
async def test_each_lead_is_checked_and_the_reason_given(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210", "9123456780", "9000000001")
    endpoint = await _resume(endpoint)
    ok, opted, calling_now = await env.leads(endpoint)
    await calling.stop_calling(endpoint, opted)  # becomes do_not_call
    async with env.factory() as session:
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.id == calling_now.id)
            .values(status="calling")
        )
        await session.commit()
    other_org = await _open_hours_endpoint(
        env,
        organization_id=env.orgs["b"].org,
        workflow_id=env.orgs["b"].workflow,
        created_by=env.orgs["b"].user.id,
    )
    await post(other_org, {"mobile": "9000000002"}, key_headers(other_org))
    (foreign,) = await env.leads(other_org)
    foreign_status = foreign.status

    result = await calling.call_leads(
        endpoint.organization_id, [ok.id, opted.id, calling_now.id, foreign.id, ok.id]
    )

    assert result.done == [ok.id]  # duplicates in the request count once
    reasons = {item["lead_id"]: item["reason"] for item in result.not_done}
    assert reasons[opted.id] == "A do not call lead can't be called now"
    assert reasons[calling_now.id] == "A calling lead can't be called now"
    assert reasons[foreign.id] == "Lead not found"  # another org's lead
    (foreign,) = await env.leads(other_org)
    assert foreign.status == foreign_status  # untouched


@pytest.mark.asyncio
async def test_an_opted_out_number_on_another_lead_is_refused(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210")
    endpoint = await _resume(endpoint)
    other = await _waiting_endpoint(env)
    await post(other, {"mobile": "9876543210", "lead_id": "z"}, key_headers(other))
    (stopped,) = await env.leads(other)
    await calling.stop_calling(other, stopped)
    # stop_calling opted out the on-hold lead too (same number, same org).
    (held,) = await env.leads(endpoint)
    assert held.status == "do_not_call"


@pytest.mark.asyncio
async def test_calling_leads_on_a_paused_endpoint_is_refused(env, no_kick):
    endpoint = await _paused_with_leads(env, "9876543210")
    (lead,) = await env.leads(endpoint)
    result = await calling.call_leads(endpoint.organization_id, [lead.id])
    assert result.done == [] and result.not_done == [
        {"lead_id": lead.id, "reason": "Resume the endpoint first"}
    ]


@pytest.mark.asyncio
async def test_a_queued_lead_cant_be_skipped(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    result = await calling.skip_leads(endpoint.organization_id, [lead.id])
    assert result.done == [] and "can be skipped" in result.not_done[0]["reason"]


@pytest.mark.asyncio
async def test_bulk_calling_works_with_auto_call_off_and_across_endpoints(env, no_kick):
    first = await _paused_with_leads(env, "9876543210", auto_call=False)
    second = await _paused_with_leads(env, "9123456780", auto_call=False)
    first, second = await _resume(first), await _resume(second)
    (a,) = await env.leads(first)
    (b,) = await env.leads(second)
    result = await calling.call_leads(first.organization_id, [a.id, b.id])
    assert sorted(result.done) == sorted([a.id, b.id])
    first, second = await _reload(first), await _reload(second)
    assert first.campaign_id and second.campaign_id
    assert first.campaign_id != second.campaign_id


@pytest.mark.asyncio
async def test_the_routes_call_and_skip_and_record_the_decision(env, no_kick):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import BulkLeadActionRequest, OnHoldActionRequest

    user = env.orgs["a"].user
    endpoint = await _paused_with_leads(env, "9876543210", "9123456780", "9000000001")
    endpoint = await _resume(endpoint)
    a, b, c = await env.leads(endpoint)

    r = await routes.bulk_lead_action(
        BulkLeadActionRequest(lead_ids=[a.id], action="skip"), user=user
    )
    assert (r.action, r.done) == ("skip", [a.id])

    r = await routes.on_hold_leads_action(
        endpoint.id, OnHoldActionRequest(action="call"), user=user
    )
    assert sorted(r.done) == sorted([b.id, c.id])  # a was skipped, not on hold
    statuses = [lead.status for lead in await env.leads(endpoint)]
    assert statuses == ["skipped", "queued", "queued"]
    from api.db.webhook_sync_models import WebhookEndpointAuditLogModel as Audit

    async with env.factory() as session:
        actions = (
            (
                await session.execute(
                    select(Audit.action).where(Audit.endpoint_id == endpoint.id)
                )
            )
            .scalars()
            .all()
        )
    assert "on_hold_called" in actions

    # Another organization can't act on these leads or this endpoint.
    other = env.orgs["b"].user
    r = await routes.bulk_lead_action(
        BulkLeadActionRequest(lead_ids=[a.id], action="call"), user=other
    )
    assert r.done == [] and r.not_done[0].reason == "Lead not found"
    with pytest.raises(HTTPException) as e:
        await routes.on_hold_leads_action(
            endpoint.id, OnHoldActionRequest(action="skip"), user=other
        )
    assert e.value.status_code == 404


def test_bulk_requests_are_bounded():
    from api.schemas.webhook_sync import BulkLeadActionRequest

    with pytest.raises(Exception):
        BulkLeadActionRequest(lead_ids=[], action="call")
    with pytest.raises(Exception):
        BulkLeadActionRequest(lead_ids=list(range(501)), action="call")
    with pytest.raises(Exception):
        BulkLeadActionRequest(lead_ids=[1], action="stop")


# ---------------------------------------------------------------------------
# Auto-call off really stops calling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_call_off_unqueues_waiting_calls(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    run_id = lead.queued_run_id

    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, auto_call=False
    )
    assert await calling.cancel_waiting_calls(endpoint) == 1

    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason, lead.queued_run_id) == (
        "received",
        "Auto-call turned off",
        None,
    )
    assert await _run_state(env, run_id) == "failed"


@pytest.mark.asyncio
async def test_auto_call_off_keeps_a_retrys_last_outcome(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    async with env.factory() as session:  # one call made, retry booked
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.id == lead.id)
            .values(call_attempts=1, last_call_status="no_answer", status="scheduled")
        )
        await session.commit()
    await calling.cancel_waiting_calls(endpoint)
    (lead,) = await env.leads(endpoint)
    assert lead.status == "no_answer"


@pytest.mark.asyncio
async def test_auto_call_off_leaves_a_call_being_dialled(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    async with env.factory() as session:  # the dispatcher already took it
        await session.execute(
            update(QueuedRunModel)
            .where(QueuedRunModel.id == lead.queued_run_id)
            .values(state="processing")
        )
        await session.commit()
    assert await calling.cancel_waiting_calls(endpoint) == 0
    (lead,) = await env.leads(endpoint)
    assert lead.status == "scheduled"


# ---------------------------------------------------------------------------
# Opting out is for the number, everywhere in the organization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_calling_blocks_the_number_across_the_organization(env, no_kick):
    first = await _waiting_endpoint(env)
    second = await _waiting_endpoint(env)
    other_org = await _waiting_endpoint(
        env,
        organization_id=env.orgs["b"].org,
        workflow_id=env.orgs["b"].workflow,
        created_by=env.orgs["b"].user.id,
    )
    for endpoint in (first, second, other_org):
        await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead_a,) = await env.leads(first)
    (lead_b,) = await env.leads(second)

    await calling.stop_calling(await _reload(first), lead_a)

    (lead_b,) = await env.leads(second)
    assert lead_b.status == "do_not_call"
    assert await _run_state(env, lead_b.queued_run_id) == "failed"
    # A later lead with the number, on any endpoint of the org, isn't called.
    r = await post(second, {"mobile": "+91 98765 43210"}, key_headers(second))
    assert r.body["opted_out"] == 1
    assert (await env.leads(second))[-1].status == "do_not_call"
    # Another organization is not affected.
    (other,) = await env.leads(other_org)
    assert other.status == "scheduled"


@pytest.mark.asyncio
async def test_an_opted_out_number_cant_be_called_again(env, no_kick):
    endpoint = await _waiting_endpoint(env)
    await post(
        endpoint, {"mobile": "9876543210", "lead_id": "A"}, key_headers(endpoint)
    )
    (lead,) = await env.leads(endpoint)
    await calling.stop_calling(endpoint, lead)
    # An older finished lead with the same number can't be called either.
    async with env.factory() as session:
        other = WebhookLeadModel(
            organization_id=endpoint.organization_id,
            endpoint_id=endpoint.id,
            phone="+919876543210",
            status="completed",
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
    with pytest.raises(calling.LeadActionError, match="opted out"):
        await calling.call_again(endpoint, other)


async def _connected_call(env, endpoint, gathered):
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    org = endpoint.organization_id
    run = await db_client.create_workflow_run(
        name="WR",
        workflow_id=endpoint.workflow_id,
        mode="twilio",
        user_id=endpoint.created_by,
        initial_context=queued_run.context_variables,
        gathered_context=gathered,
        campaign_id=endpoint.campaign_id,
        queued_run_id=queued_run.id,
        organization_id=org,
    )
    await calling.call_started(queued_run, run, org)
    await calling.call_finished(run.id)
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gathered",
    [
        {"mapped_call_disposition": "DO_NOT_CALL"},
        {"call_disposition": "Do not call"},
        {"call_tags": ["interested", "dnc"]},
        {"extracted_variables": {"do_not_call": "Yes"}},
        {"extracted_variables": {"opt_out": True}},
    ],
)
async def test_opting_out_during_the_call_blocks_the_number(env, no_kick, gathered):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    other = await _waiting_endpoint(env)
    await post(other, {"mobile": "9876543210"}, key_headers(other))

    await _connected_call(env, endpoint, gathered)

    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason, lead.last_call_status) == (
        "do_not_call",
        calling.OPTED_OUT_ON_CALL,
        "completed",
    )
    (waiting,) = await env.leads(other)
    assert waiting.status == "do_not_call"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gathered",
    [
        {"mapped_call_disposition": "Interested"},
        {"extracted_variables": {"do_not_call": "no"}},
        {"extracted_variables": {"notes": "said do not call before 5pm"}},
        None,
    ],
)
async def test_an_ordinary_call_is_not_an_opt_out(env, no_kick, gathered):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    await _connected_call(env, endpoint, gathered)
    (lead,) = await env.leads(endpoint)
    assert lead.status == "completed"


@pytest.mark.asyncio
async def test_an_opt_out_during_the_call_reaches_the_crm(env, no_kick):
    from api.services.webhook_sync import callback

    assert callback.is_final("do_not_call", 1, CallSettings())


# ---------------------------------------------------------------------------
# The endpoint's campaign is managed from the endpoint only
# ---------------------------------------------------------------------------


def test_campaigns_page_actions_are_refused_on_an_endpoint_campaign():
    continuous = SimpleNamespace(orchestrator_metadata={"continuous": True})
    normal = SimpleNamespace(orchestrator_metadata={})
    assert is_managed_elsewhere(continuous) and not is_managed_elsewhere(normal)
    with pytest.raises(HTTPException) as e:
        reject_if_managed_elsewhere(continuous)
    assert e.value.status_code == 400 and "Webhook Sync" in e.value.detail
    reject_if_managed_elsewhere(normal)  # no error


@pytest.mark.asyncio
async def test_the_campaigns_page_hides_and_protects_endpoint_campaigns(env, no_kick):
    from api.routes import campaign as campaign_routes

    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    user = env.orgs["a"].user

    listed = await campaign_routes.get_campaigns(user=user)
    assert endpoint.campaign_id not in [c.id for c in listed.campaigns]

    for action in (
        campaign_routes.stop_campaign,
        campaign_routes.pause_campaign,
        campaign_routes.resume_campaign,
    ):
        with pytest.raises(HTTPException) as e:
            await action(campaign_id=endpoint.campaign_id, user=user)
        assert e.value.status_code in (400, 401), action.__name__
    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    assert campaign.state == "running"


# ---------------------------------------------------------------------------
# Leads stuck in "calling"
# ---------------------------------------------------------------------------


async def _calling_lead(env, endpoint, *, run=None, minutes_ago=120):
    (lead,) = await env.leads(endpoint)
    async with env.factory() as session:
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.id == lead.id)
            .values(
                status="calling",
                call_attempts=1,
                last_workflow_run_id=run.id if run else None,
                updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
            )
        )
        await session.commit()
    return lead


@pytest.mark.asyncio
async def test_a_call_that_never_reported_back_is_settled_as_failed(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    await _calling_lead(env, endpoint)
    assert await calling.close_stuck_calls() >= 1
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.status_reason) == (
        "failed",
        "The call never reported how it ended",
    )


@pytest.mark.asyncio
async def test_a_stuck_lead_whose_call_did_finish_takes_its_result(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    run = await db_client.create_workflow_run(
        name="WR",
        workflow_id=endpoint.workflow_id,
        mode="twilio",
        user_id=endpoint.created_by,
        initial_context=queued_run.context_variables,
        gathered_context={"mapped_call_disposition": "Interested"},
        campaign_id=endpoint.campaign_id,
        queued_run_id=queued_run.id,
        organization_id=endpoint.organization_id,
    )
    await db_client.update_workflow_run(run_id=run.id, is_completed=True)
    await _calling_lead(env, endpoint, run=run)
    await calling.close_stuck_calls()
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.disposition) == ("completed", "Interested")


@pytest.mark.asyncio
async def test_a_call_in_progress_is_left_alone(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    await _calling_lead(env, endpoint, minutes_ago=10)
    await calling.close_stuck_calls()
    (lead,) = await env.leads(endpoint)
    assert lead.status == "calling"


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_failed_requests_raise_one_alert(env, no_alert_email):
    endpoint = await env.make_endpoint(
        call_settings={"alert_emails": ["ops@client.example"]}
    )
    await _clear_alert_state(endpoint)
    for _ in range(alerts.FAILED_REQUESTS_THRESHOLD + 3):
        await post(endpoint, {"mobile": "9876543210"}, {"X-API-Key": "wrong"})

    (alert,) = await _alerts(env, endpoint)  # once, not once per request
    assert alert.changes["kind"] == alerts.REQUESTS_FAILING
    assert "Wrong API key" in alert.changes["message"]
    assert alert.changes["emailed_to"] == ["ops@client.example"]
    ((to, subject, body),) = no_alert_email
    assert to == ["ops@client.example"] and "failing" in subject
    assert f"/webhook-sync/{endpoint.id}" in body


@pytest.mark.asyncio
async def test_a_success_resets_the_failed_request_count(env, no_alert_email):
    endpoint = await env.make_endpoint()
    await _clear_alert_state(endpoint)
    for _ in range(alerts.FAILED_REQUESTS_THRESHOLD - 1):
        await post(endpoint, {"mobile": "9876543210"}, {"X-API-Key": "wrong"})
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    for _ in range(alerts.FAILED_REQUESTS_THRESHOLD - 1):
        await post(endpoint, {"mobile": "9123456780"}, {"X-API-Key": "wrong"})
    assert await _alerts(env, endpoint) == []


@pytest.mark.asyncio
async def test_bad_payloads_count_as_failures_too(env, no_alert_email):
    endpoint = await env.make_endpoint()
    await _clear_alert_state(endpoint)
    for _ in range(alerts.FAILED_REQUESTS_THRESHOLD):
        await post(endpoint, {"name": "no phone here"}, key_headers(endpoint))
    (alert,) = await _alerts(env, endpoint)
    assert "phone is required" in alert.changes["message"]


@pytest.mark.asyncio
async def test_without_alert_emails_the_creator_is_emailed(env, no_alert_email):
    import uuid

    from api.db.models import UserModel

    owner = f"owner-{uuid.uuid4().hex[:8]}@botrix.test"
    endpoint = await env.make_endpoint()
    await _clear_alert_state(endpoint)
    async with env.factory() as session:
        await session.execute(
            update(UserModel)
            .where(UserModel.id == endpoint.created_by)
            .values(email=owner)
        )
        await session.commit()
    await alerts.calls_not_placed(endpoint, "Insufficient wallet balance")
    ((to, _, body),) = no_alert_email
    assert to == [owner] and "Insufficient wallet balance" in body


@pytest.mark.asyncio
async def test_without_smtp_the_alert_is_still_in_the_history(env, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    endpoint = await env.make_endpoint(call_settings={"alert_emails": ["a@b.co"]})
    await _clear_alert_state(endpoint)
    assert await alerts.calls_not_placed(endpoint, "No telephony configuration") is None
    (alert,) = await _alerts(env, endpoint)
    assert alert.changes["emailed_to"] == []


@pytest.mark.asyncio
async def test_a_lead_that_couldnt_be_called_raises_an_alert(
    env, no_kick, no_alert_email
):
    endpoint = await _open_hours_endpoint(env)
    await _clear_alert_state(endpoint)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    await calling.call_not_started(
        queued_run, endpoint.organization_id, "Insufficient wallet balance"
    )
    (alert,) = await _alerts(env, endpoint)
    assert alert.changes["kind"] == alerts.CALLS_NOT_PLACED


@pytest.mark.asyncio
async def test_calling_paused_by_the_circuit_breaker_raises_an_alert(
    env, no_kick, no_alert_email
):
    endpoint = await _open_hours_endpoint(env)
    await _clear_alert_state(endpoint)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    await db_client.update_campaign(campaign_id=endpoint.campaign_id, state="paused")
    await calling.alert_paused_calling()
    await calling.alert_paused_calling()  # the next minute: no second alert
    (alert,) = await _alerts(env, endpoint)
    assert alert.changes["kind"] == alerts.CALLING_PAUSED


@pytest.mark.asyncio
async def test_a_paused_endpoint_is_not_reported_as_paused_calling(
    env, no_kick, no_alert_email
):
    endpoint = await _open_hours_endpoint(env)
    await _clear_alert_state(endpoint)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, is_active=False
    )
    await calling.sync_campaign_settings(endpoint)  # pauses its campaign
    await calling.alert_paused_calling()
    assert await _alerts(env, endpoint) == []


@pytest.mark.asyncio
async def test_callbacks_that_gave_up_raise_an_alert(env, no_kick, no_alert_email):
    endpoint = await _open_hours_endpoint(env)
    await _clear_alert_state(endpoint)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await _reload(endpoint)
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    run = await db_client.create_workflow_run(
        name="WR",
        workflow_id=endpoint.workflow_id,
        mode="twilio",
        user_id=endpoint.created_by,
        initial_context=queued_run.context_variables,
        campaign_id=endpoint.campaign_id,
        queued_run_id=queued_run.id,
        organization_id=endpoint.organization_id,
    )
    (lead,) = await env.leads(endpoint)
    async with env.factory() as session:
        session.add(
            WebhookDeliveryModel(
                workflow_run_id=run.id,
                organization_id=endpoint.organization_id,
                endpoint_url="https://crm.example/hook",
                payload={"endpoint": {"id": endpoint.id}},
                webhook_node_id=f"webhook_sync_lead_{lead.id}",
                status="dead_letter",
            )
        )
        await session.commit()
    await calling.alert_failed_callbacks()
    (alert,) = await _alerts(env, endpoint)
    assert alert.changes["kind"] == alerts.CALLBACKS_FAILING
    assert "1 call result" in alert.changes["message"]


@pytest.mark.asyncio
async def test_the_minute_sweep_runs_every_step_even_if_one_fails(env):
    from api.tasks import webhook_sync_tasks

    steps = {
        "enqueue_stragglers": AsyncMock(side_effect=RuntimeError("boom")),
        "close_stuck_calls": AsyncMock(return_value=0),
        "alert_paused_calling": AsyncMock(side_effect=RuntimeError("boom")),
        "alert_failed_callbacks": AsyncMock(return_value=0),
    }
    with patch.multiple(calling, **steps):
        assert await webhook_sync_tasks.queue_waiting_webhook_leads({}) == 0
    for name, mock in steps.items():
        mock.assert_awaited_once(), name


# ---------------------------------------------------------------------------
# Settings and email
# ---------------------------------------------------------------------------


def test_alert_emails_are_validated_and_cleaned():
    s = CallSettings(alert_emails=[" Ops@Client.Example ", "ops@client.example", ""])
    assert s.alert_emails == ["ops@client.example"]
    with pytest.raises(Exception):
        CallSettings(alert_emails=["not-an-email"])
    with pytest.raises(Exception):
        CallSettings(alert_emails=[f"u{i}@x.co" for i in range(6)])
    with pytest.raises(Exception):
        CallSettings(reenquiry_days=0)
    assert CallSettings().reenquiry_days == 30


@pytest.mark.asyncio
async def test_email_is_sent_over_smtp_with_starttls(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "alerts@botrix.test")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_SECURITY", raising=False)
    smtp = MagicMock()
    with patch.object(email_module.smtplib, "SMTP", return_value=smtp) as factory:
        assert await email_module.send_email(["a@b.co", "a@b.co"], "Subj", "Body")
    factory.assert_called_once_with("smtp.example.com", 587, timeout=20)
    client = smtp  # used as `with client:`; SMTP.__enter__ returns itself
    client.starttls.assert_called_once()
    client.login.assert_called_once_with("alerts@botrix.test", "pw")
    message = client.send_message.call_args.args[0]
    assert (message["To"], message["From"], message["Subject"]) == (
        "a@b.co",
        "alerts@botrix.test",
        "Subj",
    )


@pytest.mark.asyncio
async def test_email_failure_or_no_config_never_raises(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert await email_module.send_email(["a@b.co"], "S", "B") is False
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "x@y.co")
    with patch.object(email_module.smtplib, "SMTP", side_effect=OSError("refused")):
        assert await email_module.send_email(["a@b.co"], "S", "B") is False
    assert await email_module.send_email([], "S", "B") is False
