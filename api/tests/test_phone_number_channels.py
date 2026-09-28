"""Channels per phone number: a number with N channels carries N campaign calls
at once. Default 1 keeps the behaviour from before channels existed.

The pool tests run against real Redis (the pool is Lua scripts) with unique
keys per test, cleaned up afterwards.
"""

import asyncio
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from api.services.campaign.rate_limiter import RateLimiter

requires_redis = pytest.mark.skipif(
    "REDIS_URL" not in os.environ,
    reason="Requires Redis (set REDIS_URL via .env.test)",
)


def _unique_id() -> int:
    return uuid.uuid4().int % 10_000_000


@pytest.fixture
async def pool():
    """A RateLimiter on the real Redis, with a unique (org, config) pool key
    that is deleted after the test."""
    rl = RateLimiter()
    org_id, config_id = _unique_id(), _unique_id()
    yield rl, org_id, config_id
    redis_client = await rl._get_redis()
    await redis_client.delete(rl._from_number_pool_key(org_id, config_id))
    await rl.close()


async def _drain(rl, org_id, config_id) -> list[str]:
    """Acquire until the pool is empty; returns the slots handed out."""
    taken = []
    while (slot := await rl.acquire_from_number(org_id, config_id)) is not None:
        taken.append(slot)
        assert len(taken) < 1000, "pool never ran dry"
    return taken


# ---------------------------------------------------------------------------
# Slot naming
# ---------------------------------------------------------------------------


def test_a_slot_maps_back_to_its_number():
    assert RateLimiter.pool_slot_address("+919429396634") == "+919429396634"
    assert RateLimiter.pool_slot_address("+919429396634::ch7") == "+919429396634"
    assert RateLimiter.pool_slot_address("sip:agent@pbx.example") == "sip:agent@pbx.example"


def test_slot_one_is_the_bare_number_so_old_mappings_still_release():
    assert RateLimiter._pool_slots("+911", 1) == ["+911"]
    assert RateLimiter._pool_slots("+911", 3) == ["+911", "+911::ch2", "+911::ch3"]


# ---------------------------------------------------------------------------
# Pool behaviour on real Redis
# ---------------------------------------------------------------------------


@requires_redis
@pytest.mark.asyncio
async def test_without_channels_each_number_carries_one_call(pool):
    rl, org_id, config_id = pool
    numbers = ["+919000000001", "+919000000002"]
    await rl.initialize_from_number_pool(org_id, numbers, config_id)

    assert sorted(await _drain(rl, org_id, config_id)) == numbers


@requires_redis
@pytest.mark.asyncio
async def test_one_number_with_five_channels_carries_five_calls(pool):
    rl, org_id, config_id = pool
    number = "+919429396634"
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 5}
    )

    taken = await _drain(rl, org_id, config_id)
    assert len(taken) == 5
    assert len(set(taken)) == 5
    assert {rl.pool_slot_address(s) for s in taken} == {number}


@requires_redis
@pytest.mark.asyncio
async def test_many_numbers_with_many_channels_add_up(pool):
    rl, org_id, config_id = pool
    channels = {"+919000000001": 3, "+919000000002": 2, "+919000000003": 1}
    await rl.initialize_from_number_pool(
        org_id, list(channels), config_id, channels=channels
    )

    taken = await _drain(rl, org_id, config_id)
    per_number: dict[str, int] = {}
    for slot in taken:
        number = rl.pool_slot_address(slot)
        per_number[number] = per_number.get(number, 0) + 1
    assert per_number == channels


@requires_redis
@pytest.mark.asyncio
async def test_a_released_channel_is_reusable(pool):
    rl, org_id, config_id = pool
    number = "+919000000001"
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 2}
    )
    first, second = await _drain(rl, org_id, config_id)

    assert await rl.release_from_number(org_id, second, config_id)
    assert await rl.acquire_from_number(org_id, config_id) == second
    assert await rl.acquire_from_number(org_id, config_id) is None
    assert first != second


@requires_redis
@pytest.mark.asyncio
async def test_reinitializing_keeps_busy_channels_busy(pool):
    # process_batch re-initializes the pool every batch; calls in flight must
    # not be handed out twice.
    rl, org_id, config_id = pool
    number = "+919000000001"
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 3}
    )
    busy = [await rl.acquire_from_number(org_id, config_id) for _ in range(2)]

    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 3}
    )

    remaining = await _drain(rl, org_id, config_id)
    assert len(remaining) == 1
    assert remaining[0] not in busy


