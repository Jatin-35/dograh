"""Phone masking on every existing route that returns a customer's number.

Masking is a per-organization switch and never applies to superadmins. Each route
is checked with the switch off (numbers untouched: existing behaviour), on (masked),
and on for a superadmin (full), so a change here can neither leak a number nor
silently change what an organization that has not asked for masking sees.
"""

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CampaignModel,
    OrganizationConfigurationModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.routes.campaign import router as campaign_router
from api.routes.organization_usage import router as usage_router
from api.routes.reports import router as reports_router
from api.routes.workflow import router as workflow_router
from api.services.auth.depends import get_user
from api.services.phone_masking import set_phone_masking_for_organization

FULL = "+919876543210"
MASKED = "+91 98••••3210"
FULL_CALLED = "+911800000000"
MASKED_CALLED = "+91 18••••0000"

# A number the caller *spoke* is not a structured field: masking cannot and does not touch it.
SPOKEN_NUMBER_LOG = {
    "realtime_feedback_events": [
        {
            "type": "rtf-user-transcription",
            "payload": {"text": "my number is 9876543210", "final": True},
        }
    ]
}


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
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
class World:
    organization_id: int
    user_id: int
    workflow_id: int
    run_id: int
    campaign_id: int


async def _create_world(db_session_factory) -> World:
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
            state="running",
            rate_limit_per_second=100,
        )
        session.add(campaign)
        await session.flush()
        run = WorkflowRunModel(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow.id,
            campaign_id=campaign.id,
            mode="voicelink",
            call_type="inbound",
            is_completed=True,
            created_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
            initial_context={
                "caller_number": FULL,
                "called_number": FULL_CALLED,
                "phone_number": FULL,
                "customer_name": "Rahul",
            },
            gathered_context={
                "mapped_call_disposition": "user_hangup",
                "customer_phone_number": FULL,
            },
            usage_info={"call_duration_seconds": 42},
            cost_info={},
            annotations={},
            logs=SPOKEN_NUMBER_LOG,
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return World(org.id, user.id, workflow.id, run.id, campaign.id)


async def _cleanup(db_session_factory, world: World) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WorkflowRunModel).where(
                WorkflowRunModel.workflow_id == world.workflow_id
            )
        )
        await session.execute(
            delete(CampaignModel).where(CampaignModel.id == world.campaign_id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == world.workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == world.user_id))
        await session.execute(
            delete(OrganizationConfigurationModel).where(
                OrganizationConfigurationModel.organization_id == world.organization_id
            )
        )
        await session.execute(
            delete(OrganizationModel).where(
                OrganizationModel.id == world.organization_id
            )
        )
        await session.commit()


async def _get(
    world: World, path: str, *, superuser: bool = False, **params
) -> httpx.Response:
    app = FastAPI()
    for router in (usage_router, workflow_router, reports_router, campaign_router):
        app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=world.user_id,
        is_superuser=superuser,
        selected_organization_id=world.organization_id,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params)


def _csv_phone(response: httpx.Response) -> str:
    rows = list(csv.reader(io.StringIO(response.text)))
    return rows[1][rows[0].index("Phone Number")]


