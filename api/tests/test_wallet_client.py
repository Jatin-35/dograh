"""
Tests for WalletClient — the self-hosted prepaid wallet ledger.

These verify:
1. topup / adjust move the balance correctly and write the right ledger row.
2. debit_for_call computes exact fractional-minute cost, decrements the
   balance, is idempotent per workflow_run_id, and no-ops when billing
   isn't configured (price_per_minute unset).
3. record_campaign_call_cost writes a ledger row but never touches the
   balance, and is idempotent per workflow_run_id.
4. reserve_for_campaign enforces the credit_limit floor, is idempotent per
   campaign, and the reservation amount is exactly what left the wallet.
5. cancel_reservation and reconcile_campaign share one reconciliation slot
   per campaign (at most one can ever apply) — refund when under budget,
   debit the overage when over, zero-diff when exact.
6. The concurrency claim itself: two campaigns launched at the same
   instant against a wallet that can only afford one must not both
   succeed — this is the actual race the whole reservation design exists
   to prevent, proven empirically rather than trusted from the code
   reading alone.
"""

import asyncio
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
from api.db.wallet_client import (
    InsufficientBalanceError,
    WalletClient,
    billable_seconds,
    compute_call_cost,
    resolve_call_cost,
)


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
class OrgIds:
    organization_id: int
    user_id: int
    workflow_id: int


async def _create_org(
    db_session_factory,
    *,
    wallet_balance: Decimal = Decimal("0"),
    credit_limit: Decimal = Decimal("0"),
    price_per_minute=None,
    avg_call_duration_minutes=None,
    billing_mode: str = "per_minute",
    price_per_call=None,
    wallet_enabled: bool = True,
) -> OrgIds:
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
        )
        session.add(workflow)
        await session.flush()
        await session.commit()

        return OrgIds(organization_id=org.id, user_id=user.id, workflow_id=workflow.id)


async def _create_workflow_run(db_session_factory, workflow_id: int) -> int:
    async with db_session_factory() as session:
        run = WorkflowRunModel(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            mode="voice",
            initial_context={},
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return run.id


async def _create_campaign(db_session_factory, ids: OrgIds, state: str = "running") -> int:
    async with db_session_factory() as session:
        campaign = CampaignModel(
            name=f"test-campaign-{uuid.uuid4().hex[:8]}",
            organization_id=ids.organization_id,
            workflow_id=ids.workflow_id,
            created_by=ids.user_id,
            source_type="test",
            source_id="test-source",
            state=state,
            rate_limit_per_second=100,
        )
        session.add(campaign)
        await session.flush()
        await session.commit()
        return campaign.id


async def _get_org(db_session_factory, organization_id: int) -> OrganizationModel:
    async with db_session_factory() as session:
        result = await session.execute(
            select(OrganizationModel).where(OrganizationModel.id == organization_id)
        )
        return result.scalar_one()


async def _cleanup(db_session_factory, ids: OrgIds) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WalletTransactionModel).where(
                WalletTransactionModel.organization_id == ids.organization_id
            )
        )
        await session.execute(
            delete(WorkflowRunModel).where(WorkflowRunModel.workflow_id == ids.workflow_id)
        )
        await session.execute(
            delete(CampaignModel).where(CampaignModel.organization_id == ids.organization_id)
        )
        await session.execute(delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id))
        await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == ids.organization_id)
        )
        await session.commit()


@pytest.fixture
async def org_factory(db_session_factory):
    """Yields a factory to create orgs (+ user + workflow) with given
    wallet settings; cleans up everything it created, regardless of
    outcome."""
    created: list[OrgIds] = []

    async def _factory(**kwargs) -> OrgIds:
        ids = await _create_org(db_session_factory, **kwargs)
        created.append(ids)
        return ids

    yield _factory

    for ids in created:
        await _cleanup(db_session_factory, ids)


@pytest.fixture
def wallet_client(db_session_factory):
    """A WalletClient pointed at the test database. BaseDBClient.__init__
    opens its own engine against DATABASE_URL (the dev DB) by default —
    override it with the test session factory so this actually runs
    against test_db."""
    client = WalletClient()
    client.async_session = db_session_factory
    return client


