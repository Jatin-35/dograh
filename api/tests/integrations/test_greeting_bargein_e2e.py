"""Greeting barge-in end to end on the real ``_run_pipeline``.

With the start node's "Allow greeting interruption" on, a caller who talks over
the greeting stops it and is answered. With it off, the greeting plays to the
end. Only the LLM and TTS are mocked; turn strategies, mute strategies, the
greeting controller and the engine are the production ones.
"""

import asyncio

import pytest
from unittest.mock import patch

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    InterruptionFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.tests import MockLLMService, MockTTSService
from pipecat.utils.time import time_now_iso8601

from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_text_greeting import _wait_for
from api.tests.integrations.test_run_pipeline_turn_taking import (
    _cleanup,
    _definition,
    _end_call_step,
    _ObservedTasks,
    _Recorder,
    _start,
)

HARD_TIMEOUT_SECONDS = 45.0
GREETING_MS = 4000


class _InterruptRecorder(_Recorder):
    def __init__(self):
        super().__init__()
        self.interruptions = 0

    async def on_push_frame(self, data: FramePushed):
        if isinstance(data.frame, InterruptionFrame) and data.frame.id not in self._seen:
            self.interruptions += 1
        await super().on_push_frame(data)


async def _setup(db_session, async_session, suffix, *, interrupt_greeting, allow_interrupt):
    definition = _definition(allow_interrupt)
    definition["nodes"][0]["data"]["allow_interrupt_greeting"] = interrupt_greeting
    return await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=definition,
        name_prefix=f"Greeting Bargein {suffix}",
        provider_id_suffix=f"greeting-bargein-{suffix}",
    )


async def _talk_over_greeting(setup, *, words="haan ji bataiye na", flux=False):
    llm = MockLLMService(
        mock_steps=[MockLLMService.create_text_chunks("Ji, boliye."), _end_call_step()],
        chunk_delay=0.001,
    )
    tts = MockTTSService(mock_audio_duration_ms=GREETING_MS, frame_delay=0)
    captured = _ObservedTasks(_InterruptRecorder())
    task = None
    run_task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts), patch(
            "api.services.pipecat.run_pipeline.stt_uses_external_turns", return_value=flux
        ):
            run_task, task, _ = await _start(setup, captured)
            rec = captured.recorder
            assert await _wait_for(lambda: rec.bot_started, timeout=8.0)
            greeting_started = rec.bot_started[0]
            await asyncio.sleep(0.8)
            if flux:
                # What Deepgram Flux emits: a proposed turn start, interim words
                # while the caller talks, then the final transcript and a
                # proposed stop at its end of turn.
                await task.queue_frame(ProposedUserStartedSpeakingFrame())
                for i, _ in enumerate(words.split(), start=1):
                    await asyncio.sleep(0.15)
                    partial = " ".join(words.split()[:i])
                    await task.queue_frame(
                        InterimTranscriptionFrame(partial, "caller", time_now_iso8601())
                    )
                await asyncio.sleep(0.2)
                await task.queue_frame(
                    TranscriptionFrame(text=words, user_id="caller", timestamp=time_now_iso8601())
                )
                await task.queue_frame(ProposedUserStoppedSpeakingFrame())
            else:
                await task.queue_frame(VADUserStartedSpeakingFrame())
                await asyncio.sleep(0.3)
                await task.queue_frame(
                    TranscriptionFrame(text=words, user_id="caller", timestamp=time_now_iso8601())
                )
                await asyncio.sleep(0.2)
                await task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
            await _wait_for(lambda: rec.bot_stopped, timeout=GREETING_MS / 1000 + 4)
            greeting_ms = (rec.bot_stopped[0] - greeting_started) * 1000 if rec.bot_stopped else None
            answered = await _wait_for(lambda: llm.get_current_step() >= 1, timeout=6.0)
            return greeting_ms, rec.interruptions, answered
    finally:
        await _cleanup(task, run_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_interrupt", [True, False], ids=["node-interruptible", "node-not-interruptible"])
async def test_switch_on_talking_over_the_greeting_stops_it(db_session, async_session, allow_interrupt):
    setup = await _setup(
        db_session, async_session, f"on-{allow_interrupt}", interrupt_greeting=True, allow_interrupt=allow_interrupt
    )
    greeting_ms, interruptions, answered = await asyncio.wait_for(
        _talk_over_greeting(setup), HARD_TIMEOUT_SECONDS
    )
    print(f"switch on, allow_interrupt={allow_interrupt}: greeting {greeting_ms} ms, interruptions {interruptions}, answered {answered}")
    assert greeting_ms is not None and greeting_ms < GREETING_MS - 1000, greeting_ms
    assert interruptions >= 1
    assert answered


@pytest.mark.asyncio
async def test_switch_off_the_greeting_plays_to_the_end(db_session, async_session):
    setup = await _setup(db_session, async_session, "off", interrupt_greeting=False, allow_interrupt=True)
    greeting_ms, interruptions, _answered = await asyncio.wait_for(
        _talk_over_greeting(setup), HARD_TIMEOUT_SECONDS
    )
    print(f"switch off: greeting {greeting_ms} ms, interruptions {interruptions}")
    assert greeting_ms is not None and greeting_ms >= GREETING_MS - 500, greeting_ms


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["default", "min_words"])
async def test_switch_on_flux_caller_talking_over_the_greeting_stops_it(
    db_session, async_session, strategy
):
    """Deepgram Flux decides turns itself (external turns), as on prod."""
    from sqlalchemy import update

    from api.db.models import WorkflowDefinitionModel

    setup = await _setup(
        db_session, async_session, f"flux-{strategy}", interrupt_greeting=True, allow_interrupt=True
    )
    await async_session.execute(
        update(WorkflowDefinitionModel)
        .where(WorkflowDefinitionModel.id == setup[0].definition_id)
        .values(workflow_configurations={"turn_start_strategy": strategy})
    )
    await async_session.commit()
    greeting_ms, interruptions, answered = await asyncio.wait_for(
        _talk_over_greeting(setup, flux=True), HARD_TIMEOUT_SECONDS
    )
    print(f"flux {strategy}: greeting {greeting_ms} ms, interruptions {interruptions}, answered {answered}")
    assert greeting_ms is not None and greeting_ms < GREETING_MS - 1000, greeting_ms
    assert interruptions >= 1
    assert answered
