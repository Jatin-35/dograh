"""Tests for storing call reports: the table, the upsert, org scoping, and the
post-call hook that builds a report for every run (api/tasks/run_integrations.py).
"""

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CallReportModel,
    OrganizationConfigurationModel,
    OrganizationModel,
    ToolModel,
    UserModel,
    WorkflowDefinitionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.routes.call_report import router as call_report_router
from api.services.auth.depends import get_user
from api.services.call_report.service import build_and_store_call_report
from api.services.phone_masking import set_phone_masking_for_organization
from api.tasks import run_integrations as run_integrations_module

CREATE_RESULT = (
    "{'status': 'success', 'status_code': 200, 'data': {'status': 'success', "
    "'http_status': 201, 'data': {'d': {'ExObjectId': '7000002403'}}}}"
)
QA_JSON = (
    '{"tags": [], "overall_sentiment": "positive", "call_quality_score": 9, '
    '"summary": "Ticket raised.", "issue_type": "recharge", "issue_resolved": true, '
    '"human_transfer": false}'
)


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


async def _create_run(db_session_factory, workflow_id: int, **overrides) -> int:
    fields = dict(
        name=f"test-run-{uuid.uuid4().hex[:8]}",
        workflow_id=workflow_id,
        mode="voicelink",
        call_type="inbound",
        is_completed=True,
        initial_context={"caller_number": "+911234567890"},
        gathered_context={"mapped_call_disposition": "user_hangup"},
        usage_info={"call_duration_seconds": 120},
        annotations={},
        logs={
            "realtime_feedback_events": [
                {
                    "type": "rtf-function-call-end",
                    "timestamp": "2026-09-17T09:37:52.783+00:00",
                    "payload": {
                        "function_name": "create_complaint2",
                        "tool_call_id": "c1",
                        "result": CREATE_RESULT,
                    },
                }
            ]
        },
    )
    fields.update(overrides)
    async with db_session_factory() as session:
        run = WorkflowRunModel(**fields)
        session.add(run)
        await session.flush()
        await session.commit()
        return run.id


async def _cleanup(db_session_factory, ids: Ids) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WorkflowRunModel).where(
                WorkflowRunModel.workflow_id == ids.workflow_id
            )
        )
        await session.execute(
            delete(WorkflowDefinitionModel).where(
                WorkflowDefinitionModel.workflow_id == ids.workflow_id
            )
        )
        await session.execute(
            delete(ToolModel).where(ToolModel.organization_id == ids.organization_id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
        await session.execute(
            delete(OrganizationConfigurationModel).where(
                OrganizationConfigurationModel.organization_id == ids.organization_id
            )
        )
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == ids.organization_id)
        )
        await session.commit()


async def _rows_for(db_session_factory, run_id: int) -> list[CallReportModel]:
    async with db_session_factory() as session:
        result = await session.execute(
            select(CallReportModel).where(CallReportModel.workflow_run_id == run_id)
        )
        return list(result.scalars())