class TestComputeCallCost:
    def test_exact_fractional_minutes(self):
        # 90 seconds = 1.5 minutes, at 2.00/min = 3.00
        assert compute_call_cost(90, Decimal("2.00")) == Decimal("3.0000")

    def test_sub_minute_call(self):
        # 30 seconds = 0.5 minutes, at 3.00/min = 1.50
        assert compute_call_cost(30, Decimal("3.00")) == Decimal("1.5000")

    def test_rounds_to_four_decimal_places(self):
        # 61 seconds at 1.00/min = 1.01666...67 -> rounds to 1.0167
        assert compute_call_cost(61, Decimal("1.00")) == Decimal("1.0167")


class TestPulseBilling:
    """Pulse billing rounds talk time up to a whole number of pulses before
    the per-minute rate applies. The expected charges are the worked
    examples agreed with the business at ₹6/min, so a change that breaks
    them changes what clients are actually billed."""

    RATE = Decimal("6.00")

    @pytest.mark.parametrize(
        ("pulse", "expected"),
        [
            # 2m 7s / 2m 19s / 2m 38s / 2m 49s
            (0, ["12.7000", "13.9000", "15.8000", "16.9000"]),
            (15, ["13.5000", "15.0000", "16.5000", "18.0000"]),
            (30, ["15.0000", "15.0000", "18.0000", "18.0000"]),
            (45, ["13.5000", "18.0000", "18.0000", "18.0000"]),
            (60, ["18.0000", "18.0000", "18.0000", "18.0000"]),
        ],
    )
    def test_agreed_worked_examples(self, pulse, expected):
        charges = [
            compute_call_cost(seconds, self.RATE, pulse) for seconds in (127, 139, 158, 169)
        ]
        assert charges == [Decimal(e) for e in expected]

    def test_pulse_zero_is_unchanged_pay_as_you_go(self):
        assert compute_call_cost(61, Decimal("1.00"), 0) == compute_call_cost(
            61, Decimal("1.00")
        )

    def test_exact_pulse_boundary_is_not_rounded_up(self):
        assert billable_seconds(60, 60) == Decimal(60)
        assert billable_seconds(90, 45) == Decimal(90)

    def test_any_part_of_a_pulse_is_a_full_pulse(self):
        assert billable_seconds(60.001, 60) == Decimal(120)

    def test_no_grace_period_for_very_short_calls(self):
        # A 2-second answered call still bills one full pulse.
        assert billable_seconds(2, 60) == Decimal(60)
        assert compute_call_cost(2, self.RATE, 60) == Decimal("6.0000")

    def test_pulse_does_not_affect_per_call_mode(self):
        cost = resolve_call_cost(
            billing_mode="per_call",
            duration_seconds=127,
            price_per_minute=None,
            price_per_call=Decimal("7.50"),
            pulse_seconds=60,
        )
        assert cost == Decimal("7.5000")


class TestResolveCallCost:
    def test_per_minute_mode_ignores_duration_via_rate(self):
        cost = resolve_call_cost(
            billing_mode="per_minute",
            duration_seconds=90,
            price_per_minute=Decimal("2.00"),
            price_per_call=None,
        )
        assert cost == Decimal("3.0000")

    def test_per_call_mode_is_flat_regardless_of_duration(self):
        short_call = resolve_call_cost(
            billing_mode="per_call",
            duration_seconds=5,
            price_per_minute=None,
            price_per_call=Decimal("7.50"),
        )
        long_call = resolve_call_cost(
            billing_mode="per_call",
            duration_seconds=600,
            price_per_minute=None,
            price_per_call=Decimal("7.50"),
        )
        assert short_call == Decimal("7.5000")
        assert long_call == Decimal("7.5000")


