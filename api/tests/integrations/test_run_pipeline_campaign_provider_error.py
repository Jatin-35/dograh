"""End-to-end: a campaign call on the real pipeline whose LLM rejects the key.

The LLM raises the way a provider SDK does on HTTP 401. The run must pause
its campaign in the database (with the reason in the campaign log) and count
toward the campaign's failure rate exactly once, as a failure. A call that
ends normally counts once as a success, and a call with no campaign touches
neither.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.time import time_now_iso8601
from sqlalchemy import select, update

from api.db.models import CampaignModel, WorkflowRunModel
from api.enums import WorkflowRunMode
from api.services.campaign import circuit_breaker as cb_module
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.worker_runner import wait_for_pipeline_worker_started
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_turn_taking import _definition
from pipecat.tests import MockLLMService, MockTTSService

HARD_TIMEOUT_SECONDS = 45.0


class AuthenticationError(Exception):
    """Shaped like openai.AuthenticationError: carries status_code=401."""

    status_code = 401


class RejectingLLM(MockLLMService):
    """The provider rejects the key on the first real completion."""

    async def get_chat_completions(self, *args, **kwargs):
        raise AuthenticationError("Error code: 401 - invalid api key")


async def _campaign_for(async_session, run, user, workflow, *, state="running"):
    campaign = CampaignModel(
        name="Provider error campaign",
        organization_id=workflow.organization_id,
        workflow_id=workflow.id,
        created_by=user.id,
        source_type="test",
        source_id="s",
        state=state,
        rate_limit_per_second=10,
    )
    async_session.add(campaign)
    await async_session.flush()
    await async_session.execute(
        update(WorkflowRunModel).where(WorkflowRunModel.id == run.id).values(campaign_id=campaign.id)
    )
    await async_session.commit()
    return campaign.id


async def _campaign_state(async_session, campaign_id):
    result = await async_session.execute(
        select(CampaignModel.state, CampaignModel.logs)
        .where(CampaignModel.id == campaign_id)
        .execution_options(populate_existing=True)
    )
    state, logs = result.one()
    return state, logs or []


async def _run_call(run, user, workflow, llm, script):
    captured = []
    audio_config = create_audio_config(WorkflowRunMode.SMALLWEBRTC.value)
    transport = MockTransport(TransportParams(audio_in_enabled=True, audio_out_enabled=True))
    tts = MockTTSService(mock_audio_duration_ms=300, frame_delay=0)
    task = None
    with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
        run_task = asyncio.create_task(
            _run_pipeline(
                transport=transport,
                workflow_id=workflow.id,
                workflow_run_id=run.id,
                user_id=user.id,
                audio_config=audio_config,
                user_provider_id=user.provider_id,
            )
        )
        try:
            for _ in range(80):
                if captured or run_task.done():
                    break
                await asyncio.sleep(0.05)
            task = captured[0]
            await wait_for_pipeline_worker_started(task, timeout=3.0, run_task=run_task)
            await asyncio.sleep(1.5)  # greeting
            await script(task)
            await asyncio.wait_for(run_task, timeout=15.0)
        finally:
            if task is not None and not task.has_finished():
                await task.cancel()


async def _say(task, text):
    await task.queue_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=time_now_iso8601()))


@pytest.mark.asyncio
async def test_a_rejected_key_pauses_the_campaign_and_counts_one_failure(db_session, async_session):
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(),
        name_prefix="Campaign 401",
        provider_id_suffix="campaign-401",
    )
    run_id = run.id
    campaign_id = await _campaign_for(async_session, run, user, workflow)

    async def script(task):
        await _say(task, "Mujhe ek water tank chahiye")

    record = AsyncMock()
    with patch.object(cb_module.circuit_breaker, "record_and_evaluate", record):
        await asyncio.wait_for(
            _run_call(run, user, workflow, RejectingLLM(mock_steps=[]), script), HARD_TIMEOUT_SECONDS
        )

    state, logs = await _campaign_state(async_session, campaign_id)
    assert state == "paused"
    [entry] = [log for log in logs if log["event"] == "provider_error_circuit_breaker_tripped"]
    assert entry["details"]["status_code"] == 401
    assert entry["details"]["service"] == "llm"
    assert entry["details"]["workflow_run_id"] == run_id

    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["campaign_id"] == campaign_id
    assert kwargs["is_failure"] is True
    assert kwargs["workflow_run_id"] == run_id
    assert kwargs["reason"] == "pipeline_error"


@pytest.mark.asyncio
async def test_a_normal_campaign_call_counts_once_as_a_success(db_session, async_session):
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(),
        name_prefix="Campaign ok",
        provider_id_suffix="campaign-ok",
    )
    campaign_id = await _campaign_for(async_session, run, user, workflow)
    llm = MockLLMService(
        mock_steps=[
            MockLLMService.create_function_call_chunks(
                function_name="end_call", arguments={}, tool_call_id="call_end_1"
            )
        ],
        chunk_delay=0.001,
    )

    async def script(task):
        await _say(task, "Theek hai, call end kar dijiye")

    record = AsyncMock()
    with patch.object(cb_module.circuit_breaker, "record_and_evaluate", record):
        await asyncio.wait_for(_run_call(run, user, workflow, llm, script), HARD_TIMEOUT_SECONDS)

    state, _ = await _campaign_state(async_session, campaign_id)
    assert state == "running"
    record.assert_awaited_once()
    assert record.await_args.kwargs["is_failure"] is False


@pytest.mark.asyncio
async def test_a_rejected_key_on_a_call_without_a_campaign_touches_nothing(db_session, async_session):
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(),
        name_prefix="No campaign 401",
        provider_id_suffix="no-campaign-401",
    )

    async def script(task):
        await _say(task, "Mujhe ek water tank chahiye")

    record = AsyncMock()
    pause = AsyncMock()
    with (
        patch.object(cb_module.circuit_breaker, "record_and_evaluate", record),
        patch.object(cb_module.circuit_breaker, "pause_on_provider_error", pause),
    ):
        await asyncio.wait_for(
            _run_call(run, user, workflow, RejectingLLM(mock_steps=[]), script), HARD_TIMEOUT_SECONDS
        )
    record.assert_not_awaited()
    pause.assert_not_awaited()  # the hook isn't even registered for non-campaign calls
