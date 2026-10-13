"""Full-pipeline tests for the start node's allow_interrupt_greeting switch.

Drives the real ``_run_pipeline`` against the test database with a long
(4 s) text greeting and a caller who speaks halfway through it.

Switch OFF (today's behaviour): the caller is muted for the whole greeting;
what they say mid-greeting is dropped, the LLM never runs, and the greeting
plays to the end.

Switch ON: the caller is heard mid-greeting; their turn interrupts the
greeting (the bot stops speaking early) and drives the LLM, which ends the
call well before the greeting would have finished.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    UserMuteStartedFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.time import time_now_iso8601

from api.enums import WorkflowRunMode, WorkflowRunState
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.worker_runner import wait_for_pipeline_worker_started
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_text_greeting import (
    _find_processor_by_class_name,
    _wait_for,
)
from pipecat.tests import MockLLMService, MockTTSService

GREETING_TEXT = "Welcome to Think Gas! This is Pragati speaking. Which language?"
GREETING_AUDIO_MS = 4000
HARD_TIMEOUT_SECONDS = 30.0


def _definition(allow_interrupt_greeting: bool) -> dict:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "startCall",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "Start",
                    "prompt": "You are Pragati. End the call when asked.",
                    "is_start": True,
                    # Normal interruptions stay OFF: only the greeting switch
                    # decides whether the caller can barge in on the greeting.
                    "allow_interrupt": False,
                    "allow_interrupt_greeting": allow_interrupt_greeting,
                    "add_global_prompt": False,
                    "greeting": GREETING_TEXT,
                    "greeting_type": "text",
                },
            },
            {
                "id": "end",
                "type": "endCall",
                "position": {"x": 0, "y": 200},
                "data": {
                    "name": "End",
                    "prompt": "End the call politely.",
                    "is_end": True,
                    "allow_interrupt": False,
                    "add_global_prompt": False,
                },
            },
        ],
        "edges": [
            {
                "id": "start-end",
                "source": "start",
                "target": "end",
                "data": {"label": "End Call", "condition": "When the user wants to end."},
            }
        ],
    }


class _BotSpeechRecorder(BaseObserver):
    """Records loop-time of bot start/stop speaking and user-mute starts."""

    def __init__(self):
        super().__init__()
        self.bot_started: list[float] = []
        self.bot_stopped: list[float] = []
        self.mute_started = 0
        self._seen: set[int] = set()

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # frame.id is unique per frame; Python's id() is reused after GC.
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        now = asyncio.get_event_loop().time()
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_started.append(now)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_stopped.append(now)
        elif isinstance(frame, UserMuteStartedFrame):
            self.mute_started += 1


async def _setup(db_session, async_session, allow_interrupt_greeting: bool, suffix):
    return await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(allow_interrupt_greeting),
        name_prefix=f"Greeting Interrupt {suffix}",
        provider_id_suffix=f"greeting-interrupt-{suffix}",
    )


class _ObservedTasks(list):
    """Capture list that attaches the recorder the moment the pipeline task
    is created, before it starts, so no early frame is missed."""

    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def append(self, task):
        task.add_observer(self.recorder)
        super().append(task)


async def _start(workflow_run_setup, llm, tts, captured_task):
    workflow_run, user, workflow = workflow_run_setup
    transport = MockTransport(
        TransportParams(audio_in_enabled=True, audio_out_enabled=True)
    )
    run_task = asyncio.create_task(
        _run_pipeline(
            transport=transport,
            workflow_id=workflow.id,
            workflow_run_id=workflow_run.id,
            user_id=user.id,
            audio_config=create_audio_config(WorkflowRunMode.SMALLWEBRTC.value),
            user_provider_id=user.provider_id,
        )
    )
    for _ in range(60):
        if captured_task or run_task.done():
            break
        await asyncio.sleep(0.05)
    if run_task.done() and not captured_task:
        run_task.result()
    assert captured_task, "create_pipeline_task was never invoked"
    pipeline_task = captured_task[0]
    await wait_for_pipeline_worker_started(pipeline_task, timeout=3.0, run_task=run_task)
    return run_task, pipeline_task, captured_task.recorder


async def _say(pipeline_task, text):
    await pipeline_task.queue_frame(
        TranscriptionFrame(text=text, user_id="caller", timestamp=time_now_iso8601())
    )


async def _cleanup(pipeline_task):
    if pipeline_task is not None and not pipeline_task.has_finished():
        try:
            await asyncio.wait_for(pipeline_task.cancel(), timeout=3.0)
        except Exception:
            pass


def _llm_ending_call():
    return MockLLMService(
        mock_steps=[
            MockLLMService.create_function_call_chunks(
                function_name="end_call", arguments={}, tool_call_id="call_end_1"
            )
        ],
        chunk_delay=0.001,
    )


async def _body_switch_on(setup, db_session):
    llm = _llm_ending_call()
    tts = MockTTSService(mock_audio_duration_ms=GREETING_AUDIO_MS, frame_delay=0)
    captured = _ObservedTasks(_BotSpeechRecorder())
    pipeline_task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            run_task, pipeline_task, rec = await _start(setup, llm, tts, captured)
            user_agg = _find_processor_by_class_name(pipeline_task, "LLMUserAggregator")
            assert user_agg is not None

            # Caller was never muted at call start.
            assert await _wait_for(lambda: rec.bot_started, timeout=5.0)
            started = rec.bot_started[0]
            assert user_agg._user_is_muted is False
            assert rec.mute_started == 0

            # Halfway through the 4 s greeting the caller speaks.
            await asyncio.sleep(1.0)
            assert not rec.bot_stopped, "greeting ended too early, test is invalid"
            await _say(pipeline_task, "Hindi please, and actually end the call.")

            await asyncio.wait_for(run_task, timeout=10.0)

        # The caller's words drove the LLM ...
        assert llm.get_current_step() >= 1
        # ... and cut the greeting short: the bot stopped speaking well before
        # its 4 s of audio would have finished.
        assert rec.bot_stopped, "bot never stopped speaking"
        assert rec.bot_stopped[0] - started < GREETING_AUDIO_MS / 1000 - 1.0, (
            f"greeting was not interrupted: spoke {rec.bot_stopped[0] - started:.2f}s"
        )
        refreshed = await db_session.get_workflow_run_by_id(setup[0].id)
        assert refreshed.state == WorkflowRunState.COMPLETED.value
        assert "End" in refreshed.gathered_context.get("nodes_visited", [])
    finally:
        await _cleanup(pipeline_task)


async def _body_switch_off(setup, db_session):
    llm = _llm_ending_call()
    tts = MockTTSService(mock_audio_duration_ms=GREETING_AUDIO_MS, frame_delay=0)
    captured = _ObservedTasks(_BotSpeechRecorder())
    pipeline_task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            run_task, pipeline_task, rec = await _start(setup, llm, tts, captured)
            user_agg = _find_processor_by_class_name(pipeline_task, "LLMUserAggregator")

            assert await _wait_for(lambda: rec.bot_started, timeout=5.0)
            started = rec.bot_started[0]
            await asyncio.sleep(1.0)
            # Muted for the greeting, as today.
            assert user_agg._user_is_muted is True
            await _say(pipeline_task, "Hindi please, and actually end the call.")

            # Dropped: the LLM does not run while the greeting plays, and the
            # greeting plays to the end.
            assert await _wait_for(lambda: rec.bot_stopped, timeout=6.0)
            assert rec.bot_stopped[0] - started >= GREETING_AUDIO_MS / 1000 - 0.5, (
                f"greeting was cut short: {rec.bot_stopped[0] - started:.2f}s"
            )
            assert llm.get_current_step() == 0, "mid-greeting speech reached the LLM"

            # After the greeting the caller is heard normally.
            assert await _wait_for(lambda: user_agg._user_is_muted is False, timeout=2.0)
            await _say(pipeline_task, "End the call please.")
            await asyncio.wait_for(run_task, timeout=10.0)

        assert llm.get_current_step() >= 1
        refreshed = await db_session.get_workflow_run_by_id(setup[0].id)
        assert refreshed.state == WorkflowRunState.COMPLETED.value
    finally:
        await _cleanup(pipeline_task)


@pytest.mark.asyncio
async def test_switch_on_caller_interrupts_greeting(db_session, async_session):
    setup = await _setup(db_session, async_session, True, "on")
    await asyncio.wait_for(_body_switch_on(setup, db_session), HARD_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_switch_off_greeting_plays_through_and_mid_greeting_speech_is_dropped(
    db_session, async_session
):
    setup = await _setup(db_session, async_session, False, "off")
    await asyncio.wait_for(_body_switch_off(setup, db_session), HARD_TIMEOUT_SECONDS)