class TestTopupAndAdjust:
    async def test_topup_increases_balance(self, org_factory, wallet_client):
        ids = await org_factory(wallet_balance=Decimal("10"))

        tx = await wallet_client.wallet_topup(
            organization_id=ids.organization_id,
            amount=Decimal("50"),
            created_by_user_id=ids.user_id,
        )

        assert tx.type == "topup"
        assert tx.amount == Decimal("50.0000")
        assert tx.balance_after == Decimal("60.0000")

    async def test_topup_rejects_non_positive_amount(self, org_factory, wallet_client):
        ids = await org_factory()
        with pytest.raises(ValueError, match="must be positive"):
            await wallet_client.wallet_topup(
                organization_id=ids.organization_id,
                amount=Decimal("0"),
                created_by_user_id=ids.user_id,
            )

    async def test_adjust_can_go_either_direction(self, org_factory, wallet_client):
        ids = await org_factory(wallet_balance=Decimal("100"))

        tx = await wallet_client.wallet_adjust(
            organization_id=ids.organization_id,
            amount=Decimal("-30"),
            created_by_user_id=ids.user_id,
            note="correction",
        )
        assert tx.balance_after == Decimal("70.0000")


class TestDebitForCall:
    async def test_debit_computes_cost_and_decrements_balance(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        tx = await wallet_client.wallet_debit_for_call(
            organization_id=ids.organization_id,
            workflow_run_id=run_id,
            duration_seconds=90,  # 1.5 min * 2.00 = 3.00
            price_per_minute=Decimal("2.00"),
        )

        assert tx is not None
        assert tx.amount == Decimal("-3.0000")
        assert tx.balance_after == Decimal("97.0000")

    async def test_debit_is_idempotent_per_run(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        first = await wallet_client.wallet_debit_for_call(
            organization_id=ids.organization_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("2.00"),
        )
        second = await wallet_client.wallet_debit_for_call(
            organization_id=ids.organization_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("2.00"),
        )

        assert first is not None
        assert second is None  # no-op, not double-charged

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("98.0000")  # only charged once

    async def test_debit_noop_when_wallet_disabled(
        self, org_factory, db_session_factory, wallet_client
    ):
        # A rate is configured, but the master switch is off — must still no-op.
        ids = await org_factory(wallet_balance=Decimal("100"), wallet_enabled=False)
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        tx = await wallet_client.wallet_debit_for_call(
            organization_id=ids.organization_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("2.00"),
        )

        assert tx is None
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("100.0000")  # unchanged

    async def test_debit_flat_per_call_ignores_duration(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        tx = await wallet_client.wallet_debit_for_call(
            organization_id=ids.organization_id,
            workflow_run_id=run_id,
            duration_seconds=930,  # duration is irrelevant to a flat per-call rate
            billing_mode="per_call",
            price_per_call=Decimal("5.00"),
        )

        assert tx is not None
        assert tx.amount == Decimal("-5.0000")
        assert tx.balance_after == Decimal("95.0000")


class TestRecordCampaignCallCost:
    async def test_records_cost_without_touching_balance(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("50"))
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)
        campaign_id = await _create_campaign(db_session_factory, ids)

        tx = await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=120,
            price_per_minute=Decimal("1.50"),
        )

        assert tx is not None
        assert tx.type == "campaign_cost"
        assert tx.amount == Decimal("-3.0000")  # 2 min * 1.50
        assert tx.balance_after is None

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("50.0000")  # unchanged

    async def test_idempotent_per_run(self, org_factory, db_session_factory, wallet_client):
        ids = await org_factory()
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)
        campaign_id = await _create_campaign(db_session_factory, ids)

        first = await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("1.00"),
        )
        second = await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("1.00"),
        )

        assert first is not None
        assert second is None

    async def test_records_flat_per_call_cost(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("50"))
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)
        campaign_id = await _create_campaign(db_session_factory, ids)

        tx = await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=3,  # irrelevant to a flat per-call rate
            billing_mode="per_call",
            price_per_call=Decimal("4.00"),
        )

        assert tx is not None
        assert tx.amount == Decimal("-4.0000")
        assert tx.balance_after is None  # ledger-only, campaign already reserved


