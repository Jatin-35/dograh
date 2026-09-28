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
    assert (
        RateLimiter.pool_slot_address("sip:agent@pbx.example")
        == "sip:agent@pbx.example"
    )


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
        id=7,
        organization_id=5,
        workflow_id=10,
        created_by=1,
        telephony_configuration_id=42,
    )
    queued_run = SimpleNamespace(
        id=99,
        source_uuid="row-1",
        context_variables={"phone_number": "+919018737669"},
    )
    provider = SimpleNamespace(
        PROVIDER_NAME="voicelink",
        WEBHOOK_ENDPOINT="voicelink/events",
        initiate_call=AsyncMock(
            return_value=SimpleNamespace(call_id="c1", provider_metadata={})
        ),
    )
    run = SimpleNamespace(id=500, logs={})

    with (
        patch.object(module, "db_client") as db,
        patch.object(module, "rate_limiter") as rl,
        patch.object(module, "call_concurrency") as cc,
        patch.object(
            module,
            "authorize_workflow_run_start",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(has_quota=True),
        ),
        patch.object(
            module,
            "get_backend_endpoints",
            new_callable=AsyncMock,
            return_value=("https://api.example", "wss://api.example"),
        ),
        patch.object(
            dispatcher,
            "get_provider_for_campaign",
            new_callable=AsyncMock,
            return_value=provider,
        ),
        patch.object(
            dispatcher, "acquire_from_number", new_callable=AsyncMock, return_value=slot
        ),
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
        id=7,
        organization_id=5,
        workflow_id=10,
        created_by=1,
        telephony_configuration_id=42,
    )
    queued_run = SimpleNamespace(
        id=99,
        source_uuid="row-1",
        context_variables={"phone_number": "+919018737669"},
    )
    provider = SimpleNamespace(PROVIDER_NAME="voicelink")

    with (
        patch.object(module, "db_client") as db,
        patch.object(module, "rate_limiter") as rl,
        patch.object(module, "call_concurrency") as cc,
        patch.object(
            dispatcher,
            "get_provider_for_campaign",
            new_callable=AsyncMock,
            return_value=provider,
        ),
        patch.object(
            dispatcher, "acquire_from_number", new_callable=AsyncMock, return_value=slot
        ),
    ):
        rl.pool_slot_address = RateLimiter.pool_slot_address
        rl.release_from_number = AsyncMock()
        db.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(id=10))
        db.create_workflow_run = AsyncMock(side_effect=RuntimeError("db down"))
        cc.release_slot = AsyncMock()

        with pytest.raises(RuntimeError):
            await dispatcher.dispatch_call(
                queued_run, campaign, concurrency_slot=object()
            )

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
        db.get_active_channel_capacity_for_config = AsyncMock(return_value={"+911": 4})
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
        db.get_active_channel_capacity_for_config = AsyncMock(return_value={"+911": 10})
        assert await campaign_routes._get_from_numbers_count(5) == 10


# ---------------------------------------------------------------------------
# Editing channels: the organization-scoped phone number update
# ---------------------------------------------------------------------------


def test_channels_in_the_update_request_are_bounded_and_optional():
    from api.schemas.telephony_phone_number import PhoneNumberUpdateRequest

    assert PhoneNumberUpdateRequest().max_concurrent_calls is None
    assert PhoneNumberUpdateRequest(max_concurrent_calls=1).max_concurrent_calls == 1
    assert (
        PhoneNumberUpdateRequest(max_concurrent_calls=200).max_concurrent_calls == 200
    )
    for bad in (0, -1, 201):
        with pytest.raises(ValidationError):
            PhoneNumberUpdateRequest(max_concurrent_calls=bad)


def test_phone_number_response_defaults_to_one_channel():
    from api.schemas.telephony_phone_number import PhoneNumberResponse

    assert PhoneNumberResponse.model_fields["max_concurrent_calls"].default == 1


