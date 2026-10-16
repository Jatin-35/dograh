"""Both new per-agent settings, saved through the real API and read by calls.

1. Save the start-node switch (allow_interrupt_greeting) and the workflow
   setting (noise_reduction) with PUT /workflow/{id}, read them back.
2. Bad noise_reduction values are rejected (422), nothing is stored.
3. A draft test call sees the new settings; a published call keeps the old
   ones until the draft is published, then it sees them too.
4. The WebRTC call path attaches RNNoise to the transport only when the
   run's version has it on.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams

from api.enums import WorkflowRunMode
from api.routes.workflow import router as workflow_router
from api.services.pipecat.native_rnnoise_filter import NativeRNNoiseFilter
from api.services.auth.depends import get_user
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.workflow_graph import WorkflowGraph
from api.tests.integrations._run_pipeline_helpers import create_workflow_run_rows

from .test_run_pipeline_greeting_interrupt import _definition


def _app(user):
    app = FastAPI()
    app.include_router(workflow_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=user.id,
        is_superuser=False,
        selected_organization_id=user.selected_organization_id,
        provider_id=user.provider_id,
    )
    return app


async def _call(user, method, path, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(user))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


def _start_flag(definition_json) -> bool:
    graph = WorkflowGraph(ReactFlowDTO.model_validate(definition_json))
    return graph.nodes[graph.start_node_id].allow_interrupt_greeting


async def _run(db_session, workflow, user, *, use_draft):
    run = await db_session.create_workflow_run(
        name="settings check",
        workflow_id=workflow.id,
        mode=WorkflowRunMode.SMALLWEBRTC.value,
        user_id=user.id,
        use_draft=use_draft,
        organization_id=workflow.organization_id,
    )
    # Re-read the way the call path does, with the bound version loaded.
    return await db_session.get_workflow_run(
        run.id, organization_id=workflow.organization_id
    )


async def _transport_filter_for_run(workflow, run, user):
    """Run the real WebRTC entry point up to the pipeline, return the filter
    attached to the transport it built."""
    from api.services.pipecat import run_pipeline as rp

    built = {}

    async def fake_create_webrtc_transport(*args, **kwargs):
        built["t"] = MockTransport(
            TransportParams(audio_in_enabled=True, audio_out_enabled=True)
        )
        return built["t"]

    with (
        patch.object(rp, "create_webrtc_transport", fake_create_webrtc_transport),
        patch.object(rp, "_run_pipeline_impl", AsyncMock()),
    ):
        await rp._run_pipeline_smallwebrtc_impl(
            webrtc_connection=None,
            workflow_id=workflow.id,
            workflow_run_id=run.id,
            user_id=user.id,
            organization_id=workflow.organization_id,
        )
    return built["t"]._params.audio_in_filter


@pytest.mark.asyncio
async def test_settings_save_reach_draft_then_published_calls(db_session, async_session):
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix="Settings API",
        provider_id_suffix="settings-api",
    )

    # Published version: both off.
    published = await _run(db_session, workflow, user, use_draft=False)
    assert _start_flag(published.definition.workflow_json) is False
    assert (published.definition.workflow_configurations or {}).get(
        "noise_reduction"
    ) in (None, "off")
    assert await _transport_filter_for_run(workflow, published, user) is None

    # Save both settings via the API (a new draft).
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_definition": _definition(True),
            "workflow_configurations": {"noise_reduction": "rnnoise"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow_configurations"]["noise_reduction"] == "rnnoise"
    assert _start_flag(body["workflow_definition"]) is True

    # Reading it back returns the saved draft.
    got = await _call(user, "GET", f"/workflow/fetch/{workflow.id}")
    assert got.status_code == 200, got.text
    assert got.json()["workflow_configurations"]["noise_reduction"] == "rnnoise"
    assert _start_flag(got.json()["workflow_definition"]) is True

    # A draft test call sees both; a live (published) call does not yet.
    draft_run = await _run(db_session, workflow, user, use_draft=True)
    assert _start_flag(draft_run.definition.workflow_json) is True
    assert draft_run.definition.workflow_configurations["noise_reduction"] == "rnnoise"
    assert isinstance(
        await _transport_filter_for_run(workflow, draft_run, user),
        (NativeRNNoiseFilter, RNNoiseFilter),
    )

    live_run = await _run(db_session, workflow, user, use_draft=False)
    assert _start_flag(live_run.definition.workflow_json) is False
    assert await _transport_filter_for_run(workflow, live_run, user) is None

    # Publish: live calls now get both.
    pub = await _call(user, "POST", f"/workflow/{workflow.id}/publish")
    assert pub.status_code == 200, pub.text
    live_after = await _run(db_session, workflow, user, use_draft=False)
    assert _start_flag(live_after.definition.workflow_json) is True
    assert isinstance(
        await _transport_filter_for_run(workflow, live_after, user),
        (NativeRNNoiseFilter, RNNoiseFilter),
    )

    # Turning it back off and publishing removes the filter again.
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_definition": _definition(False),
            "workflow_configurations": {"noise_reduction": "off"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert (await _call(user, "POST", f"/workflow/{workflow.id}/publish")).status_code == 200
    final = await _run(db_session, workflow, user, use_draft=False)
    assert _start_flag(final.definition.workflow_json) is False
    assert await _transport_filter_for_run(workflow, final, user) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["krisp", "RNNoise", "on", 1, True])
async def test_bad_noise_reduction_value_is_rejected(db_session, async_session, bad):
    _, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix="Settings API bad",
        provider_id_suffix=f"settings-api-bad-{bad}",
    )
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={"workflow_configurations": {"noise_reduction": bad}},
    )
    assert resp.status_code == 422, resp.text
    got = await _call(user, "GET", f"/workflow/fetch/{workflow.id}")
    assert (got.json().get("workflow_configurations") or {}).get(
        "noise_reduction"
    ) in (None, "off")
