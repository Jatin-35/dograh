# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.
# ruff: noqa: F811
"""Webhook Sync dashboard actions: send a test lead, call a lead again, stop
calling a lead, export leads as CSV, and resend a result to the CRM."""

import csv
import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from api.db import db_client
from api.db.models import QueuedRunModel, WebhookDeliveryModel
from api.routes import webhook_sync as routes
from api.schemas.webhook_sync import TestLeadRequest
from api.services.webhook_sync import calling
from api.tests.test_webhook_sync_calling import _open_hours_endpoint, _queued_runs
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401


@pytest.fixture
def no_kick():
    with patch.object(calling, "_kick", AsyncMock()):
        yield


@pytest.fixture
def unmasked():
    with patch(
        "api.routes.webhook_sync.should_mask_phone_numbers",
        AsyncMock(return_value=False),
    ):
        yield


async def _state(env, queued_run_id):
    async with env.factory() as session:
        return (
            await session.execute(
                select(QueuedRunModel.state).where(QueuedRunModel.id == queued_run_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# Send a test lead
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_type", ["api_key", "hmac", "url_token"])
async def test_a_test_lead_goes_through_the_real_receiver_in_every_auth_mode(
    env, no_kick, auth_type
):
    endpoint = await _open_hours_endpoint(env, auth_type=auth_type)
    user = env.orgs["a"].user
    result = await routes.send_test(
        endpoint.id, TestLeadRequest(phone="9876543210", name="Me"), user=user
    )
    assert result.status_code == 200 and result.body["created"] == 1
    (lead,) = await env.leads(endpoint)
    assert result.lead_id == lead.id
    assert (lead.name, lead.source, lead.phone) == (
        "Me",
        "Dashboard test",
        "+919876543210",
    )
    assert lead.status == "queued"  # auto-call on: it will really be called
    (log,) = await env.logs(endpoint)
    assert log.response_code == 200


@pytest.mark.asyncio
async def test_a_test_lead_reports_what_a_crm_would_see(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    user = env.orgs["a"].user
    bad = await routes.send_test(endpoint.id, TestLeadRequest(phone="12345"), user=user)
    assert bad.status_code == 200 and bad.body["invalid"] == 1
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, is_active=False
    )
    paused = await routes.send_test(
        endpoint.id, TestLeadRequest(phone="9876543210"), user=user
    )
    # Paused: the lead is stored on hold, as a CRM's would be.
    assert paused.status_code == 200 and paused.body["paused"] is True
    assert paused.lead_id is not None


@pytest.mark.asyncio
async def test_another_org_cannot_send_test_leads_to_my_endpoint(env):
    endpoint = await _open_hours_endpoint(env)
    with pytest.raises(HTTPException) as exc:
        await routes.send_test(
            endpoint.id, TestLeadRequest(phone="9876543210"), user=env.orgs["b"].user
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Call again / stop calling
# ---------------------------------------------------------------------------


async def _lead_in(env, status, **overrides):
    endpoint = await _open_hours_endpoint(env, **overrides)
    await post(
        endpoint, {"name": "Asha", "mobile": "9876543210"}, key_headers(endpoint)
    )
    (lead,) = await env.leads(endpoint)
    if status != lead.status:
        await db_client.update_webhook_lead(
            lead.id, lead.organization_id, status=status
        )
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    return endpoint, (await env.leads(endpoint))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "no_answer", "busy", "completed"])
async def test_a_finished_lead_can_be_called_again(env, no_kick, unmasked, status):
    endpoint, lead = await _lead_in(env, status)
    result = await routes.call_lead_again(lead.id, user=env.orgs["a"].user)
    assert (result.status, result.status_reason) == (
        "queued",
        "Called again from the dashboard",
    )
    runs = await _queued_runs(env, endpoint.campaign_id)
    assert len(runs) == 2
    assert runs[-1].context_variables["webhook_lead_id"] == lead.id
    assert runs[-1].state == "queued"


@pytest.mark.asyncio
async def test_a_lead_never_called_because_auto_call_was_off_can_be_called(
    env, no_kick, unmasked
):
    endpoint, lead = await _lead_in(env, "received", auto_call=False)
    result = await routes.call_lead_again(lead.id, user=env.orgs["a"].user)
    assert result.status == "queued"
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert len(await _queued_runs(env, endpoint.campaign_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["queued", "scheduled", "calling", "duplicate", "invalid_number", "do_not_call"],
)
async def test_call_again_is_refused_while_busy_or_for_leads_that_must_not_be_called(
    env, no_kick, unmasked, status
):
    _, lead = await _lead_in(env, status)
    with pytest.raises(HTTPException) as exc:
        await routes.call_lead_again(lead.id, user=env.orgs["a"].user)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_call_again_on_a_paused_endpoint_asks_to_resume_first(
    env, no_kick, unmasked
):
    endpoint, lead = await _lead_in(env, "failed")
    await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, is_active=False
    )
    with pytest.raises(HTTPException) as exc:
        await routes.call_lead_again(lead.id, user=env.orgs["a"].user)
    assert exc.value.status_code == 409 and "Resume" in exc.value.detail


@pytest.mark.asyncio
async def test_stop_calling_cancels_the_queued_call(env, no_kick, unmasked):
    endpoint, lead = await _lead_in(env, "queued")
    result = await routes.stop_calling_lead(lead.id, user=env.orgs["a"].user)
    assert (result.status, result.status_reason) == (
        "do_not_call",
        "Stopped from the dashboard",
    )
    assert await _state(env, lead.queued_run_id) == "failed"
    # Idempotent.
    again = await routes.stop_calling_lead(lead.id, user=env.orgs["a"].user)
    assert again.status == "do_not_call"


@pytest.mark.asyncio
async def test_a_call_already_under_way_cannot_undo_stop_calling(
    env, no_kick, unmasked
):
    endpoint, lead = await _lead_in(env, "queued")
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
    await calling.call_started(queued_run, run, endpoint.organization_id)
    await routes.stop_calling_lead(lead.id, user=env.orgs["a"].user)

    # The call ends unanswered and the engine books a retry anyway.
    await calling.call_not_connected(run, "no-answer")
    retry = await db_client.create_queued_run(
        campaign_id=endpoint.campaign_id,
        source_uuid=f"{queued_run.source_uuid}_retry_1",
        context_variables=queued_run.context_variables,
        retry_count=1,
        parent_queued_run_id=queued_run.id,
        retry_reason="no_answer",
    )
    await calling.retry_scheduled(retry, endpoint.organization_id)

    (after,) = await env.leads(endpoint)
    assert after.status == "do_not_call"
    assert await _state(env, retry.id) == "failed"  # the retry never dials


@pytest.mark.asyncio
async def test_stopping_an_ordinary_campaigns_retry_is_never_touched(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    retry = await db_client.create_queued_run(
        campaign_id=campaign.id,
        source_uuid="csv-row-1_retry_1",
        context_variables={"phone_number": "+919876543210"},  # no webhook lead
        retry_count=1,
    )
    await calling.retry_scheduled(retry, endpoint.organization_id)
    assert await _state(env, retry.id) == "queued"


@pytest.mark.asyncio
async def test_lead_actions_are_organization_scoped(env, no_kick):
    _, lead = await _lead_in(env, "failed")
    other = env.orgs["b"].user
    for action in (
        routes.call_lead_again,
        routes.stop_calling_lead,
        routes.resend_result,
    ):
        with pytest.raises(HTTPException) as exc:
            await action(lead.id, user=other)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _rows(response):
    text = response.body.decode("utf-8")
    assert text.startswith("﻿")  # Excel-friendly
    return list(csv.DictReader(io.StringIO(text[1:])))


@pytest.mark.asyncio
async def test_export_is_a_csv_of_the_filtered_leads(env, no_kick, unmasked):
    endpoint = await _open_hours_endpoint(env)
    await post(
        endpoint,
        {"name": "राहुल", "mobile": "9876543210", "city": "Patna"},
        key_headers(endpoint),
    )
    await post(endpoint, {"name": "Dup", "mobile": "9876543210"}, key_headers(endpoint))
    await post(endpoint, {"name": "Bad", "mobile": "123"}, key_headers(endpoint))
    user = env.orgs["a"].user
    kwargs = dict(
        endpoint_id=endpoint.id,
        search=None,
        received_from=None,
        received_to=None,
        user=user,
    )

    everything = await routes.export_leads(status=None, **kwargs)
    assert everything.media_type.startswith("text/csv")
    assert "attachment" in everything.headers["content-disposition"]
    rows = _rows(everything)
    assert [r["name"] for r in rows] == ["Bad", "Dup", "राहुल"]
    first = rows[-1]
    assert (first["phone"], first["city"], first["status"], first["endpoint"]) == (
        "+919876543210",
        "Patna",
        "queued",
        "CRM",
    )

    dups = _rows(await routes.export_leads(status=["duplicate"], **kwargs))
    assert [r["name"] for r in dups] == ["Dup"]


@pytest.mark.asyncio
async def test_export_masks_numbers_when_the_org_masks_them(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    with patch(
        "api.routes.webhook_sync.should_mask_phone_numbers",
        AsyncMock(return_value=True),
    ):
        rows = _rows(
            await routes.export_leads(
                endpoint_id=endpoint.id,
                status=None,
                search=None,
                received_from=None,
                received_to=None,
                user=env.orgs["a"].user,
            )
        )
    assert "9876543210" not in rows[0]["phone"]


@pytest.mark.asyncio
async def test_export_of_another_orgs_endpoint_is_refused(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    with pytest.raises(HTTPException) as exc:
        await routes.export_leads(
            endpoint_id=endpoint.id,
            status=None,
            search=None,
            received_from=None,
            received_to=None,
            user=env.orgs["b"].user,
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Resend a result to the CRM
# ---------------------------------------------------------------------------


async def _lead_with_callback(env, status="dead_letter"):
    endpoint = await _open_hours_endpoint(env)
    await post(
        endpoint, {"name": "Asha", "mobile": "9876543210"}, key_headers(endpoint)
    )
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
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
    await calling.call_started(queued_run, run, endpoint.organization_id)
    (lead,) = await env.leads(endpoint)
    delivery, _ = await db_client.create_webhook_delivery(
        workflow_run_id=run.id,
        organization_id=endpoint.organization_id,
        endpoint_url="https://crm.example.com/r",
        payload={"old": True},
        max_attempts=5,
        webhook_node_id=f"webhook_sync_lead_{lead.id}",
    )
    async with env.factory() as session:
        await session.execute(
            update(WebhookDeliveryModel)
            .where(WebhookDeliveryModel.id == delivery.id)
            .values(status=status, attempt_count=5, last_error="HTTP 500: boom")
        )
        await session.commit()
    return endpoint, lead, delivery


@pytest.mark.asyncio
async def test_a_failed_result_is_resent_with_fresh_data(env, no_kick):
    endpoint, lead, delivery = await _lead_with_callback(env)
    enqueue = AsyncMock()
    with patch("api.tasks.arq.enqueue_job", enqueue):
        result = await routes.resend_result(lead.id, user=env.orgs["a"].user)
    assert result == {"success": True}
    enqueue.assert_awaited_once()
    fresh = await db_client.get_webhook_lead_callback(lead.id, endpoint.organization_id)
    assert (fresh.status, fresh.attempt_count, fresh.last_error) == ("pending", 0, None)
    assert fresh.payload["lead"]["id"] == lead.id and "old" not in fresh.payload


@pytest.mark.asyncio
async def test_resend_is_refused_while_sending_or_when_there_is_nothing(env, no_kick):
    _, lead, _ = await _lead_with_callback(env, status="pending")
    with pytest.raises(HTTPException) as exc:
        await routes.resend_result(lead.id, user=env.orgs["a"].user)
    assert exc.value.status_code == 409

    endpoint = await _open_hours_endpoint(env)
    await post(endpoint, {"mobile": "9123456780"}, key_headers(endpoint))
    (plain,) = await env.leads(endpoint)
    with pytest.raises(HTTPException) as exc:
        await routes.resend_result(plain.id, user=env.orgs["a"].user)
    assert exc.value.status_code == 409