@requires_redis
@pytest.mark.asyncio
async def test_reducing_channels_never_takes_a_channel_from_a_live_call(pool):
    rl, org_id, config_id = pool
    number = "+919000000001"
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 3}
    )
    busy = [await rl.acquire_from_number(org_id, config_id) for _ in range(3)]

    # Superadmin lowers the number to 1 channel while all 3 calls are live.
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 1}
    )
    redis_client = await rl._get_redis()
    key = rl._from_number_pool_key(org_id, config_id)
    assert set(await redis_client.zrange(key, 0, -1)) == set(busy)

    # The calls end: releasing works for every one of them ...
    for slot in busy:
        assert await rl.release_from_number(org_id, slot, config_id)
    # ... and the next batch drops the channels that no longer exist.
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 1}
    )
    assert await redis_client.zrange(key, 0, -1) == [number]
    assert await _drain(rl, org_id, config_id) == [number]


@requires_redis
@pytest.mark.asyncio
async def test_a_removed_number_stops_being_handed_out(pool):
    rl, org_id, config_id = pool
    kept, removed = "+919000000001", "+919000000002"
    await rl.initialize_from_number_pool(
        org_id, [kept, removed], config_id, channels={kept: 1, removed: 2}
    )

    await rl.initialize_from_number_pool(org_id, [kept], config_id)

    assert await _drain(rl, org_id, config_id) == [kept]


@requires_redis
@pytest.mark.asyncio
async def test_a_call_started_before_channels_existed_still_releases(pool):
    # A call dispatched by the previous release holds the bare number as its
    # slot; after the deploy that slot is channel 1 of the same number.
    rl, org_id, config_id = pool
    number = "+919000000001"
    await rl.initialize_from_number_pool(org_id, [number], config_id)
    legacy_slot = await rl.acquire_from_number(org_id, config_id)
    assert legacy_slot == number

    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 2}
    )
    assert await rl.release_from_number(org_id, legacy_slot, config_id)
    assert len(await _drain(rl, org_id, config_id)) == 2


@requires_redis
@pytest.mark.asyncio
async def test_concurrent_dispatchers_never_exceed_the_channels(pool):
    rl, org_id, config_id = pool
    number = "+919000000001"
    await rl.initialize_from_number_pool(
        org_id, [number], config_id, channels={number: 5}
    )

    results = await asyncio.gather(
        *(rl.acquire_from_number(org_id, config_id) for _ in range(20))
    )
    won = [r for r in results if r is not None]
    assert len(won) == 5
    assert len(set(won)) == 5


# ---------------------------------------------------------------------------
# Dispatcher: dials the number, locks and releases the slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_dials_the_number_and_records_the_slot():
    from api.services.campaign import campaign_call_dispatcher as module

    dispatcher = module.CampaignCallDispatcher()
    slot = "+919429396634::ch3"
    campaign = SimpleNamespace(
        id=7, organization_id=5, workflow_id=10, created_by=1,
        telephony_configuration_id=42,
    )
    queued_run = SimpleNamespace(
        id=99, source_uuid="row-1", context_variables={"phone_number": "+919018737669"},
    )
    provider = SimpleNamespace(
        PROVIDER_NAME="voicelink", WEBHOOK_ENDPOINT="voicelink/events",
        initiate_call=AsyncMock(
            return_value=SimpleNamespace(call_id="c1", provider_metadata={})
        ),
    )
    run = SimpleNamespace(id=500, logs={})

    with (
        patch.object(module, "db_client") as db,
        patch.object(module, "rate_limiter") as rl,
        patch.object(module, "call_concurrency") as cc,
        patch.object(module, "authorize_workflow_run_start", new_callable=AsyncMock,
                     return_value=SimpleNamespace(has_quota=True)),
        patch.object(module, "get_backend_endpoints", new_callable=AsyncMock,
                     return_value=("https://api.example", "wss://api.example")),
        patch.object(dispatcher, "get_provider_for_campaign", new_callable=AsyncMock,
                     return_value=provider),
        patch.object(dispatcher, "acquire_from_number", new_callable=AsyncMock,
                     return_value=slot),
    ):
        rl.pool_slot_address = RateLimiter.pool_slot_address
        rl.store_workflow_from_number_mapping = AsyncMock()
        db.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(id=10))
        db.create_workflow_run = AsyncMock(return_value=run)
        db.update_workflow_run = AsyncMock()
        cc.bind_workflow_run = AsyncMock()

        await dispatcher.dispatch_call(queued_run, campaign, concurrency_slot=object())

    kwargs = provider.initiate_call.await_args.kwargs
    assert kwargs["from_number"] == "+919429396634"
    assert (
        db.create_workflow_run.await_args.kwargs["initial_context"]["caller_number"]
        == "+919429396634"
    )
    mapping_args = rl.store_workflow_from_number_mapping.await_args.args
    assert mapping_args[2] == slot  # the slot is what gets released later


