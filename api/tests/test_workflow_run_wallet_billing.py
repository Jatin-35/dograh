"""
Tests for the workflow-run completion wallet hook
(api/services/workflow_run_billing.py::report_completed_workflow_run_wallet_usage),
which debits a normal call's cost from the wallet directly, or records a
campaign call's cost against its campaign's existing reservation — wired
into api/tasks/workflow_completion.py::process_workflow_completion.
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
from api.services.workflow_run_billing import (
    report_completed_workflow_run_wallet_usage,
)


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
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
    billing_mode: str = "per_minute",
    price_per_call=None,
    pulse_seconds: int = 0,
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
            price_per_minute=price_per_minute,
            billing_mode=billing_mode,
            price_per_call=price_per_call,
            pulse_seconds=pulse_seconds,
        )
        session.add(workflow)
        await session.flush()
        await session.commit()

        return OrgIds(organization_id=org.id, user_id=user.id, workflow_id=workflow.id)


async def _create_completed_run(
    db_session_factory,
    workflow_id: int,
    *,
    duration_seconds: float = 90,
    campaign_id: int | None = None,
) -> int:
    async with db_session_factory() as session:
        run = WorkflowRunModel(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            mode="voice",
            initial_context={},
            is_completed=True,
            usage_info={"call_duration_seconds": duration_seconds},
            campaign_id=campaign_id,
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return run.id


async def _create_campaign(db_session_factory, ids: OrgIds) -> int:
    async with db_session_factory() as session:
        campaign = CampaignModel(
            name=f"test-campaign-{uuid.uuid4().hex[:8]}",
            organization_id=ids.organization_id,
            workflow_id=ids.workflow_id,
            created_by=ids.user_id,
            source_type="test",
            source_id="test-source",
            state="running",
            rate_limit_per_second=100,
        )
        session.add(campaign)
        await session.flush()
        await session.commit()
        return campaign.id


async def _get_org(db_session_factory, organization_id: int) -> OrganizationModel:
    async with db_session_factory() as session:
        return await session.get(OrganizationModel, organization_id)


async def _cleanup(db_session_factory, ids: OrgIds, campaign_id: int | None = None) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WalletTransactionModel).where(
                WalletTransactionModel.organization_id == ids.organization_id
            )
        )
        await session.execute(
            delete(WorkflowRunModel).where(WorkflowRunModel.workflow_id == ids.workflow_id)
        )
        if campaign_id is not None:
            await session.execute(
                delete(CampaignModel).where(CampaignModel.id == campaign_id)
            )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == ids.organization_id)
        )
        await session.commit()


class TestNormalCallDebit:
    async def test_debits_wallet_for_completed_call(self, db_session_factory):
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            price_per_minute=Decimal("2.00"),
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=90
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("97.0000")  # 1.5 min * 2.00 = 3.00

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                tx = result.scalar_one()
                assert tx.type == WalletTransactionType.DEBIT.value
                assert tx.amount == Decimal("-3.0000")
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_rounds_up_to_the_agents_pulse(self, db_session_factory):
        # 2m 19s at 6.00/min on a 45s pulse = 4 pulses = 180s billed = 18.00,
        # where pay-as-you-go would have charged 13.90.
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            price_per_minute=Decimal("6.00"),
            pulse_seconds=45,
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=139
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("82.0000")

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                tx = result.scalar_one()
                assert tx.amount == Decimal("-18.0000")
                assert tx.transaction_metadata["pulse_seconds"] == 45
                assert tx.transaction_metadata["billed_seconds"] == "180"
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_noop_when_run_not_completed(self, db_session_factory):
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            price_per_minute=Decimal("2.00"),
        )
        try:
            async with db_session_factory() as session:
                run = WorkflowRunModel(
                    name=f"test-run-{uuid.uuid4().hex[:8]}",
                    workflow_id=ids.workflow_id,
                    mode="voice",
                    initial_context={},
                    is_completed=False,
                    usage_info={"call_duration_seconds": 90},
                )
                session.add(run)
                await session.flush()
                await session.commit()
                run_id = run.id

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("100.0000")  # unchanged
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_noop_when_billing_not_configured(self, db_session_factory):
        ids = await _create_org(db_session_factory, wallet_balance=Decimal("100"))
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=90
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("100.0000")  # unchanged
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_is_idempotent(self, db_session_factory):
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            price_per_minute=Decimal("2.00"),
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=60
            )

            await report_completed_workflow_run_wallet_usage(run_id)
            await report_completed_workflow_run_wallet_usage(run_id)  # retry

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("98.0000")  # charged only once
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_noop_when_wallet_disabled(self, db_session_factory):
        # A rate is configured, but the master switch is off.
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            price_per_minute=Decimal("2.00"),
            wallet_enabled=False,
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=90
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("100.0000")  # unchanged
        finally:
            await _cleanup(db_session_factory, ids)


class TestNormalCallDebitPerCall:
    """billing_mode=per_call charges a flat rate regardless of duration."""

    async def test_debits_flat_rate_regardless_of_duration(self, db_session_factory):
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            billing_mode="per_call",
            price_per_call=Decimal("4.00"),
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=930
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("96.0000")  # flat 4.00, not duration-based

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                tx = result.scalar_one()
                assert tx.type == WalletTransactionType.DEBIT.value
                assert tx.amount == Decimal("-4.0000")
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_noop_when_price_per_call_not_set(self, db_session_factory):
        ids = await _create_org(
            db_session_factory, wallet_balance=Decimal("100"), billing_mode="per_call"
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=90
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("100.0000")  # unchanged
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_ignores_stray_price_per_minute_when_mode_is_per_call(
        self, db_session_factory
    ):
        # A leftover price_per_minute from a prior per_minute configuration
        # must not be read while billing_mode is per_call.
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("100"),
            billing_mode="per_call",
            price_per_call=Decimal("4.00"),
            price_per_minute=Decimal("999.00"),
        )
        try:
            run_id = await _create_completed_run(
                db_session_factory, ids.workflow_id, duration_seconds=90
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("96.0000")  # still the flat 4.00
        finally:
            await _cleanup(db_session_factory, ids)


class TestCampaignCallCostRecording:
    async def test_records_cost_without_touching_balance(self, db_session_factory):
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("50"),
            price_per_minute=Decimal("1.50"),
        )
        campaign_id = await _create_campaign(db_session_factory, ids)
        try:
            run_id = await _create_completed_run(
                db_session_factory,
                ids.workflow_id,
                duration_seconds=120,
                campaign_id=campaign_id,
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            org = await _get_org(db_session_factory, ids.organization_id)
            assert org.wallet_balance == Decimal("50.0000")  # unchanged — ledger-only

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                tx = result.scalar_one()
                assert tx.type == WalletTransactionType.CAMPAIGN_COST.value
                assert tx.amount == Decimal("-3.0000")  # 2 min * 1.50
                assert tx.balance_after is None
                assert tx.campaign_id == campaign_id
        finally:
            await _cleanup(db_session_factory, ids, campaign_id)

    async def test_noop_when_billing_not_configured(self, db_session_factory):
        ids = await _create_org(db_session_factory, wallet_balance=Decimal("50"))
        campaign_id = await _create_campaign(db_session_factory, ids)
        try:
            run_id = await _create_completed_run(
                db_session_factory,
                ids.workflow_id,
                duration_seconds=120,
                campaign_id=campaign_id,
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                assert result.scalars().first() is None
        finally:
            await _cleanup(db_session_factory, ids, campaign_id)

    async def test_noop_when_wallet_disabled(self, db_session_factory):
        # A rate is configured, but the master switch is off.
        ids = await _create_org(
            db_session_factory,
            wallet_balance=Decimal("50"),
            price_per_minute=Decimal("1.50"),
            wallet_enabled=False,
        )
        campaign_id = await _create_campaign(db_session_factory, ids)
        try:
            run_id = await _create_completed_run(
                db_session_factory,
                ids.workflow_id,
                duration_seconds=120,
                campaign_id=campaign_id,
            )

            await report_completed_workflow_run_wallet_usage(run_id)

            async with db_session_factory() as session:
                result = await session.execute(
                    select(WalletTransactionModel).where(
                        WalletTransactionModel.workflow_run_id == run_id
                    )
                )
                assert result.scalars().first() is None
        finally:
            await _cleanup(db_session_factory, ids, campaign_id)