class TestUsageRunList:
    PATH = "/organizations/usage/runs"

    async def test_numbers_are_untouched_until_the_organization_turns_masking_on(
        self, db_session_factory
    ):
        world = await _create_world(db_session_factory)
        try:
            (run,) = (await _get(world, self.PATH)).json()["runs"]

            assert run["phone_number"] == FULL
            assert run["caller_number"] == FULL
            assert run["called_number"] == FULL_CALLED
            assert run["initial_context"]["caller_number"] == FULL
            assert run["gathered_context"]["customer_phone_number"] == FULL
        finally:
            await _cleanup(db_session_factory, world)

    async def test_masked_everywhere_a_number_appears_when_on(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            await set_phone_masking_for_organization(world.organization_id, True)

            (run,) = (await _get(world, self.PATH)).json()["runs"]

            assert run["phone_number"] == MASKED
            assert run["caller_number"] == MASKED
            assert run["called_number"] == MASKED_CALLED
            assert run["initial_context"]["caller_number"] == MASKED
            assert run["initial_context"]["called_number"] == MASKED_CALLED
            assert run["initial_context"]["phone_number"] == MASKED
            assert run["gathered_context"]["customer_phone_number"] == MASKED
            # Nothing else about the run changes.
            assert run["initial_context"]["customer_name"] == "Rahul"
            assert run["disposition"] == "user_hangup"
            assert run["call_duration_seconds"] == 42
            assert run["id"] == world.run_id
        finally:
            await _cleanup(db_session_factory, world)

    async def test_a_superadmin_still_sees_full_numbers(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            await set_phone_masking_for_organization(world.organization_id, True)

            (run,) = (await _get(world, self.PATH, superuser=True)).json()["runs"]

            assert run["phone_number"] == FULL
            assert run["initial_context"]["caller_number"] == FULL
        finally:
            await _cleanup(db_session_factory, world)


class TestUsageCsv:
    PATH = "/organizations/usage/runs/report"

    async def test_full_when_off_masked_when_on_full_for_a_superadmin(
        self, db_session_factory
    ):
        world = await _create_world(db_session_factory)
        try:
            assert _csv_phone(await _get(world, self.PATH)) == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            assert _csv_phone(await _get(world, self.PATH)) == MASKED
            assert _csv_phone(await _get(world, self.PATH, superuser=True)) == FULL
        finally:
            await _cleanup(db_session_factory, world)


class TestWorkflowRunDetail:
    def _path(self, world: World) -> str:
        return f"/workflow/{world.workflow_id}/runs/{world.run_id}"

    async def test_contexts_are_masked_when_on_and_full_otherwise(
        self, db_session_factory
    ):
        world = await _create_world(db_session_factory)
        try:
            before = (await _get(world, self._path(world))).json()
            assert before["initial_context"]["caller_number"] == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            after = (await _get(world, self._path(world))).json()
            assert after["initial_context"]["caller_number"] == MASKED
            assert after["initial_context"]["called_number"] == MASKED_CALLED
            assert after["gathered_context"]["customer_phone_number"] == MASKED
            assert after["initial_context"]["customer_name"] == "Rahul"

            superadmin = (await _get(world, self._path(world), superuser=True)).json()
            assert superadmin["initial_context"]["caller_number"] == FULL
        finally:
            await _cleanup(db_session_factory, world)

    async def test_what_a_caller_said_aloud_is_not_something_masking_can_reach(
        self, db_session_factory
    ):
        # Documented limit: masking covers the structured number fields, not free text
        # in the transcript or call log. This pins the behaviour so it is never mistaken
        # for full protection.
        world = await _create_world(db_session_factory)
        try:
            await set_phone_masking_for_organization(world.organization_id, True)

            body = (await _get(world, self._path(world))).json()

            assert "9876543210" in str(body["logs"])
        finally:
            await _cleanup(db_session_factory, world)


class TestWorkflowRunList:
    async def test_masked_when_on(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            path = f"/workflow/{world.workflow_id}/runs"
            assert (await _get(world, path)).json()["runs"][0]["initial_context"][
                "caller_number"
            ] == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            masked = (await _get(world, path)).json()["runs"][0]
            assert masked["initial_context"]["caller_number"] == MASKED
            assert masked["gathered_context"]["customer_phone_number"] == MASKED

            full = (await _get(world, path, superuser=True)).json()["runs"][0]
            assert full["initial_context"]["caller_number"] == FULL
        finally:
            await _cleanup(db_session_factory, world)


class TestExports:
    async def test_the_workflow_csv(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            path = f"/workflow/{world.workflow_id}/report"
            assert _csv_phone(await _get(world, path)) == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            assert _csv_phone(await _get(world, path)) == MASKED
            assert _csv_phone(await _get(world, path, superuser=True)) == FULL
        finally:
            await _cleanup(db_session_factory, world)

    async def test_the_campaign_csv(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            path = f"/campaign/{world.campaign_id}/report"
            assert _csv_phone(await _get(world, path)) == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            assert _csv_phone(await _get(world, path)) == MASKED
            assert _csv_phone(await _get(world, path, superuser=True)) == FULL
        finally:
            await _cleanup(db_session_factory, world)


class TestDailyReport:
    PATH = "/organizations/reports/daily/runs"

    async def test_masked_when_on(self, db_session_factory):
        world = await _create_world(db_session_factory)
        try:
            params = {"date": "2026-09-17", "timezone": "UTC"}
            assert (await _get(world, self.PATH, **params)).json()[0][
                "phone_number"
            ] == FULL

            await set_phone_masking_for_organization(world.organization_id, True)
            assert (await _get(world, self.PATH, **params)).json()[0][
                "phone_number"
            ] == MASKED
            assert (await _get(world, self.PATH, superuser=True, **params)).json()[0][
                "phone_number"
            ] == FULL
        finally:
            await _cleanup(db_session_factory, world)


class TestOneOrganizationsSettingNeverAffectsAnother:
    async def test_masking_is_scoped_to_the_organization_that_turned_it_on(
        self, db_session_factory
    ):
        masked_world = await _create_world(db_session_factory)
        other_world = await _create_world(db_session_factory)
        try:
            await set_phone_masking_for_organization(masked_world.organization_id, True)

            masked = (await _get(masked_world, "/organizations/usage/runs")).json()[
                "runs"
            ][0]
            other = (await _get(other_world, "/organizations/usage/runs")).json()[
                "runs"
            ][0]

            assert masked["phone_number"] == MASKED
            assert other["phone_number"] == FULL
        finally:
            await _cleanup(db_session_factory, masked_world)
            await _cleanup(db_session_factory, other_world)