class TestStoreCallReport:
    async def test_stores_typed_columns_and_full_report(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                annotations={
                    "qa_5": {"node_results": {"1": {"raw_response": QA_JSON}}}
                },
            )

            await build_and_store_call_report(run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.organization_id == ids.organization_id
            assert row.workflow_id == ids.workflow_id
            assert row.call_type == "inbound"
            assert row.is_telephony is True
            assert row.duration_seconds == 120
            assert row.disconnect_category == "customer_hung_up"
            assert row.ticket_created is True and row.ticket_closed is False
            assert row.ticket_count == 1
            assert row.outcome == "ticket_open"
            assert row.is_successful is True
            assert row.sentiment == "positive"
            assert row.reason_for_call == "recharge"
            assert row.qa_status == "analysed"
            assert row.report["ticket"]["tickets"][0]["ticket_id"] == "7000002403"
            assert row.report["call"]["phone_number"] == "+911234567890"
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_rebuilding_replaces_the_row_instead_of_duplicating(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)
            assert (await _rows_for(db_session_factory, run_id))[
                0
            ].qa_status == "not_run"

            # QA finishes late: the run gains annotations, the report is rebuilt.
            async with db_session_factory() as session:
                run = await session.get(WorkflowRunModel, run_id)
                run.annotations = {
                    "qa_5": {"node_results": {"1": {"raw_response": QA_JSON}}}
                }
                await session.commit()
            await build_and_store_call_report(run_id)

            rows = await _rows_for(db_session_factory, run_id)
            assert len(rows) == 1
            assert rows[0].qa_status == "analysed"
            assert rows[0].sentiment == "positive"
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_reads_are_scoped_to_the_callers_organization(
        self, db_session_factory
    ):
        from api.db import db_client

        ids = await _create_org(db_session_factory)
        other = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)

            mine = await db_client.get_call_report(run_id, ids.organization_id)
            theirs = await db_client.get_call_report(run_id, other.organization_id)

            assert mine is not None and mine.run_id == run_id
            assert theirs is None
        finally:
            await _cleanup(db_session_factory, ids)
            await _cleanup(db_session_factory, other)

    async def test_browser_test_call_is_flagged_non_telephony(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                mode="smallwebrtc",
                call_type="outbound",
            )

            await build_and_store_call_report(run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.is_telephony is False
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_unknown_run_stores_nothing(self, db_session_factory):
        assert await build_and_store_call_report(2_000_000_000) is None


class TestPostCallHook:
    async def test_report_is_built_even_when_there_is_nothing_to_integrate(
        self, db_session_factory, monkeypatch
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)

            async def _no_integrations(_run_id):
                return None

            monkeypatch.setattr(
                run_integrations_module, "_run_integrations_for_run", _no_integrations
            )

            await run_integrations_module.run_integrations_post_workflow_run(
                None, run_id
            )

            assert len(await _rows_for(db_session_factory, run_id)) == 1
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_report_is_built_and_error_still_propagates_when_integrations_fail(
        self, db_session_factory, monkeypatch
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)

            async def _boom(_run_id):
                raise RuntimeError("integration exploded")

            monkeypatch.setattr(
                run_integrations_module, "_run_integrations_for_run", _boom
            )

            with pytest.raises(RuntimeError, match="integration exploded"):
                await run_integrations_module.run_integrations_post_workflow_run(
                    None, run_id
                )

            assert len(await _rows_for(db_session_factory, run_id)) == 1
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_a_failing_report_never_breaks_the_hook(
        self, db_session_factory, monkeypatch
    ):
        async def _no_integrations(_run_id):
            return None

        async def _broken_report(_run_id):
            raise RuntimeError("report exploded")

        monkeypatch.setattr(
            run_integrations_module, "_run_integrations_for_run", _no_integrations
        )
        monkeypatch.setattr(
            "api.services.call_report.service.build_and_store_call_report",
            _broken_report,
        )

        # Must not raise.
        await run_integrations_module.run_integrations_post_workflow_run(None, 1)


async def test_table_has_the_dashboard_indexes(db_session_factory):
    async with db_session_factory() as session:
        result = await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'call_reports'")
        )
        names = {row[0] for row in result}

    assert {
        "ix_call_reports_org_started",
        "ix_call_reports_org_workflow_started",
        "uq_call_reports_workflow_run",
    } <= names