class TestReserveForCampaign:
    async def test_reserve_succeeds_within_balance(self, org_factory, db_session_factory, wallet_client):
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        tx = await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("40"),
        )

        assert tx.type == "campaign_reserve"
        assert tx.amount == Decimal("-40.0000")
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("60.0000")

    async def test_reserve_respects_credit_limit_buffer(
        self, org_factory, db_session_factory, wallet_client
    ):
        # Balance 10, credit_limit -20 -> available = 10 - (-20) = 30
        ids = await org_factory(wallet_balance=Decimal("10"), credit_limit=Decimal("-20"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        tx = await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("25"),
        )
        assert tx is not None
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("-15.0000")

    async def test_reserve_raises_when_insufficient(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("10"), credit_limit=Decimal("0"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        with pytest.raises(InsufficientBalanceError):
            await wallet_client.wallet_reserve_for_campaign(
                organization_id=ids.organization_id,
                campaign_id=campaign_id,
                estimated_cost=Decimal("11"),
            )

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("10.0000")  # unchanged, refused cleanly

    async def test_reserve_is_idempotent_per_campaign(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("10"),
        )
        with pytest.raises(ValueError, match="already has a reservation"):
            await wallet_client.wallet_reserve_for_campaign(
                organization_id=ids.organization_id,
                campaign_id=campaign_id,
                estimated_cost=Decimal("10"),
            )

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("90.0000")  # only reserved once


class TestCancelReservationAndReconcile:
    async def test_cancel_reservation_refunds_in_full(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("40"),
        )
        tx = await wallet_client.wallet_cancel_reservation(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )

        assert tx.amount == Decimal("40.0000")
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("100.0000")  # fully restored

    async def test_reconcile_refunds_unused_portion(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids, state="completed")
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("40"),
        )
        await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=60,
            price_per_minute=Decimal("10"),  # actual cost = 10
        )

        tx = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )

        assert tx.type == "campaign_reconcile"
        assert tx.amount == Decimal("30.0000")  # refund the unused 30
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("90.0000")  # 100 - 40 reserved + 30 refund

    async def test_reconcile_debits_overage(self, org_factory, db_session_factory, wallet_client):
        ids = await org_factory(wallet_balance=Decimal("100"), credit_limit=Decimal("-50"))
        campaign_id = await _create_campaign(db_session_factory, ids, state="completed")
        run_id = await _create_workflow_run(db_session_factory, ids.workflow_id)

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("10"),
        )
        await wallet_client.wallet_record_campaign_call_cost(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            workflow_run_id=run_id,
            duration_seconds=600,  # 10 min
            price_per_minute=Decimal("5"),  # actual cost = 50, way over the 10 reserved
        )

        tx = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )

        assert tx.amount == Decimal("-40.0000")  # overage debit: 10 reserved - 50 actual
        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("50.0000")  # 100 - 10 reserved - 40 overage

    async def test_reconcile_is_idempotent_per_campaign(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids, state="completed")

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("10"),
        )
        first = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )
        second = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )

        assert first is not None
        assert second is None  # already reconciled, no double-refund

    async def test_reconcile_returns_none_for_never_reserved_campaign(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory()
        campaign_id = await _create_campaign(db_session_factory, ids, state="completed")

        tx = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )
        assert tx is None

    async def test_cancel_and_reconcile_share_one_slot(
        self, org_factory, db_session_factory, wallet_client
    ):
        """If a reservation is cancelled, the normal end-of-campaign
        reconcile must not ALSO apply — they share one slot per campaign."""
        ids = await org_factory(wallet_balance=Decimal("100"))
        campaign_id = await _create_campaign(db_session_factory, ids)

        await wallet_client.wallet_reserve_for_campaign(
            organization_id=ids.organization_id,
            campaign_id=campaign_id,
            estimated_cost=Decimal("40"),
        )
        await wallet_client.wallet_cancel_reservation(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )

        # A later attempt to reconcile the same campaign must not double-refund.
        tx = await wallet_client.wallet_reconcile_campaign(
            organization_id=ids.organization_id, campaign_id=campaign_id
        )
        assert tx is None

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("100.0000")  # exactly restored, no double-count