@pytest.mark.asyncio
async def test_failed_dispatch_releases_the_slot_it_took():
    from api.services.campaign import campaign_call_dispatcher as module

    dispatcher = module.CampaignCallDispatcher()
    slot = "+919429396634::ch2"
    campaign = SimpleNamespace(
        id=7, organization_id=5, workflow_id=10, created_by=1,
        telephony_configuration_id=42,
    )
    queued_run = SimpleNamespace(
        id=99, source_uuid="row-1", context_variables={"phone_number": "+919018737669"},
    )
    provider = SimpleNamespace(PROVIDER_NAME="voicelink")

    with (
        patch.object(module, "db_client") as db,
        patch.object(module, "rate_limiter") as rl,
        patch.object(module, "call_concurrency") as cc,
        patch.object(dispatcher, "get_provider_for_campaign", new_callable=AsyncMock,
                     return_value=provider),
        patch.object(dispatcher, "acquire_from_number", new_callable=AsyncMock,
                     return_value=slot),
    ):
        rl.pool_slot_address = RateLimiter.pool_slot_address
        rl.release_from_number = AsyncMock()
        db.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(id=10))
        db.create_workflow_run = AsyncMock(side_effect=RuntimeError("db down"))
        cc.release_slot = AsyncMock()

        with pytest.raises(RuntimeError):
            await dispatcher.dispatch_call(queued_run, campaign, concurrency_slot=object())

    rl.release_from_number.assert_awaited_once_with(
        5, slot, telephony_configuration_id=42
    )


@pytest.mark.asyncio
async def test_channel_capacity_falls_back_to_the_org_default_config():
    from api.services.campaign import campaign_call_dispatcher as module

    dispatcher = module.CampaignCallDispatcher()
    legacy = SimpleNamespace(organization_id=5, telephony_configuration_id=None)
    with patch.object(module, "db_client") as db:
        db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=42)
        )
        db.get_active_channel_capacity_for_config = AsyncMock(
            return_value={"+911": 4}
        )
        assert await dispatcher.get_channel_capacity_for_campaign(legacy) == {"+911": 4}
    db.get_active_channel_capacity_for_config.assert_awaited_once_with(42)


# ---------------------------------------------------------------------------
# Campaign max_concurrency is validated against total channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_campaign_concurrency_limit_counts_channels_not_numbers():
    from api.routes import campaign as campaign_routes

    with patch.object(campaign_routes, "db_client") as db:
        db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=42)
        )
        db.get_active_channel_capacity_for_config = AsyncMock(
            return_value={"+911": 10}
        )
        assert await campaign_routes._get_from_numbers_count(5) == 10


# ---------------------------------------------------------------------------
# Superadmin endpoint
# ---------------------------------------------------------------------------


def test_channels_request_is_bounded():
    from api.routes.superuser import UpdatePhoneNumberChannelsRequest

    assert UpdatePhoneNumberChannelsRequest(max_concurrent_calls=1)
    assert UpdatePhoneNumberChannelsRequest(max_concurrent_calls=200)
    for bad in (0, -1, 201):
        with pytest.raises(ValidationError):
            UpdatePhoneNumberChannelsRequest(max_concurrent_calls=bad)


def test_channels_endpoint_requires_a_superuser():
    from fastapi.params import Depends as DependsParam

    from api.routes.superuser import update_phone_number_channels
    from api.services.auth.depends import get_superuser

    import inspect

    user_param = inspect.signature(update_phone_number_channels).parameters["user"]
    assert isinstance(user_param.default, DependsParam)
    assert user_param.default.dependency is get_superuser


@pytest.mark.asyncio
async def test_channels_endpoint_returns_404_for_an_unknown_number():
    from fastapi import HTTPException

    from api.routes import superuser as superuser_routes

    with patch.object(superuser_routes, "db_client") as db:
        db.set_phone_number_channels = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await superuser_routes.update_phone_number_channels(
                123,
                superuser_routes.UpdatePhoneNumberChannelsRequest(max_concurrent_calls=4),
                user=SimpleNamespace(id=1),
            )
    assert exc.value.status_code == 404


def test_org_members_cannot_set_channels_through_the_normal_update():
    # Channels are superadmin-only: the org-facing update request has no field
    # for them, so an org member's PATCH cannot change it.
    from api.schemas.telephony_phone_number import (
        PhoneNumberResponse,
        PhoneNumberUpdateRequest,
    )

    assert "max_concurrent_calls" not in PhoneNumberUpdateRequest.model_fields
    assert PhoneNumberResponse.model_fields["max_concurrent_calls"].default == 1
