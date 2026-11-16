"""Langfuse v4 against the real database: Telemetry settings API, project-id
backfill, and the legacy trace-link repair script."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.enums import OrganizationConfigurationKey
from api.routes.organization import router as organization_router
from api.scripts import fix_langfuse_trace_urls as repair
from api.services.auth.depends import get_user
from api.services.pipecat import tracing_config

KEY = OrganizationConfigurationKey.LANGFUSE_CREDENTIALS.value


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original_engine, original_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, session_factory
    yield session_factory
    db_client.engine, db_client.async_session = original_engine, original_session
    await engine.dispose()


async def _org_with_workflow(factory):
    async with factory() as session:
        org = OrganizationModel(provider_id=f"lf-org-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        user = UserModel(
            provider_id=f"lf-user-{uuid.uuid4().hex[:8]}",
            selected_organization_id=org.id,
        )
        session.add(user)
        await session.flush()
        workflow = WorkflowModel(
            name="lf",
            user_id=user.id,
            organization_id=org.id,
            workflow_definition={"nodes": [], "edges": []},
            template_context_variables={},
        )
        session.add(workflow)
        await session.commit()
        return org.id, workflow.id


async def _run(factory, workflow_id, gathered_context):
    async with factory() as session:
        run = WorkflowRunModel(
            name=f"lf-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow_id,
            mode="voicelink",
            call_type="outbound",
            gathered_context=gathered_context,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _context(factory, run_id):
    async with factory() as session:
        row = await session.execute(
            select(WorkflowRunModel.gathered_context).where(
                WorkflowRunModel.id == run_id
            )
        )
        return row.scalar_one()


@pytest.fixture
def routing():
    exporter = tracing_config._OrgRoutingExporter(default_exporter=None)
    with (
        patch.object(tracing_config, "_org_routing_exporter", exporter),
        patch.object(tracing_config, "_tracing_initialized", True),
    ):
        yield exporter


def _app(org_id):
    app = FastAPI()
    app.include_router(organization_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, selected_organization_id=org_id, is_superuser=False
    )
    return app


async def _call(app, method, url, **kw):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kw)


@pytest.fixture
def no_broadcast():
    manager = AsyncMock()
    with patch("api.routes.organization.get_worker_sync_manager", return_value=manager):
        yield manager


async def test_settings_save_detects_project_and_round_trips(
    db_session_factory, no_broadcast
):
    org_id, _ = await _org_with_workflow(db_session_factory)
    app = _app(org_id)
    lookup = AsyncMock(return_value="proj-77")
    with patch("api.routes.organization.fetch_langfuse_project_id", lookup):
        saved = await _call(
            app,
            "POST",
            "/organizations/langfuse-credentials",
            json={
                "host": "https://cloud.langfuse.com/project/old/traces",
                "public_key": "pk-lf-1234567890",
                "secret_key": "sk-lf-abcdefghij",
            },
        )
    assert saved.status_code == 200, saved.text
    assert saved.json()["project_id"] == "proj-77"
    lookup.assert_awaited_once_with(
        "https://cloud.langfuse.com", "pk-lf-1234567890", "sk-lf-abcdefghij"
    )
    no_broadcast.broadcast.assert_awaited_once()

    got = (await _call(app, "GET", "/organizations/langfuse-credentials")).json()
    assert got["host"] == "https://cloud.langfuse.com"
    assert got["project_id"] == "proj-77"
    assert got["traces_public"] is False
    assert got["configured"] is True
    assert got["secret_key"] != "sk-lf-abcdefghij"  # masked

    # Re-save from the form: masked keys come back unchanged, public switched on,
    # project id already filled, so no lookup.
    lookup2 = AsyncMock()
    with patch("api.routes.organization.fetch_langfuse_project_id", lookup2):
        resaved = await _call(
            app,
            "POST",
            "/organizations/langfuse-credentials",
            json={**got, "traces_public": True},
        )
    assert resaved.status_code == 200
    lookup2.assert_not_awaited()
    from api.db import db_client

    stored = (await db_client.get_configuration(org_id, KEY)).value
    assert stored["secret_key"] == "sk-lf-abcdefghij"
    assert stored["public_key"] == "pk-lf-1234567890"
    assert stored["traces_public"] is True
    assert stored["project_id"] == "proj-77"


async def test_settings_save_when_project_cannot_be_detected(
    db_session_factory, no_broadcast
):
    org_id, _ = await _org_with_workflow(db_session_factory)
    with patch(
        "api.routes.organization.fetch_langfuse_project_id",
        AsyncMock(return_value=None),
    ):
        response = await _call(
            _app(org_id),
            "POST",
            "/organizations/langfuse-credentials",
            json={"host": "https://h", "public_key": "pk", "secret_key": "sk"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == ""
    assert "could not be detected" in body["message"]


async def test_old_saved_config_gets_project_id_backfilled(db_session_factory, routing):
    from api.db import db_client

    org_id, _ = await _org_with_workflow(db_session_factory)
    await db_client.upsert_configuration(
        org_id,
        KEY,
        {"host": "https://cloud.langfuse.com", "public_key": "pk", "secret_key": "sk"},
    )
    with (
        patch.object(
            tracing_config,
            "fetch_langfuse_project_id",
            AsyncMock(return_value="proj-bf"),
        ),
        patch.object(tracing_config, "_default_project_id", "already-known"),
    ):
        await tracing_config._resolve_missing_project_ids(
            [org_id], include_default=False
        )

    stored = (await db_client.get_configuration(org_id, KEY)).value
    assert stored["project_id"] == "proj-bf"
    assert stored["secret_key"] == "sk"
    assert tracing_config.get_trace_url("ab", org_id=org_id) == (
        "https://cloud.langfuse.com/project/proj-bf/traces/ab"
    )


async def test_repair_script_rewrites_legacy_links(db_session_factory, capsys):
    from api.db import db_client

    org_own, wf_own = await _org_with_workflow(db_session_factory)
    org_default, wf_default = await _org_with_workflow(db_session_factory)
    await db_client.upsert_configuration(
        org_own,
        KEY,
        {
            "host": "https://cloud.langfuse.com",
            "public_key": "pk",
            "secret_key": "sk",
            "project_id": "p-own",
        },
    )
    tag = uuid.uuid4().hex[:6]
    legacy_own = await _run(
        db_session_factory,
        wf_own,
        {"trace_url": f"https://cloud.langfuse.com/trace/aa{tag}", "summary": "kept"},
    )
    legacy_default = await _run(
        db_session_factory,
        wf_default,
        {"trace_url": f"https://default.lf/trace/bb{tag}"},
    )
    unknown_host = await _run(
        db_session_factory,
        wf_default,
        {"trace_url": f"https://elsewhere/trace/cc{tag}"},
    )
    already_new = f"https://cloud.langfuse.com/project/p-own/traces/dd{tag}"
    modern = await _run(db_session_factory, wf_own, {"trace_url": already_new})
    no_link = await _run(db_session_factory, wf_own, {"summary": "x"})

    patches = (
        patch.object(repair, "LANGFUSE_HOST", "https://default.lf"),
        patch.object(repair, "LANGFUSE_PROJECT_ID", "p-default"),
        patch.object(repair, "fetch_langfuse_project_id", AsyncMock(return_value=None)),
    )
    for p in patches:
        p.start()
    try:
        await repair.main(apply=False)
        assert (await _context(db_session_factory, legacy_own))["trace_url"].endswith(
            f"/trace/aa{tag}"
        ), "dry run must not write"

        await repair.main(apply=True)
        own = await _context(db_session_factory, legacy_own)
        assert own["trace_url"] == (
            f"https://cloud.langfuse.com/project/p-own/traces/aa{tag}"
        )
        assert own["summary"] == "kept"  # other context untouched
        assert (await _context(db_session_factory, legacy_default))["trace_url"] == (
            f"https://default.lf/project/p-default/traces/bb{tag}"
        )
        assert (await _context(db_session_factory, unknown_host))["trace_url"] == (
            f"https://elsewhere/trace/cc{tag}"
        )
        assert (await _context(db_session_factory, modern))["trace_url"] == already_new
        assert await _context(db_session_factory, no_link) == {"summary": "x"}

        capsys.readouterr()
        await repair.main(apply=True)  # idempotent: our two are done
        out = capsys.readouterr().out
        assert f"aa{tag}" not in out and f"bb{tag}" not in out
    finally:
        for p in patches:
            p.stop()
