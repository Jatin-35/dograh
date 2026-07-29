"""
Tests for stopping a campaign (CampaignRunnerService.stop_campaign).

These verify:
1. stop_campaign succeeds from every non-terminal state (created, syncing,
   running, paused) and sets state='cancelled' + cancelled_at.
2. stop_campaign rejects already-terminal states (completed, failed,
   cancelled) and a missing campaign.
3. A cancelled campaign cannot be resumed or paused afterward.
4. The dispatcher's existing `state != "running"` gate already treats
   'cancelled' exactly like 'paused' — no dispatcher code change needed,
   this just proves it empirically.
"""

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CampaignModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
)
from api.services.campaign.campaign_call_dispatcher import CampaignCallDispatcher
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
class CampaignIds:
    organization_id: int
    user_id: int
    workflow_id: int
    campaign_id: int


async def _create_campaign(db_session_factory, state: str) -> CampaignIds:
    """Create a minimal org/user/workflow/campaign in the given state."""
    async with db_session_factory() as session:
        org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
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

        return CampaignIds(
            organization_id=org.id,
            user_id=user.id,
            workflow_id=workflow.id,
            campaign_id=campaign.id,
        )


async def _cleanup_campaign(db_session_factory, ids: CampaignIds) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(CampaignModel).where(CampaignModel.id == ids.campaign_id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == ids.organization_id)
        )
        await session.commit()


@pytest.fixture
async def campaign_factory(db_session_factory):
    """Yields a factory to create campaigns in a given state; cleans up
    every campaign it created, regardless of test outcome."""
    created: list[CampaignIds] = []

    async def _factory(state: str) -> int:
        ids = await _create_campaign(db_session_factory, state)
        created.append(ids)
        return ids.campaign_id

    yield _factory

    for ids in created:
        await _cleanup_campaign(db_session_factory, ids)


class TestStopCampaignAllowedStates:
    """stop_campaign() should succeed from every non-terminal state."""

    @pytest.mark.parametrize("initial_state", ["created", "syncing", "running", "paused"])
    async def test_stop_succeeds_and_sets_cancelled(
        self, db_session_factory, campaign_factory, initial_state
    ):
        campaign_id = await campaign_factory(initial_state)
        service = CampaignRunnerService()

        await service.stop_campaign(campaign_id)

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT state, cancelled_at FROM campaigns WHERE id = :id"),
                {"id": campaign_id},
            )
            row = result.one()
            assert row.state == "cancelled"
            assert row.cancelled_at is not None


class TestStopCampaignRejectedStates:
    """stop_campaign() must reject already-terminal states."""

    @pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
    async def test_stop_raises_on_terminal_state(
        self, db_session_factory, campaign_factory, terminal_state
    ):
        campaign_id = await campaign_factory(terminal_state)
        service = CampaignRunnerService()

        with pytest.raises(ValueError, match="must be in"):
            await service.stop_campaign(campaign_id)

        # State must be unchanged after a rejected stop attempt.
        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT state FROM campaigns WHERE id = :id"),
                {"id": campaign_id},
            )
            assert result.scalar_one() == terminal_state

    async def test_stop_raises_on_missing_campaign(self):
        service = CampaignRunnerService()
        with pytest.raises(ValueError, match="not found"):
            await service.stop_campaign(999_999_999)


class TestCancelledCampaignCannotBeReactivated:
    """A cancelled campaign is a real dead end — no resume, no pause."""

    async def test_resume_rejects_cancelled_campaign(self, campaign_factory):
        campaign_id = await campaign_factory("cancelled")
        service = CampaignRunnerService()

        with pytest.raises(ValueError, match="must be in 'paused' state"):
            await service.resume_campaign(campaign_id)

    async def test_pause_rejects_cancelled_campaign(self, campaign_factory):
        campaign_id = await campaign_factory("cancelled")
        service = CampaignRunnerService()

        with pytest.raises(ValueError, match="must be in 'running' or 'syncing'"):
            await service.pause_campaign(campaign_id)


class TestDispatcherSkipsCancelledCampaign:
    """The dispatcher's existing `state != "running"` gate already covers
    'cancelled' with zero code changes — this proves it empirically rather
    than trusting the reasoning alone."""

    async def test_process_batch_returns_zero_for_cancelled_campaign(
        self, campaign_factory
    ):
        campaign_id = await campaign_factory("cancelled")

        dispatcher = CampaignCallDispatcher()
        result = await dispatcher.process_batch(campaign_id=campaign_id, batch_size=5)

        assert result == 0