def _app_for(organization_id: int, *, superuser: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(call_report_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, is_superuser=superuser, selected_organization_id=organization_id
    )
    return app


async def _get_report(app: FastAPI, workflow_id: int, run_id: int) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/workflow/{workflow_id}/runs/{run_id}/call-report")


class TestCallReportRoute:
    async def test_serves_the_stored_report(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)

            response = await _get_report(
                _app_for(ids.organization_id), ids.workflow_id, run_id
            )

            assert response.status_code == 200
            body = response.json()
            assert body["run_id"] == run_id
            assert body["ticket"]["tickets"][0]["ticket_id"] == "7000002403"
            assert body["outcome"]["code"] == "ticket_open"
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_builds_on_the_fly_for_a_run_with_no_stored_report_and_writes_nothing(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)

            response = await _get_report(
                _app_for(ids.organization_id), ids.workflow_id, run_id
            )

            assert response.status_code == 200
            assert response.json()["call"]["phone_number"] == "+911234567890"
            assert await _rows_for(db_session_factory, run_id) == []
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_another_organization_cannot_read_the_report(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        other = await _create_org(db_session_factory)
        try:
            stored_run = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(stored_run)
            unstored_run = await _create_run(db_session_factory, ids.workflow_id)
            intruder = _app_for(other.organization_id)

            assert (
                await _get_report(intruder, ids.workflow_id, stored_run)
            ).status_code == 404
            assert (
                await _get_report(intruder, ids.workflow_id, unstored_run)
            ).status_code == 404
        finally:
            await _cleanup(db_session_factory, ids)
            await _cleanup(db_session_factory, other)

    async def test_run_under_the_wrong_workflow_is_not_found(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)

            response = await _get_report(
                _app_for(ids.organization_id), ids.workflow_id + 1_000_000, run_id
            )

            assert response.status_code == 404
        finally:
            await _cleanup(db_session_factory, ids)


class TestCallReportRoutePhoneMasking:
    async def test_number_is_full_until_the_organization_turns_masking_on(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)
            app = _app_for(ids.organization_id)

            before = (await _get_report(app, ids.workflow_id, run_id)).json()
            assert before["call"]["phone_number"] == "+911234567890"
            assert before["phone_masked"] is False

            await set_phone_masking_for_organization(ids.organization_id, True)

            after = (await _get_report(app, ids.workflow_id, run_id)).json()
            assert after["call"]["phone_number"] == "+91 12••••7890"
            assert after["phone_masked"] is True
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_masking_also_applies_to_a_report_built_on_the_fly(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await set_phone_masking_for_organization(ids.organization_id, True)

            body = (
                await _get_report(
                    _app_for(ids.organization_id), ids.workflow_id, run_id
                )
            ).json()

            assert body["call"]["phone_number"] == "+91 12••••7890"
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_a_superadmin_still_sees_the_full_number(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)
            await set_phone_masking_for_organization(ids.organization_id, True)

            body = (
                await _get_report(
                    _app_for(ids.organization_id, superuser=True),
                    ids.workflow_id,
                    run_id,
                )
            ).json()

            assert body["call"]["phone_number"] == "+911234567890"
            assert body["phone_masked"] is False
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_the_stored_report_is_never_altered_by_masking(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(db_session_factory, ids.workflow_id)
            await build_and_store_call_report(run_id)
            await set_phone_masking_for_organization(ids.organization_id, True)

            await _get_report(_app_for(ids.organization_id), ids.workflow_id, run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.report["call"]["phone_number"] == "+911234567890"
        finally:
            await _cleanup(db_session_factory, ids)


async def _add_tool(session, ids: Ids, name: str) -> str:
    tool = ToolModel(
        organization_id=ids.organization_id,
        created_by=ids.user_id,
        name=name,
        category="http_api",
        definition={},
        status="active",
    )
    session.add(tool)
    await session.flush()
    return tool.tool_uuid


async def _add_definition(session, ids: Ids, nodes: list[dict]) -> int:
    definition = WorkflowDefinitionModel(
        workflow_id=ids.workflow_id,
        workflow_json={"nodes": nodes, "edges": []},
        is_current=True,
        status="published",
    )
    session.add(definition)
    await session.flush()
    return definition.id


class TestCapabilitiesFromTheAgentsDefinition:
    async def test_a_ticket_tool_and_a_qa_node_make_those_sections_apply(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            async with db_session_factory() as session:
                ticket_tool = await _add_tool(session, ids, "Create Complaint2")
                other_tool = await _add_tool(session, ids, "query_taluka_tg")
                definition_id = await _add_definition(
                    session,
                    ids,
                    [
                        {
                            "id": "1",
                            "type": "startCall",
                            "data": {"tool_uuids": [ticket_tool, other_tool]},
                        },
                        {"id": "2", "type": "qa", "data": {"qa_enabled": True}},
                    ],
                )
                await session.commit()
            # A call that has produced neither a ticket nor an analysis (yet).
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                definition_id=definition_id,
                logs={},
                annotations={},
            )

            await build_and_store_call_report(run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.ticket_capable is True
            assert row.analysis_capable is True
            assert row.report["capabilities"] == {"ticket": True, "analysis": True}
            assert row.ticket_created is False
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_an_agent_without_them_gets_neither_section(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            async with db_session_factory() as session:
                tool = await _add_tool(session, ids, "send_template_message")
                definition_id = await _add_definition(
                    session,
                    ids,
                    [
                        {
                            "id": "1",
                            "type": "startCall",
                            "data": {"tool_uuids": [tool]},
                        },
                        {"id": "2", "type": "qa", "data": {"qa_enabled": False}},
                    ],
                )
                await session.commit()
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                definition_id=definition_id,
                logs={},
                annotations={},
                call_type="outbound",
                mode="smallwebrtc",
                initial_context={},
            )

            await build_and_store_call_report(run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.ticket_capable is False
            assert row.analysis_capable is False
            assert row.report["call"]["is_telephony"] is False
            assert row.report["call"]["phone_number"] is None
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_extracted_variables_are_stored_as_captured_data(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                logs={},
                gathered_context={
                    "mapped_call_disposition": "user_qualified",
                    "extracted_variables": {
                        "interested_item": "water tank",
                        "conversation_language": "hi",
                        "budget": None,
                    },
                },
            )

            await build_and_store_call_report(run_id)

            (row,) = await _rows_for(db_session_factory, run_id)
            assert row.report["captured"] == {
                "interested_item": "water tank",
                "conversation_language": "hi",
            }
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_unreadable_tools_do_not_stop_the_report(
        self, db_session_factory, monkeypatch
    ):
        from api.db import db_client

        ids = await _create_org(db_session_factory)
        try:
            async with db_session_factory() as session:
                tool = await _add_tool(session, ids, "create_complaint2")
                definition_id = await _add_definition(
                    session,
                    ids,
                    [{"id": "1", "type": "startCall", "data": {"tool_uuids": [tool]}}],
                )
                await session.commit()
            run_id = await _create_run(
                db_session_factory,
                ids.workflow_id,
                definition_id=definition_id,
                logs={},
            )

            async def _boom(*_args, **_kwargs):
                raise RuntimeError("tool lookup failed")

            monkeypatch.setattr(db_client, "get_tools_by_uuids", _boom)

            report = await build_and_store_call_report(run_id)

            # Falls back to what the call itself shows: no ticket, so not ticket-capable.
            assert report is not None
            assert report.capabilities.ticket is False
            assert len(await _rows_for(db_session_factory, run_id)) == 1
        finally:
            await _cleanup(db_session_factory, ids)