class TestReservationConcurrency:
    """The actual race the whole reservation design exists to prevent:
    two campaigns launched at the same instant against a wallet that can
    only afford one must not both succeed."""

    async def test_concurrent_reservations_cannot_both_succeed_past_balance(
        self, org_factory, db_session_factory, setup_test_database
    ):
        # Balance covers exactly ONE of the two 60-unit reservations, not both.
        ids = await org_factory(wallet_balance=Decimal("60"), credit_limit=Decimal("0"))
        campaign_a = await _create_campaign(db_session_factory, ids)
        campaign_b = await _create_campaign(db_session_factory, ids)

        # Genuinely separate engines/connection pools against the SAME test
        # database — matching how two real concurrent requests would each
        # get their own DB connection from the pool.
        test_url = setup_test_database
        engine_a = create_async_engine(test_url, echo=False)
        engine_b = create_async_engine(test_url, echo=False)
        client_a = WalletClient()
        client_a.engine = engine_a
        client_a.async_session = async_sessionmaker(bind=engine_a, expire_on_commit=False)
        client_b = WalletClient()
        client_b.engine = engine_b
        client_b.async_session = async_sessionmaker(bind=engine_b, expire_on_commit=False)

        async def try_reserve(client, campaign_id):
            try:
                await client.wallet_reserve_for_campaign(
                    organization_id=ids.organization_id,
                    campaign_id=campaign_id,
                    estimated_cost=Decimal("60"),
                )
                return "reserved"
            except InsufficientBalanceError:
                return "rejected"
            finally:
                await client.engine.dispose()

        results = await asyncio.gather(
            try_reserve(client_a, campaign_a),
            try_reserve(client_b, campaign_b),
        )

        # Exactly one must have won — never both, never neither.
        assert sorted(results) == ["rejected", "reserved"]

        org = await _get_org(db_session_factory, ids.organization_id)
        assert org.wallet_balance == Decimal("0.0000")  # exactly one 60 was reserved


class TestUpdateWalletSettings:
    """Superadmin-only config: currency, credit limit, and the on/off
    switch. Never touches wallet_balance — that only moves through
    ledgered transactions. The rate itself lives per-agent, not here (see
    TestUpdateWorkflowBillingSettings)."""

    async def test_updates_only_provided_fields(
        self, org_factory, db_session_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("100"), credit_limit=Decimal("0"))

        updated = await wallet_client.update_wallet_settings(
            organization_id=ids.organization_id,
            wallet_currency="USD",
        )

        assert updated.wallet_currency == "USD"
        assert updated.credit_limit == Decimal("0.0000")  # untouched
        assert updated.wallet_balance == Decimal("100.0000")  # never touched
        assert updated.wallet_enabled is True  # untouched (test default)

    async def test_toggles_wallet_enabled_independently(
        self, org_factory, wallet_client
    ):
        # Wallet starts off (real-world default).
        ids = await org_factory(wallet_enabled=False, credit_limit=Decimal("5"))

        turned_on = await wallet_client.update_wallet_settings(
            organization_id=ids.organization_id, wallet_enabled=True
        )
        assert turned_on.wallet_enabled is True
        assert turned_on.credit_limit == Decimal("5.0000")  # untouched

        turned_off = await wallet_client.update_wallet_settings(
            organization_id=ids.organization_id, wallet_enabled=False
        )
        assert turned_off.wallet_enabled is False
        assert turned_off.credit_limit == Decimal("5.0000")  # still untouched

    async def test_updates_all_fields_together(self, org_factory, wallet_client):
        ids = await org_factory()

        updated = await wallet_client.update_wallet_settings(
            organization_id=ids.organization_id,
            wallet_currency="USD",
            credit_limit=Decimal("25"),
            wallet_enabled=False,
        )

        assert updated.wallet_currency == "USD"
        assert updated.credit_limit == Decimal("25.0000")
        assert updated.wallet_enabled is False

    async def test_returns_none_for_missing_organization(self, wallet_client):
        result = await wallet_client.update_wallet_settings(
            organization_id=999_999_999, wallet_currency="USD"
        )
        assert result is None

