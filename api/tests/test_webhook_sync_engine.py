# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.
# ruff: noqa: F811
"""Webhook Sync against the real campaign engine, wallet and delivery pipeline.

Only the outside world is faked (the telephony provider's REST call and the
CRM's server is a real local HTTP server); everything else is the production
code on the real test Postgres and Redis: the ARQ batch task with its slot,
rate-limit and caller-id pool in Redis, the orchestrator's retry handling and
schedule check, per-call wallet authorization and debit, the durable webhook
delivery task, and concurrent and bulk intake.
"""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from api.db import db_client
from api.db.models import (
    CampaignModel,
    OrganizationModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.schemas.webhook_sync import CallSettings
from api.services.campaign import campaign_event_publisher as publisher_module
from api.services.campaign.campaign_event_protocol import RetryNeededEvent
from api.services.campaign.campaign_orchestrator import CampaignOrchestrator
from api.services.campaign.circuit_breaker import circuit_breaker
from api.services.campaign.rate_limiter import rate_limiter
from api.services.quota_service import authorize_workflow_run_start
from api.services.telephony import status_processor
from api.services.webhook_sync import calling, callback
from api.services.workflow_run_billing import (
    report_completed_workflow_run_wallet_usage,
)
from api.tasks import campaign_tasks, webhook_delivery
from api.tasks.campaign_tasks import process_campaign_batch
from api.tasks.webhook_delivery import deliver_webhook
from api.tests.test_webhook_sync_calling import _open_hours_endpoint, _queued_runs
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401

# Everything is imported here, at collection time, not inside tests: a test
# module elsewhere wraps imports in patch.dict(sys.modules), which on exit
# drops modules first loaded inside it (numpy's C extensions among them), and
# re-importing those later in the run fails ("cannot load module more than
# once per process").

IST = "Asia/Kolkata"


@pytest.fixture(autouse=True)
def fresh_redis_clients():
    """The engine's singletons cache a Redis client created on whichever
    event loop first used it; a test on a later loop can't use it (the pool
    setup fails and dispatch waits out its 10-minute from-number timeout).
    Production runs one loop, so this is test isolation only."""

    def reset():
        rate_limiter.redis_client = None
        circuit_breaker.redis_client = None
        for name in ("_campaign_publisher", "_campaign_redis_client"):
            publisher_module.__dict__.pop(name, None)

    reset()
    yield
    reset()


@pytest.fixture
def no_kick():
    with patch.object(calling, "_kick", AsyncMock()):
        yield


async def _add_numbers(env, org_key, *numbers, channels=1):
    """A telephony config for the org with active caller-id numbers."""
    org = env.orgs[org_key].org
    config = await db_client.create_telephony_configuration(
        organization_id=org,
        name=f"tel-{uuid.uuid4().hex[:6]}",
        provider="twilio",
        credentials={"account_sid": f"AC{uuid.uuid4().hex}", "auth_token": "x"},
        is_default_outbound=True,
    )
    for number in numbers:
        row = await db_client.create_phone_number(
            organization_id=org,
            telephony_configuration_id=config.id,
            address=number,
        )
        if channels != 1:
            await db_client.update_phone_number(
                row.id, config.id, max_concurrent_calls=channels
            )
    return config


def _fake_provider(from_numbers=()):
    calls = []

    async def initiate_call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(call_id=f"CA{len(calls)}", provider_metadata={})

    return (
        SimpleNamespace(
            PROVIDER_NAME="twilio",
            WEBHOOK_ENDPOINT="twiml",
            initiate_call=initiate_call,
            # The dispatcher builds its caller-id pool from these, as the
            # factory attaches them from the config's active numbers.
            from_numbers=list(from_numbers),
        ),
        calls,
    )


# ---------------------------------------------------------------------------
# Intake under concurrency and load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simultaneous_first_leads_share_one_campaign(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    bodies = [{"mobile": f"98{i:08d}"} for i in range(12)]
    results = await asyncio.gather(
        *[post(endpoint, body, key_headers(endpoint)) for body in bodies]
    )
    assert all(r.status_code == 200 for r in results)

    async with env.factory() as session:
        campaigns = (
            (
                await session.execute(
                    select(CampaignModel).where(
                        CampaignModel.source_id == endpoint.endpoint_uuid,
                        CampaignModel.state != "cancelled",
                    )
                )
            )
            .scalars()
            .all()
        )
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert [c.id for c in campaigns] == [endpoint.campaign_id]
    runs = await _queued_runs(env, endpoint.campaign_id)
    assert len(runs) == 12
    assert all(lead.status == "queued" for lead in await env.leads(endpoint))


@pytest.mark.asyncio
async def test_a_500_lead_batch_is_stored_and_queued_quickly(env, no_kick):
    endpoint = await _open_hours_endpoint(env)
    body = [{"mobile": f"97{i:08d}", "name": f"Lead {i}"} for i in range(500)]
    started = time.perf_counter()
    result = await post(endpoint, body, key_headers(endpoint))
    elapsed = time.perf_counter() - started
    assert result.status_code == 200 and result.body["created"] == 500
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    assert len(await _queued_runs(env, endpoint.campaign_id)) == 500
    # Generous bound for a laptop and a shared test DB; the point is that a
    # bulk request doesn't approach a CRM's typical 30s webhook timeout.
    assert elapsed < 30, f"500 leads took {elapsed:.1f}s"
    print(f"\n500-lead request: {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# The real ARQ batch task dispatches the calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_batch_task_calls_each_queued_lead_from_the_pool(env, no_kick):

    await _add_numbers(env, "a", "+918065607348", "+917921731184")
    endpoint = await _open_hours_endpoint(env)
    await post(
        endpoint,
        {"name": "Asha", "mobile": "9876543210", "city": "Patna"},
        key_headers(endpoint),
    )
    await post(
        endpoint, {"name": "Ravi", "mobile": "9123456780"}, key_headers(endpoint)
    )
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )

    provider, calls = _fake_provider(["+918065607348", "+917921731184"])
    quota = SimpleNamespace(has_quota=True, error_message=None)
    # Patch the dispatcher the batch task actually holds (and its module),
    # not whatever `campaign_call_dispatcher` resolves to now: another test
    # module can re-import modules, leaving two copies.

    dispatcher = campaign_tasks.campaign_call_dispatcher
    # The globals dispatch_call really reads (a re-imported module's copy of
    # the name would be a different dict).
    dispatch_globals = type(dispatcher).dispatch_call.__globals__
    with (
        patch.object(
            dispatcher, "get_provider_for_campaign", AsyncMock(return_value=provider)
        ),
        patch.dict(
            dispatch_globals,
            {
                "authorize_workflow_run_start": AsyncMock(return_value=quota),
                # test_campaign_call_dispatcher leaves a MagicMock rate
                # limiter in this module's globals; use the real one.
                "rate_limiter": rate_limiter,
            },
        ),
    ):
        from loguru import logger as _logger

        lines: list[str] = []
        sink = _logger.add(lines.append, format="{level} {name}:{line} {message}")
        try:
            await process_campaign_batch({}, endpoint.campaign_id, 10)
        finally:
            _logger.remove(sink)

    assert sorted(c["to_number"] for c in calls) == [
        "+919123456780",
        "+919876543210",
    ], "\n".join(lines)
    # Two leads, two numbers of one channel each: each call from its own number.
    assert sorted(c["from_number"] for c in calls) == ["+917921731184", "+918065607348"]

    runs = await _queued_runs(env, endpoint.campaign_id)
    assert {r.state for r in runs} == {"processed"}
    leads = {lead.phone: lead for lead in await env.leads(endpoint)}
    asha = leads["+919876543210"]
    assert (asha.status, asha.call_attempts) == ("calling", 1)

    run = await db_client.get_workflow_run_by_id(asha.last_workflow_run_id)
    assert run.campaign_id == endpoint.campaign_id
    assert run.initial_context["webhook_lead_id"] == asha.id
    assert run.initial_context["city"] == "Patna"
    assert run.initial_context["called_number"] == "+919876543210"

    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    assert campaign.state == "running" and campaign.processed_rows == 2

    # Hang both calls up (no answer) so their slots and numbers are released.

    for lead in leads.values():
        with patch.object(
            status_processor,
            "get_campaign_event_publisher",
            AsyncMock(return_value=AsyncMock()),
        ):
            await status_processor._process_status_update(
                lead.last_workflow_run_id,
                status_processor.StatusCallbackRequest(call_id="x", status="no-answer"),
            )
    assert {lead.status for lead in await env.leads(endpoint)} == {"no_answer"}


@pytest.mark.asyncio
async def test_outside_calling_hours_the_orchestrator_does_not_dispatch(env, no_kick):

    now_ist = datetime.now(UTC).astimezone(__import__("zoneinfo").ZoneInfo(IST))
    closed_day = (now_ist.weekday() + 1) % 7  # only tomorrow is a calling day
    endpoint = await env.make_endpoint(
        auto_call=True,
        call_settings={
            "calling_hours": {
                "start": "00:00",
                "end": "23:59",
                "timezone": IST,
                "days": [closed_day],
            }
        },
    )
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert (
        lead.status == "scheduled" and lead.status_reason == "Waiting for calling hours"
    )

    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    campaign = await db_client.get_campaign_by_id(endpoint.campaign_id)
    orchestrator = CampaignOrchestrator.__new__(CampaignOrchestrator)
    assert orchestrator._is_within_schedule(campaign) is False

    open_now = await _open_hours_endpoint(env)
    open_campaign = await calling.ensure_endpoint_campaign(open_now)
    assert orchestrator._is_within_schedule(open_campaign) is True


# ---------------------------------------------------------------------------
# The real orchestrator's retry handling, through to the CRM callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_answer_is_retried_then_the_final_result_is_sent(env, no_kick):

    endpoint = await _open_hours_endpoint(env)
    settings = {
        **endpoint.call_settings,
        "callback_url": "https://crm.example.com/result",
    }
    endpoint = await db_client.update_webhook_endpoint(
        endpoint.id, endpoint.organization_id, call_settings=settings
    )
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    org = endpoint.organization_id
    orchestrator = CampaignOrchestrator.__new__(CampaignOrchestrator)
    orchestrator._last_activity = {}

    async def attempt(queued_run):
        run = await db_client.create_workflow_run(
            name="WR",
            workflow_id=endpoint.workflow_id,
            mode="twilio",
            user_id=endpoint.created_by,
            initial_context=queued_run.context_variables,
            campaign_id=endpoint.campaign_id,
            queued_run_id=queued_run.id,
            organization_id=org,
        )
        await calling.call_started(queued_run, run, org)
        await calling.call_not_connected(run, "no-answer")
        await orchestrator._handle_retry_event(
            RetryNeededEvent(
                workflow_run_id=run.id,
                reason="no_answer",
                campaign_id=endpoint.campaign_id,
                queued_run_id=queued_run.id,
            )
        )
        return run

    async def resolve(host, port, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    public = patch.object(
        callback.asyncio,
        "get_running_loop",
        return_value=SimpleNamespace(getaddrinfo=resolve),
    )
    enqueue = AsyncMock()
    with public, patch("api.tasks.arq.enqueue_job", enqueue):
        (first,) = await _queued_runs(env, endpoint.campaign_id)
        await attempt(first)

        # The real orchestrator booked retry 1, 10 minutes out.
        runs = await _queued_runs(env, endpoint.campaign_id)
        assert len(runs) == 2
        retry = runs[1]
        assert (retry.retry_count, retry.parent_queued_run_id, retry.retry_reason) == (
            1,
            first.id,
            "no_answer",
        )
        assert (
            timedelta(minutes=9)
            < retry.scheduled_for - datetime.now(UTC)
            <= timedelta(minutes=10)
        )
        (lead,) = await env.leads(endpoint)
        assert (lead.status, lead.queued_run_id) == ("scheduled", retry.id)
        assert lead.status_reason == "Retry 1 after no answer"
        assert await db_client.get_webhook_lead_callback(lead.id, org) is None

        # The retry isn't answered either: attempts are used up.
        last_run = await attempt(retry)
    assert len(await _queued_runs(env, endpoint.campaign_id)) == 2  # no third call
    (lead,) = await env.leads(endpoint)
    assert (lead.status, lead.call_attempts) == ("no_answer", 2)
    delivery = await db_client.get_webhook_lead_callback(lead.id, org)
    assert delivery.workflow_run_id == last_run.id
    assert delivery.payload["result"] == {
        "status": "no_answer",
        "reason": None,
        "call_attempts": 2,
        "disposition": "no-answer",
        "duration_seconds": None,
        "extracted": {},
    }
    enqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# Wallet: checked and debited per call
# ---------------------------------------------------------------------------


async def _wallet(env, balance, price="2.0000"):
    org = env.orgs["a"].org
    async with env.factory() as session:
        await session.execute(
            update(OrganizationModel)
            .where(OrganizationModel.id == org)
            .values(
                wallet_enabled=True,
                wallet_balance=Decimal(balance),
                credit_limit=Decimal("0"),
            )
        )
        await session.execute(
            update(WorkflowModel)
            .where(WorkflowModel.id == env.orgs["a"].workflow)
            .values(price_per_minute=Decimal(price))
        )
        await session.commit()


async def _balance(env) -> Decimal:
    async with env.factory() as session:
        return (
            await session.execute(
                select(OrganizationModel.wallet_balance).where(
                    OrganizationModel.id == env.orgs["a"].org
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_an_empty_wallet_blocks_a_webhook_call_but_not_a_normal_campaign(
    env, no_kick
):

    await _wallet(env, "0")
    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    ordinary = await db_client.create_campaign(
        name="csv",
        workflow_id=endpoint.workflow_id,
        source_type="csv",
        source_id="x",
        user_id=endpoint.created_by,
        organization_id=endpoint.organization_id,
    )
    org, workflow = endpoint.organization_id, endpoint.workflow_id

    blocked = await authorize_workflow_run_start(
        workflow_id=workflow, organization_id=org, campaign_id=campaign.id
    )
    assert (
        blocked.has_quota is False
        and blocked.error_code == "insufficient_wallet_balance"
    )
    # A normal campaign reserved its money up front, so its calls aren't gated here.
    reserved = await authorize_workflow_run_start(
        workflow_id=workflow, organization_id=org, campaign_id=ordinary.id
    )
    assert reserved.error_code != "insufficient_wallet_balance"

    await _wallet(env, "100")
    allowed = await authorize_workflow_run_start(
        workflow_id=workflow, organization_id=org, campaign_id=campaign.id
    )
    assert allowed.error_code != "insufficient_wallet_balance"


@pytest.mark.asyncio
async def test_a_finished_webhook_call_is_debited_from_the_wallet(env, no_kick):

    await _wallet(env, "100", price="2.0000")
    endpoint = await _open_hours_endpoint(env)
    campaign = await calling.ensure_endpoint_campaign(endpoint)
    ordinary = await db_client.create_campaign(
        name="csv",
        workflow_id=endpoint.workflow_id,
        source_type="csv",
        source_id="x",
        user_id=endpoint.created_by,
        organization_id=endpoint.organization_id,
    )

    async def finished_run(campaign_id):
        run = await db_client.create_workflow_run(
            name="WR",
            workflow_id=endpoint.workflow_id,
            mode="twilio",
            user_id=endpoint.created_by,
            campaign_id=campaign_id,
            organization_id=endpoint.organization_id,
        )
        async with env.factory() as session:
            await session.execute(
                update(WorkflowRunModel)
                .where(WorkflowRunModel.id == run.id)
                .values(is_completed=True, usage_info={"call_duration_seconds": 120})
            )
            await session.commit()
        return run

    before = await _balance(env)
    await report_completed_workflow_run_wallet_usage(
        (await finished_run(campaign.id)).id
    )
    after_webhook = await _balance(env)
    # Two minutes at 2.00/min.
    assert before - after_webhook == Decimal("4.0000")

    # An ordinary campaign's call is ledger-only (its cost was reserved).
    await report_completed_workflow_run_wallet_usage(
        (await finished_run(ordinary.id)).id
    )
    assert await _balance(env) == after_webhook

    # A repeated completion hook doesn't charge twice.
    run = await finished_run(campaign.id)
    await report_completed_workflow_run_wallet_usage(run.id)
    once = await _balance(env)
    await report_completed_workflow_run_wallet_usage(run.id)
    assert await _balance(env) == once


# ---------------------------------------------------------------------------
# The real delivery task against a real local HTTP server
# ---------------------------------------------------------------------------


class _CRM:
    """A tiny local HTTP server playing the CRM: records requests and
    answers with the next queued status code."""

    def __init__(self):
        self.requests = []
        self.codes = []

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {
            k.lower(): v
            for k, v in (line.split(": ", 1) for line in lines[1:] if ": " in line)
        }
        body = await reader.readexactly(int(headers.get("content-length", 0)))
        self.requests.append(
            {"line": lines[0], "headers": headers, "body": json.loads(body or b"{}")}
        )
        code = self.codes.pop(0) if self.codes else 200
        writer.write(
            f"HTTP/1.1 {code} X\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".encode()
        )
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = (
            f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/botrix/result"
        )
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def _pending_callback(env, crm_url):
    """A completed lead whose callback delivery is queued (not yet sent)."""
    endpoint = await _open_hours_endpoint(env)
    await post(
        endpoint, {"name": "Asha", "mobile": "9876543210"}, key_headers(endpoint)
    )
    endpoint = await db_client.get_webhook_endpoint(
        endpoint.id, endpoint.organization_id
    )
    (queued_run,) = await _queued_runs(env, endpoint.campaign_id)
    org = endpoint.organization_id
    run = await db_client.create_workflow_run(
        name="WR",
        workflow_id=endpoint.workflow_id,
        mode="twilio",
        user_id=endpoint.created_by,
        initial_context=queued_run.context_variables,
        gathered_context={"mapped_call_disposition": "Interested"},
        campaign_id=endpoint.campaign_id,
        queued_run_id=queued_run.id,
        organization_id=org,
    )
    await calling.call_started(queued_run, run, org)
    # A local CRM is only reachable over http on 127.0.0.1, which both the
    # https-only settings and the public-URL check refuse by design (each has
    # its own tests); here the settings are built unvalidated.

    local = CallSettings.model_validate(endpoint.call_settings).model_copy(
        update={"callback_url": crm_url}
    )
    with (
        patch.object(callback, "_settings", lambda _endpoint: local),
        patch.object(callback, "ensure_public_callback_url", AsyncMock()),
        patch("api.tasks.arq.enqueue_job", AsyncMock()),
    ):
        await calling.call_finished(run.id)
    (lead,) = await env.leads(endpoint)
    delivery = await db_client.get_webhook_lead_callback(lead.id, org)
    return endpoint, lead, delivery


@pytest.mark.asyncio
async def test_the_crm_receives_the_result_with_the_secret(env, no_kick):

    async with _CRM() as crm:
        endpoint, lead, delivery = await _pending_callback(env, crm.url)
        await deliver_webhook({}, delivery.id)

    (request,) = crm.requests
    assert request["line"] == "POST /botrix/result HTTP/1.1"
    assert request["headers"]["x-botrix-secret"] == endpoint.secret
    assert request["headers"]["x-dograh-delivery-id"] == delivery.delivery_uuid
    assert request["headers"]["content-type"] == "application/json"
    assert request["body"]["lead"]["id"] == lead.id
    assert request["body"]["result"]["status"] == "completed"
    assert request["body"]["result"]["disposition"] == "Interested"
    done = await db_client.get_webhook_lead_callback(lead.id, endpoint.organization_id)
    assert (done.status, done.attempt_count, done.last_status_code) == (
        "succeeded",
        1,
        200,
    )


@pytest.mark.asyncio
async def test_a_crm_server_error_is_retried_and_a_rejection_is_not(env, no_kick):

    async with _CRM() as crm:
        crm.codes = [503]
        endpoint, lead, delivery = await _pending_callback(env, crm.url)
        with patch.object(
            webhook_delivery, "_enqueue_delivery", AsyncMock()
        ) as requeue:
            await webhook_delivery.deliver_webhook({}, delivery.id)
        retrying = await db_client.get_webhook_lead_callback(
            lead.id, endpoint.organization_id
        )
        assert (retrying.status, retrying.attempt_count, retrying.last_status_code) == (
            "pending",
            1,
            503,
        )
        assert retrying.scheduled_for > datetime.now(UTC)
        requeue.assert_awaited_once()

        crm.codes = [400]
        endpoint2, lead2, delivery2 = await _pending_callback(env, crm.url)
        await webhook_delivery.deliver_webhook({}, delivery2.id)
    rejected = await db_client.get_webhook_lead_callback(
        lead2.id, endpoint2.organization_id
    )
    assert (rejected.status, rejected.last_status_code) == ("dead_letter", 400)
