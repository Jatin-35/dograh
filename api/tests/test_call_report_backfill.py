"""Tests for filling in reports for calls that finished before reports existed."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CallReportModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.call_report import backfill as backfill_module
from api.services.call_report.backfill import backfill_call_reports
from api.services.call_report.service import build_and_store_call_report


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
class Ids:
    organization_id: int
    user_id: int
    workflow_id: int


async def _create_org(db_session_factory) -> Ids:
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
        await session.commit()
        return Ids(org.id, user.id, workflow.id)


async def _add_run(
    db_session_factory, ids: Ids, *, completed: bool = True, created_at=None
) -> int:
    async with db_session_factory() as session:
        run = WorkflowRunModel(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=ids.workflow_id,
            mode="voicelink",
            call_type="inbound",
            is_completed=completed,
            created_at=created_at or datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
            initial_context={"caller_number": "+911234567890"},
            gathered_context={"mapped_call_disposition": "user_hangup"},
            usage_info={"call_duration_seconds": 60},
            annotations={},
            logs={},
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return run.id


async def _report_run_ids(db_session_factory, ids: Ids) -> set[int]:
    async with db_session_factory() as session:
        result = await session.execute(
            select(CallReportModel.workflow_run_id).where(
                CallReportModel.organization_id == ids.organization_id
            )
        )
        return set(result.scalars().all())


async def _cleanup(db_session_factory, *all_ids: Ids) -> None:
    async with db_session_factory() as session:
        for ids in all_ids:
            await session.execute(
                delete(WorkflowRunModel).where(
                    WorkflowRunModel.workflow_id == ids.workflow_id
                )
            )
            await session.execute(
                delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id)
            )
            await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
            await session.execute(
                delete(OrganizationModel).where(
                    OrganizationModel.id == ids.organization_id
                )
            )
        await session.commit()


class TestBackfill:
    async def test_a_dry_run_counts_but_writes_nothing(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            for _ in range(3):
                await _add_run(db_session_factory, ids)

            summary = await backfill_call_reports(
                apply=False, organization_id=ids.organization_id
            )

            assert (summary.scanned, summary.built) == (3, 0)
            assert await _report_run_ids(db_session_factory, ids) == set()
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_apply_builds_a_report_for_every_completed_run(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            runs = [await _add_run(db_session_factory, ids) for _ in range(3)]

            summary = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id
            )

            assert (summary.scanned, summary.built, summary.failed) == (3, 3, 0)
            assert await _report_run_ids(db_session_factory, ids) == set(runs)
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_runs_still_in_progress_are_left_alone(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            done = await _add_run(db_session_factory, ids)
            await _add_run(db_session_factory, ids, completed=False)

            summary = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id
            )

            assert summary.built == 1
            assert await _report_run_ids(db_session_factory, ids) == {done}
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_it_can_be_repeated_and_only_does_the_missing_work(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            first = await _add_run(db_session_factory, ids)
            await build_and_store_call_report(first)  # already has one
            second = await _add_run(db_session_factory, ids)

            once = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id
            )
            again = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id
            )

            assert (once.scanned, once.built) == (1, 1)
            assert (again.scanned, again.built) == (0, 0)
            assert await _report_run_ids(db_session_factory, ids) == {first, second}
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_rebuild_existing_redoes_reports_that_already_exist(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run = await _add_run(db_session_factory, ids)
            await build_and_store_call_report(run)

            summary = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id, rebuild_existing=True
            )

            assert (summary.scanned, summary.built) == (1, 1)
            assert len(await _report_run_ids(db_session_factory, ids)) == 1
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_it_can_be_limited_to_one_organization_or_agent(
        self, db_session_factory
    ):
        mine = await _create_org(db_session_factory)
        theirs = await _create_org(db_session_factory)
        try:
            my_run = await _add_run(db_session_factory, mine)
            await _add_run(db_session_factory, theirs)

            await backfill_call_reports(
                apply=True, organization_id=mine.organization_id
            )

            assert await _report_run_ids(db_session_factory, mine) == {my_run}
            assert await _report_run_ids(db_session_factory, theirs) == set()

            summary = await backfill_call_reports(
                apply=True, workflow_id=theirs.workflow_id
            )
            assert summary.built == 1
            assert len(await _report_run_ids(db_session_factory, theirs)) == 1
        finally:
            await _cleanup(db_session_factory, mine, theirs)

    async def test_since_skips_older_runs(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            await _add_run(
                db_session_factory,
                ids,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
            recent = await _add_run(
                db_session_factory,
                ids,
                created_at=datetime(2026, 9, 10, tzinfo=UTC),
            )

            summary = await backfill_call_reports(
                apply=True,
                organization_id=ids.organization_id,
                since=datetime(2026, 9, 1, tzinfo=UTC),
            )

            assert summary.built == 1
            assert await _report_run_ids(db_session_factory, ids) == {recent}
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_it_works_through_batches_and_honours_a_limit(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            for _ in range(7):
                await _add_run(db_session_factory, ids)

            seen_batches: list[int] = []
            limited = await backfill_call_reports(
                apply=True,
                organization_id=ids.organization_id,
                batch_size=2,
                limit=5,
                on_batch=lambda s: seen_batches.append(s.scanned),
            )
            assert (limited.scanned, limited.built) == (5, 5)
            assert seen_batches == [2, 4, 5]

            rest = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id, batch_size=2
            )
            assert (rest.scanned, rest.built) == (2, 2)
            assert len(await _report_run_ids(db_session_factory, ids)) == 7
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_one_run_that_fails_does_not_stop_the_others(
        self, db_session_factory, monkeypatch
    ):
        ids = await _create_org(db_session_factory)
        try:
            runs = [await _add_run(db_session_factory, ids) for _ in range(3)]
            bad = runs[1]

            async def flaky(run_id: int):
                if run_id == bad:
                    raise RuntimeError("cannot build this one")
                return await build_and_store_call_report(run_id)

            monkeypatch.setattr(backfill_module, "build_and_store_call_report", flaky)

            summary = await backfill_call_reports(
                apply=True, organization_id=ids.organization_id
            )

            assert (summary.scanned, summary.built, summary.failed) == (3, 2, 1)
            assert summary.failed_run_ids == [bad]
            assert await _report_run_ids(db_session_factory, ids) == {
                runs[0],
                runs[2],
            }
        finally:
            await _cleanup(db_session_factory, ids)