class TestUpdateWorkflowBillingSettings:
    async def test_sets_avg_call_duration(self, org_factory, wallet_client):
        ids = await org_factory()

        updated = await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            avg_call_duration_minutes=Decimal("3.5"),
        )

        assert updated.avg_call_duration_minutes == Decimal("3.50")

    async def test_sets_price_per_minute(self, org_factory, wallet_client):
        ids = await org_factory()

        updated = await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            price_per_minute=Decimal("2.75"),
        )

        assert updated.price_per_minute == Decimal("2.7500")

    async def test_updates_only_provided_fields(self, org_factory, wallet_client):
        ids = await org_factory(price_per_minute=Decimal("1.00"))

        updated = await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            avg_call_duration_minutes=Decimal("4"),
        )

        assert updated.avg_call_duration_minutes == Decimal("4.00")
        assert updated.price_per_minute == Decimal("1.0000")  # untouched

    async def test_returns_none_for_missing_workflow(self, wallet_client):
        result = await wallet_client.update_workflow_billing_settings(
            workflow_id=999_999_999,
            avg_call_duration_minutes=Decimal("3"),
        )
        assert result is None

    async def test_switches_to_per_call_and_sets_rate(self, org_factory, wallet_client):
        ids = await org_factory(price_per_minute=Decimal("2.00"))

        updated = await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            billing_mode="per_call",
            price_per_call=Decimal("6.00"),
        )

        assert updated.billing_mode == "per_call"
        assert updated.price_per_call == Decimal("6.0000")
        # Switching modes doesn't clear the other mode's rate field.
        assert updated.price_per_minute == Decimal("2.0000")

    async def test_switching_mode_back_leaves_per_call_rate_in_place(
        self, org_factory, wallet_client
    ):
        ids = await org_factory()
        await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            billing_mode="per_call",
            price_per_call=Decimal("6.00"),
        )

        updated = await wallet_client.update_workflow_billing_settings(
            workflow_id=ids.workflow_id,
            billing_mode="per_minute",
            price_per_minute=Decimal("3.00"),
        )

        assert updated.billing_mode == "per_minute"
        assert updated.price_per_minute == Decimal("3.0000")
        assert updated.price_per_call == Decimal("6.0000")  # left in place, just unread


class TestListWalletTransactions:
    async def test_returns_transactions_newest_first_with_total_count(
        self, org_factory, wallet_client
    ):
        ids = await org_factory(wallet_balance=Decimal("0"))

        for amount in (Decimal("10"), Decimal("20"), Decimal("30")):
            await wallet_client.wallet_topup(
                organization_id=ids.organization_id,
                amount=amount,
                created_by_user_id=ids.user_id,
            )

        transactions, total_count = await wallet_client.list_wallet_transactions(
            organization_id=ids.organization_id, limit=50, offset=0
        )

        assert total_count == 3
        assert len(transactions) == 3
        # Newest first: the last topup (30) landed last, so it's first here.
        assert [tx.amount for tx in transactions] == [
            Decimal("30.0000"),
            Decimal("20.0000"),
            Decimal("10.0000"),
        ]

    async def test_pagination_limit_and_offset(self, org_factory, wallet_client):
        ids = await org_factory(wallet_balance=Decimal("0"))

        for _ in range(5):
            await wallet_client.wallet_topup(
                organization_id=ids.organization_id,
                amount=Decimal("1"),
                created_by_user_id=ids.user_id,
            )

        page1, total_count = await wallet_client.list_wallet_transactions(
            organization_id=ids.organization_id, limit=2, offset=0
        )
        page2, _ = await wallet_client.list_wallet_transactions(
            organization_id=ids.organization_id, limit=2, offset=2
        )

        assert total_count == 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert {tx.id for tx in page1}.isdisjoint({tx.id for tx in page2})

    async def test_scoped_to_organization(self, org_factory, wallet_client):
        ids_a = await org_factory(wallet_balance=Decimal("0"))
        ids_b = await org_factory(wallet_balance=Decimal("0"))

        await wallet_client.wallet_topup(
            organization_id=ids_a.organization_id,
            amount=Decimal("5"),
            created_by_user_id=ids_a.user_id,
        )

        transactions, total_count = await wallet_client.list_wallet_transactions(
            organization_id=ids_b.organization_id, limit=50, offset=0
        )

        assert total_count == 0
        assert transactions == []
