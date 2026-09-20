"""
Tests for the campaign wallet billing hooks (api/services/campaign/campaign_billing.py)
and their wiring into the campaign lifecycle:

1. reserve_campaign_wallet(): no-ops when billing isn't configured (org rate or
   workflow avg-duration unset) or there's nothing to bill, computes the right
   estimate, and converts InsufficientBalanceError into a user-facing ValueError.
2. reconcile_campaign_wallet(): no-ops when the campaign was never reserved,
   delegates correctly to WalletClient.wallet_reconcile_campaign, and never
   raises (a reconciliation failure must not block/undo a campaign's own
   state transition).
3. CampaignRunnerService.stop_campaign() actually triggers a real refund via
   this wiring, end to end.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CampaignModel,
    OrganizationModel,
    UserModel,
    WalletTransactionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.enums import WalletTransactionType
from api.services.campaign.campaign_billing import (
    reconcile_campaign_wallet,
    reserve_campaign_wallet,
)
from api.services.campaign.runner import CampaignRunnerService


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    """Real session factory, patched onto db_client for these tests."""
    from api.db import db_client

    test_url = setup_test_database
    engine = create_async_engine(test_url, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    yield session_factory

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


@dataclass
class Fixture:
    organization_id: int
    user_id: int
    workflow_id: int
    campaign_id: int


async def _create_campaign(
    db_session_factory,
    *,
    price_per_minute=None,
    avg_call_duration_minutes=None,
    billing_mode="per_minute",
    price_per_call=None,
    pulse_seconds=0,
    wallet_balance=Decimal("0"),
    credit_limit=Decimal("0"),
    state="created",
    wallet_enabled=True,
) -> Fixture:
    async with db_session_factory() as session:
        org = OrganizationModel(
            provider_id=f"test-org-{uuid.uuid4().hex[:8]}",
            wallet_balance=wallet_balance,
            credit_limit=credit_limit,
            wallet_enabled=wallet_enabled,
        )
        session.add(org)
        await session.flush()

        user = UserModel(
            provider_id=f"test-user-{uuid.uuid4().hex[:8]}",
            selected_organization_id=org.id,
        )
        session.add(user)
        await session.flush()

        workflow = WorkflowModel(
            name=f"test-workflow-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            organization_id=org.id,
            workflow_definition={"nodes": [], "edges": []},
            template_context_variables={},
            avg_call_duration_minutes=avg_call_duration_minutes,
            price_per_minute=price_per_minute,
            billing_mode=billing_mode,
            price_per_call=price_per_call,
            pulse_seconds=pulse_seconds,
        )
        session.add(workflow)
        await session.flush()

        campaign = CampaignModel(
            name=f"test-campaign-{uuid.uuid4().hex[:8]}",
            organization_id=org.id,
            workflow_id=workflow.id,
            created_by=user.id,
            source_type="test",
            source_id="test-source",
            state=state,
            rate_limit_per_second=100,
        )
        session.add(campaign)
        await session.flush()
        await session.commit()

        return Fixture(
            organization_id=org.id,
            user_id=user.id,
            workflow_id=workflow.id,
            campaign_id=campaign.id,
        )


async def _cleanup(db_session_factory, fx: Fixture) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WalletTransactionModel).where(
                WalletTransactionModel.organization_id == fx.organization_id
            )
        )
        await session.execute(
            delete(WorkflowRunModel).where(WorkflowRunModel.workflow_id == fx.workflow_id)
        )
        await session.execute(
            delete(CampaignModel).where(CampaignModel.id == fx.campaign_id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == fx.workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == fx.user_id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == fx.organization_id)
        )
        await session.commit()


@pytest.fixture
async def campaign_factory(db_session_factory):
    created: list[Fixture] = []

    async def _factory(**kwargs) -> Fixture:
        fx = await _create_campaign(db_session_factory, **kwargs)
        created.append(fx)
        return fx

    yield _factory

    for fx in created:
        await _cleanup(db_session_factory, fx)


async def _get_campaign_model(fx: Fixture) -> CampaignModel:
    from api.db import db_client

    return await db_client.get_campaign_by_id(fx.campaign_id)


async def _get_org(db_session_factory, organization_id: int) -> OrganizationModel:
    async with db_session_factory() as session:
        return await session.get(OrganizationModel, organization_id)


async def _create_workflow_run(db_session_factory, workflow_id: int, campaign_id: int) -> int:
    async with db_session_factory() as session:
        run = WorkflowRunModel(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            campaign_id=campaign_id,
            mode="voice",
            initial_context={},
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return run.id


class TestReserveCampaignWallet:
    async def test_noop_when_price_per_minute_not_set(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(
            price_per_minute=None, avg_call_duration_minutes=Decimal("3")
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id
                )
            )
            assert result.scalars().first() is None

    async def test_noop_when_avg_call_duration_not_set(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=None,
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id
                )
            )
            assert result.scalars().first() is None

    async def test_noop_when_zero_rows(self, db_session_factory, campaign_factory):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=0)

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id
                )
            )
            assert result.scalars().first() is None

    async def test_reserves_estimated_cost(self, db_session_factory, campaign_factory):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        # estimated_cost = 10 rows * 3 min/row * 2/min = 60
        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("940.0000")

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id,
                    WalletTransactionModel.type
                    == WalletTransactionType.CAMPAIGN_RESERVE.value,
                )
            )
            tx = result.scalar_one()
            assert tx.amount == Decimal("-60.0000")

    async def test_reservation_rounds_average_call_up_to_pulse(
        self, db_session_factory, campaign_factory
    ):
        # A 1.5-minute (90s) average on a 60s pulse bills as 2 minutes.
        fx = await campaign_factory(
            price_per_minute=Decimal("6.00"),
            avg_call_duration_minutes=Decimal("1.50"),
            pulse_seconds=60,
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        # estimated_cost = 10 rows * 2 billed min/row * 6/min = 120
        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("880.0000")

    async def test_noop_when_wallet_disabled(self, db_session_factory, campaign_factory):
        # A rate is configured, but the master switch is off.
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
            wallet_enabled=False,
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("1000.0000")  # unchanged — no reservation

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id
                )
            )
            assert result.scalars().first() is None

    async def test_raises_value_error_on_insufficient_balance(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("10"),
        )
        campaign = await _get_campaign_model(fx)

        with pytest.raises(ValueError, match="Insufficient wallet balance"):
            await reserve_campaign_wallet(campaign, total_rows=10)

        # No reservation should have been recorded on the failed attempt.
        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("10.0000")


class TestReserveCampaignWalletPerCall:
    """billing_mode=per_call sizes the reservation off rows * price_per_call
    alone — no avg_call_duration_minutes involved."""

    async def test_noop_when_price_per_call_not_set(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(
            billing_mode="per_call", price_per_call=None, wallet_balance=Decimal("1000")
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id
                )
            )
            assert result.scalars().first() is None

    async def test_reserves_rows_times_flat_rate(self, db_session_factory, campaign_factory):
        fx = await campaign_factory(
            billing_mode="per_call",
            price_per_call=Decimal("5.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        # estimated_cost = 10 rows * 5.00/call = 50, no avg-duration factor
        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("950.0000")

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id,
                    WalletTransactionModel.type
                    == WalletTransactionType.CAMPAIGN_RESERVE.value,
                )
            )
            tx = result.scalar_one()
            assert tx.amount == Decimal("-50.0000")

    async def test_ignores_avg_call_duration_when_present(
        self, db_session_factory, campaign_factory
    ):
        # A stray avg_call_duration_minutes (e.g. left over from a prior
        # per_minute configuration) must not factor into a per_call estimate.
        fx = await campaign_factory(
            billing_mode="per_call",
            price_per_call=Decimal("5.00"),
            avg_call_duration_minutes=Decimal("99.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)

        await reserve_campaign_wallet(campaign, total_rows=10)

        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("950.0000")  # still 10 * 5.00, not * 99


class TestReconcileCampaignWallet:
    async def test_noop_when_never_reserved(self, campaign_factory):
        fx = await campaign_factory()

        # Must not raise even though there's no reservation to true up.
        await reconcile_campaign_wallet(fx.campaign_id)

    async def test_noop_when_campaign_missing(self):
        # Must not raise for a nonexistent campaign_id either.
        await reconcile_campaign_wallet(999_999_999)

    async def test_refunds_unused_reservation(self, db_session_factory, campaign_factory):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)
        await reserve_campaign_wallet(campaign, total_rows=10)  # reserves 60

        run_id = await _create_workflow_run(
            db_session_factory, fx.workflow_id, fx.campaign_id
        )
        from api.db import db_client

        await db_client.wallet_record_campaign_call_cost(
            organization_id=fx.organization_id,
            campaign_id=fx.campaign_id,
            workflow_run_id=run_id,
            duration_seconds=150,  # 2.5 min * 2/min = 5 spent
            price_per_minute=Decimal("2.00"),
        )

        await reconcile_campaign_wallet(fx.campaign_id)

        # 1000 - 60 (reserve) + 55 (refund of the unused 60 - 5) = 995
        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("995.0000")

        async with db_session_factory() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == fx.campaign_id,
                    WalletTransactionModel.type
                    == WalletTransactionType.CAMPAIGN_RECONCILE.value,
                )
            )
            tx = result.scalar_one()
            assert tx.amount == Decimal("55.0000")

    async def test_is_idempotent(self, db_session_factory, campaign_factory):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
        )
        campaign = await _get_campaign_model(fx)
        await reserve_campaign_wallet(campaign, total_rows=10)  # reserves 60

        await reconcile_campaign_wallet(fx.campaign_id)
        await reconcile_campaign_wallet(fx.campaign_id)  # second call must no-op

        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("1000.0000")  # full refund, only once


class TestStopCampaignRefundsReservation:
    """End-to-end: stopping a reserved campaign actually refunds it, via the
    real reconcile_campaign_wallet() wiring in CampaignRunnerService.stop_campaign()."""

    async def test_stop_triggers_full_refund_when_no_calls_made(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(
            price_per_minute=Decimal("2.00"),
            avg_call_duration_minutes=Decimal("3.00"),
            wallet_balance=Decimal("1000"),
            state="running",
        )
        campaign = await _get_campaign_model(fx)
        await reserve_campaign_wallet(campaign, total_rows=10)  # reserves 60

        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("940.0000")

        service = CampaignRunnerService()
        await service.stop_campaign(fx.campaign_id)

        org = await _get_org(db_session_factory, fx.organization_id)
        assert org.wallet_balance == Decimal("1000.0000")  # fully refunded

    async def test_stop_is_harmless_when_never_reserved(
        self, db_session_factory, campaign_factory
    ):
        fx = await campaign_factory(state="running")  # no price_per_minute set

        service = CampaignRunnerService()
        await service.stop_campaign(fx.campaign_id)  # must not raise

        async with db_session_factory() as session:
            result = await session.execute(
                select(CampaignModel).where(CampaignModel.id == fx.campaign_id)
            )
            assert result.scalar_one().state == "cancelled"
