"""BotrixAI campaign dispatch behaviour on top of upstream's parallel dispatcher.

Covers the fork's additions (see ``CampaignCallDispatcher``'s docstring):
pause/stop taking effect between dials, the per-channel caller-ID pool,
provider account rejections pausing the campaign while only telephony outages
count toward the failure rate, and Webhook Sync lead updates.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db import db_client
from api.db.models import (
    CampaignModel,
    OrganizationModel,
    QueuedRunModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.campaign import campaign_call_dispatcher as dispatcher_module
from api.services.campaign.campaign_call_dispatcher import CampaignCallDispatcher
from api.services.campaign.errors import PhoneNumberPoolExhaustedError

# ---------------------------------------------------------------------------
# Batch path, against the test database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def sessions(setup_test_database):
    engine = create_async_engine(setup_test_database)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    old_engine, old_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, factory
    yield factory
    db_client.engine, db_client.async_session = old_engine, old_session
    await engine.dispose()


@pytest.fixture
async def campaign_data(sessions):
    async with sessions() as session:
        org = OrganizationModel(provider_id=f"botrix-dispatch-{uuid.uuid4().hex}")
        session.add(org)
        await session.flush()
        user = UserModel(
            provider_id=f"botrix-dispatch-{uuid.uuid4().hex}",
            selected_organization_id=org.id,
        )
        session.add(user)
        await session.flush()
        workflow = WorkflowModel(
            name="botrix-dispatch", user_id=user.id, organization_id=org.id
        )
        session.add(workflow)
        await session.flush()
        campaign = CampaignModel(
            name="botrix-dispatch",
            organization_id=org.id,
            workflow_id=workflow.id,
            created_by=user.id,
            source_type="csv",
            source_id="test",
            state="running",
            rate_limit_per_second=4,
        )
        session.add(campaign)
        await session.flush()
        rows = [
            QueuedRunModel(
                campaign_id=campaign.id,
                source_uuid=f"contact-{i}",
                context_variables={"phone_number": f"+15551000{i:03}"},
                state="queued",
            )
            for i in range(5)
        ]
        session.add_all(rows)
        await session.commit()
    yield SimpleNamespace(campaign=campaign, rows=rows, org=org)
    async with sessions() as session:
        await session.execute(
            delete(WorkflowRunModel).where(WorkflowRunModel.campaign_id == campaign.id)
        )
        await session.execute(
            delete(QueuedRunModel).where(QueuedRunModel.campaign_id == campaign.id)
        )
        await session.execute(
            delete(CampaignModel).where(CampaignModel.id == campaign.id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == workflow.id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == user.id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == org.id)
        )
        await session.commit()


async def _row_states(sessions, campaign_id):
    async with sessions() as session:
        rows = (
            await session.execute(
                select(QueuedRunModel).where(QueuedRunModel.campaign_id == campaign_id)
            )
        ).scalars()
        return sorted(row.state for row in rows)


def _create_run(sessions):
    async def create_run(row, campaign, slot):
        async with sessions() as session:
            run = WorkflowRunModel(
                name="botrix-dispatch",
                workflow_id=campaign.workflow_id,
                mode="twilio",
                campaign_id=campaign.id,
                queued_run_id=row.id,
            )
            session.add(run)
            await session.commit()
            return run

    return create_run


@pytest.fixture
def batch(sessions, campaign_data):
    """A dispatcher whose provider, slots and dialing are stubbed out."""
    instance = CampaignCallDispatcher()
    instance.SETUP_CONCURRENCY = 1  # one contact at a time, so order is known
    provider = SimpleNamespace(from_numbers=[])
    with (
        patch.object(
            instance, "get_provider_for_campaign", AsyncMock(return_value=provider)
        ),
        patch.object(
            instance, "acquire_concurrent_slot", AsyncMock(return_value=object())
        ),
        patch.object(
            instance, "dispatch_call", AsyncMock(side_effect=_create_run(sessions))
        ),
        patch.object(
            dispatcher_module.webhook_sync_calling, "call_not_started", AsyncMock()
        ) as not_started,
        patch.object(
            dispatcher_module.call_concurrency, "release_slot", AsyncMock()
        ) as release_slot,
    ):
        yield SimpleNamespace(
            dispatcher=instance, not_started=not_started, release_slot=release_slot
        )


@pytest.mark.asyncio
async def test_pause_mid_batch_keeps_remaining_contacts_queued(
    batch, campaign_data, sessions
):
    campaign_id = campaign_data.campaign.id
    dial = batch.dispatcher.dispatch_call.side_effect

    async def dial_then_pause(row, campaign, slot):
        run = await dial(row, campaign, slot)
        await db_client.update_campaign(campaign_id, state="paused")
        return run

    batch.dispatcher.dispatch_call.side_effect = dial_then_pause

    assert await batch.dispatcher.process_batch(campaign_id) == 1
    assert batch.dispatcher.dispatch_call.await_count == 1
    assert await _row_states(sessions, campaign_id) == ["processed"] + ["queued"] * 4


@pytest.mark.asyncio
async def test_pause_while_waiting_for_a_slot_releases_it(
    batch, campaign_data, sessions
):
    campaign_id = campaign_data.campaign.id
    slot = object()

    async def pause_while_waiting(*args, **kwargs):
        await db_client.update_campaign(campaign_id, state="paused")
        return slot

    batch.dispatcher.acquire_concurrent_slot.side_effect = pause_while_waiting

    assert await batch.dispatcher.process_batch(campaign_id) == 0
    batch.dispatcher.dispatch_call.assert_not_awaited()
    batch.release_slot.assert_awaited_once_with(slot)
    assert await _row_states(sessions, campaign_id) == ["queued"] * 5


@pytest.mark.asyncio
async def test_exhausted_caller_id_pool_returns_contacts_unfailed(
    batch, campaign_data, sessions
):
    campaign_id = campaign_data.campaign.id
    batch.dispatcher.dispatch_call.side_effect = PhoneNumberPoolExhaustedError(
        organization_id=campaign_data.org.id
    )

    assert await batch.dispatcher.process_batch(campaign_id) == 0
    batch.not_started.assert_not_awaited()
    assert await _row_states(sessions, campaign_id) == ["queued"] * 5


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_failed_contact_updates_webhook_sync_lead_unless_dial_uncertain(
    uncertain, batch, campaign_data, sessions
):
    campaign_id = campaign_data.campaign.id
    error = ValueError("Quota exceeded")
    if uncertain:
        error.dispatch_uncertain = True
    batch.dispatcher.dispatch_call.side_effect = error

    assert await batch.dispatcher.process_batch(campaign_id, batch_size=1) == 0
    if uncertain:
        batch.not_started.assert_not_awaited()
    else:
        batch.not_started.assert_awaited_once()
        assert batch.not_started.await_args.args[1] == campaign_data.org.id
        assert batch.not_started.await_args.args[2] == "Quota exceeded"
    assert await _row_states(sessions, campaign_id) == ["failed"] + ["queued"] * 4


@pytest.mark.asyncio
async def test_caller_id_pool_is_sized_by_channels(batch, campaign_data):
    batch.dispatcher.get_provider_for_campaign.return_value = SimpleNamespace(
        from_numbers=["+15550001", "+15550002"]
    )
    with (
        patch.object(
            batch.dispatcher,
            "get_channel_capacity_for_campaign",
            AsyncMock(return_value={"+15550001": 3}),
        ),
        patch.object(
            dispatcher_module.rate_limiter, "initialize_from_number_pool", AsyncMock()
        ) as init_pool,
    ):
        await batch.dispatcher.process_batch(campaign_data.campaign.id, batch_size=1)
    init_pool.assert_awaited_once()
    assert init_pool.await_args.args == (
        campaign_data.org.id,
        ["+15550001", "+15550002"],
    )
    assert init_pool.await_args.kwargs["channels"] == {"+15550001": 3}


# ---------------------------------------------------------------------------
# Dial path, with every collaborator stubbed
# ---------------------------------------------------------------------------


@pytest.fixture
def dial():
    instance = CampaignCallDispatcher()
    instance.CAPACITY_WAIT_TIMEOUT = 0
    campaign = SimpleNamespace(
        orchestrator_metadata=None,
        id=7,
        organization_id=3,
        workflow_id=11,
        created_by=5,
        telephony_configuration_id=13,
        rate_limit_per_second=1,
    )
    queued_run = SimpleNamespace(
        id=17,
        source_uuid="lead-1",
        context_variables={"phone_number": "+919999900000"},
    )
    slot = SimpleNamespace(slot_id="slot-1", scope_key=None)
    run = SimpleNamespace(id=19)
    provider = SimpleNamespace(
        PROVIDER_NAME="twilio",
        WEBHOOK_ENDPOINT="twiml",
        from_numbers=["+15550001"],
        initiate_call=AsyncMock(
            return_value=SimpleNamespace(provider_metadata={"call_id": "CA1"})
        ),
    )
    db = MagicMock()
    db.get_workflow = AsyncMock(return_value=SimpleNamespace(id=11))
    db.create_workflow_run = AsyncMock(return_value=run)
    db.update_workflow_run = AsyncMock()
    db.update_queued_run = AsyncMock()
    limiter = MagicMock()
    limiter.acquire_from_number = AsyncMock(return_value="+15550001::ch2")
    limiter.pool_slot_address = dispatcher_module.rate_limiter.pool_slot_address
    limiter.store_workflow_from_number_mapping = AsyncMock()
    limiter.get_workflow_from_number_mapping = AsyncMock(
        return_value=(3, "+15550001::ch2", 13)
    )
    limiter.release_from_number = AsyncMock(return_value=True)
    limiter.delete_workflow_from_number_mapping = AsyncMock()
    limiter.acquire_token = AsyncMock(return_value=True)
    concurrency = MagicMock()
    concurrency.bind_workflow_run = AsyncMock()
    concurrency.release_slot = AsyncMock()
    concurrency.release_workflow_run_slot = AsyncMock(return_value=True)
    breaker = MagicMock()
    breaker.pause_on_provider_error = AsyncMock()
    breaker.record_and_evaluate = AsyncMock()
    webhook_sync = MagicMock()
    webhook_sync.call_started = AsyncMock()
    quota = AsyncMock(return_value=SimpleNamespace(has_quota=True))
    with (
        patch.object(dispatcher_module, "db_client", db),
        patch.object(dispatcher_module, "rate_limiter", limiter),
        patch.object(dispatcher_module, "call_concurrency", concurrency),
        patch.object(dispatcher_module, "circuit_breaker", breaker),
        patch.object(dispatcher_module, "webhook_sync_calling", webhook_sync),
        patch.object(dispatcher_module, "authorize_workflow_run_start", quota),
        patch.object(
            dispatcher_module,
            "prepare_workflow_run_inputs",
            AsyncMock(return_value=SimpleNamespace(definition_id=23)),
        ),
        patch.object(
            dispatcher_module,
            "get_backend_endpoints",
            AsyncMock(return_value=("https://api.example", None)),
        ),
        patch.object(
            instance, "get_provider_for_campaign", AsyncMock(return_value=provider)
        ),
    ):
        yield SimpleNamespace(
            dispatcher=instance,
            campaign=campaign,
            queued_run=queued_run,
            slot=slot,
            run=run,
            provider=provider,
            db=db,
            limiter=limiter,
            concurrency=concurrency,
            breaker=breaker,
            webhook_sync=webhook_sync,
            quota=quota,
        )


async def _dispatch(d):
    return await d.dispatcher.dispatch_call(d.queued_run, d.campaign, d.slot)


@pytest.mark.asyncio
async def test_successful_dial_reserves_a_caller_id_channel(dial):
    assert await _dispatch(dial) is dial.run

    kwargs = dial.provider.initiate_call.await_args.kwargs
    assert kwargs["from_number"] == "+15550001"  # the number, not the channel slot
    assert kwargs["to_number"] == "+919999900000"
    initial_context = dial.db.create_workflow_run.await_args.kwargs["initial_context"]
    assert initial_context["caller_number"] == "+15550001"
    dial.limiter.store_workflow_from_number_mapping.assert_awaited_once_with(
        19, 3, "+15550001::ch2", telephony_configuration_id=13
    )
    dial.webhook_sync.call_started.assert_awaited_once_with(
        dial.queued_run, dial.run, 3
    )
    assert dial.quota.await_args.kwargs["campaign_id"] == 7
    dial.concurrency.release_slot.assert_not_awaited()
    dial.limiter.release_from_number.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_pool_releases_the_concurrency_slot(dial):
    dial.limiter.acquire_from_number.return_value = None

    with pytest.raises(PhoneNumberPoolExhaustedError):
        await _dispatch(dial)

    dial.db.create_workflow_run.assert_not_awaited()
    dial.provider.initiate_call.assert_not_awaited()
    dial.concurrency.release_slot.assert_awaited_once_with(dial.slot)
    dial.limiter.release_from_number.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "counts_as_outage"),
    [(401, False), (400, False), (503, True)],
)
async def test_rejected_dial_pauses_on_account_errors_and_counts_only_outages(
    status_code, counts_as_outage, dial
):
    # Shaped like the providers' own errors (Twilio raises HTTPException).
    error = HTTPException(status_code=status_code, detail='{"code": 21211}')
    dial.provider.initiate_call.side_effect = error

    with pytest.raises(HTTPException):
        await _dispatch(dial)

    dial.breaker.pause_on_provider_error.assert_awaited_once()
    pause = dial.breaker.pause_on_provider_error.await_args.kwargs
    assert pause["exception"] is error
    assert pause["provider"] == "twilio"
    assert pause["service"] == "telephony"
    if counts_as_outage:
        dial.breaker.record_and_evaluate.assert_awaited_once()
        record = dial.breaker.record_and_evaluate.await_args
        assert record.args == (7,)
        assert record.kwargs["reason"] == "telephony_unavailable"
    else:
        dial.breaker.record_and_evaluate.assert_not_awaited()
    # The mapped caller-ID channel and the concurrency slot both come back.
    dial.limiter.release_from_number.assert_awaited_once_with(
        3, "+15550001::ch2", telephony_configuration_id=13
    )
    dial.concurrency.release_slot.assert_awaited_once_with(dial.slot)


@pytest.mark.asyncio
async def test_setup_failure_before_dialing_never_touches_the_breaker(dial):
    dial.quota.return_value = SimpleNamespace(
        has_quota=False, error_message="Quota exceeded"
    )

    with pytest.raises(ValueError, match="Quota exceeded"):
        await _dispatch(dial)

    dial.provider.initiate_call.assert_not_awaited()
    dial.breaker.pause_on_provider_error.assert_not_awaited()
    dial.breaker.record_and_evaluate.assert_not_awaited()
    dial.concurrency.release_slot.assert_awaited_once_with(dial.slot)


@pytest.mark.asyncio
async def test_channel_taken_before_its_mapping_was_saved_is_released(dial):
    dial.concurrency.bind_workflow_run.side_effect = RuntimeError("redis down")
    dial.limiter.get_workflow_from_number_mapping.return_value = None

    with pytest.raises(RuntimeError):
        await _dispatch(dial)

    dial.limiter.store_workflow_from_number_mapping.assert_not_awaited()
    dial.limiter.release_from_number.assert_awaited_once_with(
        3, "+15550001::ch2", telephony_configuration_id=13
    )
    dial.concurrency.release_slot.assert_awaited_once_with(dial.slot)


@pytest.mark.asyncio
async def test_uncertain_dial_keeps_its_channel_and_slot(dial):
    dial.provider.initiate_call.side_effect = TimeoutError()

    with pytest.raises(TimeoutError) as raised:
        await _dispatch(dial)

    assert raised.value.dispatch_uncertain is True
    dial.limiter.release_from_number.assert_not_awaited()
    dial.concurrency.release_slot.assert_not_awaited()
    dial.breaker.pause_on_provider_error.assert_not_awaited()
    # A timed-out dial is how an outage looks: it counts once.
    dial.breaker.record_and_evaluate.assert_awaited_once()
    assert dial.breaker.record_and_evaluate.await_args.kwargs["reason"] == (
        "telephony_unavailable"
    )


@pytest.mark.asyncio
async def test_a_cancelled_dial_is_not_counted_as_an_outage(dial):
    dial.provider.initiate_call.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _dispatch(dial)

    dial.breaker.record_and_evaluate.assert_not_awaited()
    dial.concurrency.release_slot.assert_not_awaited()  # may still be live


@pytest.mark.asyncio
async def test_call_end_releases_slot_and_caller_id_channel(dial):
    assert await dial.dispatcher.release_call_slot(19) is True

    dial.concurrency.release_workflow_run_slot.assert_awaited_once_with(19)
    dial.limiter.release_from_number.assert_awaited_once_with(
        3, "+15550001::ch2", telephony_configuration_id=13
    )
    dial.limiter.delete_workflow_from_number_mapping.assert_awaited_once_with(19)