@pytest.fixture
async def two_orgs_with_numbers(setup_test_database):
    """Two organizations, each with a telephony config and one active number,
    plus an inactive number in org A. Real database."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.db import db_client
    from api.db.models import (
        OrganizationModel,
        TelephonyConfigurationModel,
        TelephonyPhoneNumberModel,
        UserModel,
    )

    engine = create_async_engine(setup_test_database, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original_engine, original_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, factory

    ids = {}
    async with factory() as session:
        for key in ("a", "b"):
            org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            user = UserModel(
                provider_id=f"test-user-{uuid.uuid4().hex[:8]}",
                selected_organization_id=org.id,
            )
            cfg = TelephonyConfigurationModel(
                organization_id=org.id,
                name=f"cfg-{key}",
                provider="twilio",
                credentials={},
            )
            session.add_all([user, cfg])
            await session.flush()
            number = f"+9190000{_unique_id():05d}"[:13]
            row = TelephonyPhoneNumberModel(
                organization_id=org.id,
                telephony_configuration_id=cfg.id,
                address=number,
                address_normalized=number,
                address_type="pstn",
            )
            session.add(row)
            await session.flush()
            ids[key] = SimpleNamespace(
                org=org.id, user=user.id, cfg=cfg.id, number=row.id, address=number
            )
        inactive = TelephonyPhoneNumberModel(
            organization_id=ids["a"].org,
            telephony_configuration_id=ids["a"].cfg,
            address="+919999999990",
            address_normalized="+919999999990",
            address_type="pstn",
            is_active=False,
            max_concurrent_calls=9,
        )
        session.add(inactive)
        await session.commit()

    yield ids

    db_client.engine, db_client.async_session = original_engine, original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_numbers_start_with_one_channel(two_orgs_with_numbers):
    from api.db import db_client

    a = two_orgs_with_numbers["a"]
    row = await db_client.get_phone_number(a.number)
    assert row.max_concurrent_calls == 1


@pytest.mark.asyncio
async def test_update_sets_channels_and_capacity_reflects_it(two_orgs_with_numbers):
    from api.db import db_client

    a = two_orgs_with_numbers["a"]
    row = await db_client.update_phone_number(a.number, a.cfg, max_concurrent_calls=6)
    assert row.max_concurrent_calls == 6
    # Only active numbers count toward what campaigns can use.
    assert await db_client.get_active_channel_capacity_for_config(a.cfg) == {
        a.address: 6
    }


@pytest.mark.asyncio
async def test_other_updates_leave_channels_alone(two_orgs_with_numbers):
    from api.db import db_client

    a = two_orgs_with_numbers["a"]
    await db_client.update_phone_number(a.number, a.cfg, max_concurrent_calls=4)
    row = await db_client.update_phone_number(a.number, a.cfg, label="Sales line")
    assert row.label == "Sales line"
    assert row.max_concurrent_calls == 4


@pytest.mark.asyncio
async def test_a_number_cannot_be_updated_through_another_config(two_orgs_with_numbers):
    from api.db import db_client

    a, b = two_orgs_with_numbers["a"], two_orgs_with_numbers["b"]
    assert (
        await db_client.update_phone_number(a.number, b.cfg, max_concurrent_calls=50)
        is None
    )
    assert (await db_client.get_phone_number(a.number)).max_concurrent_calls == 1


@pytest.mark.asyncio
async def test_a_user_cannot_change_another_organizations_channels(
    two_orgs_with_numbers,
):
    from fastapi import HTTPException

    from api.routes import organization as org_routes
    from api.schemas.telephony_phone_number import PhoneNumberUpdateRequest

    a, b = two_orgs_with_numbers["a"], two_orgs_with_numbers["b"]
    intruder = SimpleNamespace(id=b.user, selected_organization_id=b.org)
    with pytest.raises(HTTPException) as exc:
        await org_routes.update_phone_number(
            config_id=a.cfg,
            phone_number_id=a.number,
            request=PhoneNumberUpdateRequest(max_concurrent_calls=50),
            user=intruder,
        )
    assert exc.value.status_code == 404

    from api.db import db_client

    assert (await db_client.get_phone_number(a.number)).max_concurrent_calls == 1


@pytest.mark.asyncio
async def test_an_org_member_can_change_their_own_channels(two_orgs_with_numbers):
    from api.db import db_client
    from api.routes import organization as org_routes
    from api.schemas.telephony_phone_number import PhoneNumberUpdateRequest

    a = two_orgs_with_numbers["a"]
    member = SimpleNamespace(id=a.user, selected_organization_id=a.org)
    with patch.object(
        org_routes,
        "_sync_inbound_for_phone_number",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await org_routes.update_phone_number(
            config_id=a.cfg,
            phone_number_id=a.number,
            request=PhoneNumberUpdateRequest(max_concurrent_calls=8),
            user=member,
        )
    assert response.max_concurrent_calls == 8
    assert (await db_client.get_phone_number(a.number)).max_concurrent_calls == 8
