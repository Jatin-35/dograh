"""End-to-end turn-taking tests on the real ``_run_pipeline``.

These drive the whole pipeline (transport, user/assistant aggregators, the
turn controller, our VAD and turn strategies, the workflow engine and the test
database) with only the STT/LLM/TTS services mocked, to check the turn-taking
changes together rather than one class at a time:

- the run wires Silero v6.2, two-word interruptions, our fixed turn-stop
  strategy and empty-turn recovery, and a retired ``provisional_vad`` setting
  still loads;
- real speech audio through the pipeline is detected by the new VAD on 8 kHz
  phone audio and on 16 kHz browser audio;
- a caller sound that cuts the bot off with no words makes the bot ask them to
  repeat instead of going silent;
- the per-turn latency breakdown is saved in the run's logs.
"""

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
import soxr
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.utils.time import time_now_iso8601
from sqlalchemy import update

from api.db.models import WorkflowDefinitionModel
from api.enums import WorkflowRunMode, WorkflowRunState
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.empty_turn_recovery import (
    EMPTY_TURN_INTERRUPTED_PROMPT,
    BotActivityObserver,
)
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.silero_vad import DograhSileroVADAnalyzer
from api.services.pipecat.turn_stop_strategies import DograhSpeechTimeoutUserTurnStopStrategy
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

HARD_TIMEOUT_SECONDS = 45.0
SPEECH_PATH = Path(__file__).resolve().parents[1] / "assets" / "continuous_speech_16k.wav"


def _definition(allow_interrupt: bool = True) -> dict:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "startCall",
                "position": {"x": 0, "y": 0},
                "data": {
                    "name": "Start",
                    "prompt": "You are Aastha. Help with water tanks. End the call when asked.",
                    "is_start": True,
                    "allow_interrupt": allow_interrupt,
                    "add_global_prompt": False,
                    "greeting": "Namaskar, main Aastha bol rahi hoon.",
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


class _Recorder(BaseObserver):
    def __init__(self):
        super().__init__()
        self._seen: set[int] = set()
        self.bot_started: list[float] = []
        self.bot_stopped: list[float] = []
        self.user_started = 0
        self.vad_started = 0

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        now = asyncio.get_event_loop().time()
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_started.append(now)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_stopped.append(now)
        elif isinstance(frame, UserStartedSpeakingFrame):
            self.user_started += 1
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.vad_started += 1


class _ObservedTasks(list):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def append(self, task):
        task.add_observer(self.recorder)
        super().append(task)


async def _setup(db_session, async_session, suffix, configs=None, allow_interrupt=True):
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(allow_interrupt),
        name_prefix=f"Turn Taking {suffix}",
        provider_id_suffix=f"turn-taking-{suffix}",
    )
    if configs:
        await async_session.execute(
            update(WorkflowDefinitionModel)
            .where(WorkflowDefinitionModel.id == run.definition_id)
            .values(workflow_configurations=configs)
        )
        await async_session.commit()
    return run, user, workflow


async def _start(setup, captured, mode=WorkflowRunMode.SMALLWEBRTC.value):
    run, user, workflow = setup
    audio_config = create_audio_config(mode)
    transport = MockTransport(
        TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=audio_config.transport_out_sample_rate,
        )
    )
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
    for _ in range(80):
        if captured or run_task.done():
            break
        await asyncio.sleep(0.05)
    if run_task.done() and not captured:
        run_task.result()
    assert captured, "create_pipeline_task was never invoked"
    task = captured[0]
    await wait_for_pipeline_worker_started(task, timeout=3.0, run_task=run_task)
    return run_task, task, audio_config


async def _say(task, text):
    await task.queue_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=time_now_iso8601()))


async def _cleanup(task):
    if task is not None and not task.has_finished():
        try:
            await asyncio.wait_for(task.cancel(), timeout=3.0)
        except Exception:
            pass


def _end_call_step():
    return MockLLMService.create_function_call_chunks(
        function_name="end_call", arguments={}, tool_call_id="call_end_1"
    )


def _turn_strategies(user_agg):
    return user_agg._user_turn_controller._user_turn_strategies


# ── 1. Wiring ─────────────────────────────────────────────────────────


async def _body_wiring(setup, db_session, expect_start_cls=MinWordsUserTurnStartStrategy):
    llm = MockLLMService(mock_steps=[_end_call_step()], chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=300, frame_delay=0)
    captured = _ObservedTasks(_Recorder())
    task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            run_task, task, _ = await _start(setup, captured)
            user_agg = _find_processor_by_class_name(task, "LLMUserAggregator")
            strategies = _turn_strategies(user_agg)
            start = strategies.start
            assert len(start) == 1 and isinstance(start[0], expect_start_cls)
            assert start[0]._min_words == 2
            assert isinstance(strategies.stop[0], DograhSpeechTimeoutUserTurnStopStrategy)
            assert isinstance(user_agg._params.vad_analyzer, DograhSileroVADAnalyzer)
            assert any(isinstance(o, BotActivityObserver) for o in task._observer._observers), (
                "empty-turn recovery is not attached"
            )
            rec = captured.recorder
            assert await _wait_for(lambda: rec.bot_stopped, timeout=5.0)  # greeting played
            await _say(task, "Thank you, please end the call now.")
            await asyncio.wait_for(run_task, timeout=10.0)
        refreshed = await db_session.get_workflow_run_by_id(setup[0].id)
        assert refreshed.state == WorkflowRunState.COMPLETED.value
    finally:
        await _cleanup(task)


@pytest.mark.asyncio
async def test_new_turn_taking_is_wired_into_a_cascaded_call(db_session, async_session):
    setup = await _setup(db_session, async_session, "wiring")
    await asyncio.wait_for(_body_wiring(setup, db_session), HARD_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_agent_saved_with_retired_provisional_vad_still_runs(db_session, async_session):
    setup = await _setup(
        db_session,
        async_session,
        "provisional",
        configs={"turn_start_strategy": "provisional_vad", "provisional_vad_pause_secs": 1.5},
    )
    await asyncio.wait_for(_body_wiring(setup, db_session), HARD_TIMEOUT_SECONDS)


# ── 2. Real speech audio through the pipeline's VAD ───────────────────


def _speech_frames(sample_rate: int, seconds_silence: float = 1.0):
    speech, sr = sf.read(SPEECH_PATH, dtype="int16")
    audio = speech.astype(np.float32)
    if sample_rate != sr:
        audio = soxr.resample(audio, sr, sample_rate)
    silence = np.zeros(int(seconds_silence * sample_rate), dtype=np.float32)
    pcm = np.clip(np.concatenate([audio[: int(2.5 * sample_rate)], silence]), -32768, 32767).astype(np.int16)
    step = int(0.02 * sample_rate)  # 20 ms frames like a real transport
    for i in range(0, len(pcm) - step + 1, step):
        yield InputAudioRawFrame(audio=pcm[i : i + step].tobytes(), sample_rate=sample_rate, num_channels=1)


async def _body_vad_hears_speech(setup, mode):
    llm = MockLLMService(mock_steps=[], chunk_delay=0.001)
    tts = MockTTSService(mock_audio_duration_ms=200, frame_delay=0)
    captured = _ObservedTasks(_Recorder())
    task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            _, task, audio_config = await _start(setup, captured, mode=mode)
            rec = captured.recorder
            assert await _wait_for(lambda: rec.bot_stopped, timeout=5.0)
            for frame in _speech_frames(audio_config.transport_in_sample_rate):
                await task.queue_frame(frame)
                await asyncio.sleep(0.02)  # real-time pacing
            assert await _wait_for(lambda: rec.vad_started >= 1, timeout=3.0), (
                "Silero v6.2 did not detect the caller's speech"
            )
            # Voice-activity turn start: the detected speech opened a user turn.
            assert rec.user_started >= 1
    finally:
        await _cleanup(task)


@pytest.mark.asyncio
async def test_vad_detects_real_speech_on_8khz_phone_audio(db_session, async_session):
    setup = await _setup(db_session, async_session, "vad8k", configs={"turn_start_strategy": "default"})
    await asyncio.wait_for(_body_vad_hears_speech(setup, "twilio"), HARD_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_vad_detects_real_speech_on_16khz_browser_audio(db_session, async_session):
    setup = await _setup(db_session, async_session, "vad16k", configs={"turn_start_strategy": "default"})
    await asyncio.wait_for(
        _body_vad_hears_speech(setup, WorkflowRunMode.SMALLWEBRTC.value), HARD_TIMEOUT_SECONDS
    )


# ── 3. Empty turn that cuts the bot off ───────────────────────────────


async def _body_empty_turn(setup, db_session, recovery_on=True):
    llm = MockLLMService(
        mock_steps=[
            MockLLMService.create_text_chunks(
                "Ji, hamare paas 500 litre se 5000 litre tak tank hain, aapko kitne litre ka chahiye?"
            ),
            MockLLMService.create_text_chunks("Sorry, clear nahi suna, phir se boliye? Kitne litre ka tank?"),
            _end_call_step(),
        ],
        chunk_delay=0.001,
    )
    tts = MockTTSService(mock_audio_duration_ms=3000, frame_delay=0)
    captured = _ObservedTasks(_Recorder())
    task = None
    try:
        with patch_run_pipeline_externals(captured, llm=llm, tts=tts):
            run_task, task, _ = await _start(setup, captured)
            rec = captured.recorder
            user_agg = _find_processor_by_class_name(task, "LLMUserAggregator")
            assert await _wait_for(lambda: len(rec.bot_stopped) >= 1, timeout=8.0)  # greeting

            await _say(task, "Mujhe ek water tank chahiye")
            assert await _wait_for(lambda: llm.get_current_step() >= 1, timeout=5.0)
            # The bot starts its long reply ...
            assert await _wait_for(lambda: len(rec.bot_started) >= 2, timeout=5.0)
            await asyncio.sleep(0.5)
            # ... and a cough cuts it off: voice activity, no words at all.
            await task.queue_frame(VADUserStartedSpeakingFrame())
            await asyncio.sleep(0.3)
            await task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))

            if not recovery_on:
                # Control: stock behaviour. The reply was cut and nothing runs
                # the LLM again, so the bot sits silent.
                assert not await _wait_for(lambda: llm.get_current_step() >= 2, timeout=8.0)
                return

            # With recovery the LLM runs again and the bot asks to repeat.
            assert await _wait_for(lambda: llm.get_current_step() >= 2, timeout=12.0), (
                "bot stayed silent after the empty interruption"
            )
            assert any(
                m.get("role") == "system" and m.get("content") == EMPTY_TURN_INTERRUPTED_PROMPT
                for m in user_agg.context.messages
                if isinstance(m, dict)
            )
            # The bot speaks the whole "please repeat" reply ...
            assert await _wait_for(lambda: len(rec.bot_stopped) >= 3, timeout=10.0)
            assert "phir se boliye" in " ".join(tts.received_texts)
            # ... and the caller answers normally.
            await _say(task, "2000 litre, ab call end kar dijiye")
            await asyncio.wait_for(run_task, timeout=15.0)

        refreshed = await db_session.get_workflow_run_by_id(setup[0].id)
        assert refreshed.state == WorkflowRunState.COMPLETED.value
        events = (refreshed.logs or {}).get("realtime_feedback_events", [])
        # The per-turn latency breakdown is saved next to the total.
        breakdowns = [e for e in events if e.get("type") == "rtf-latency-breakdown"]
        assert breakdowns, "no latency breakdown saved in the run logs"
        assert "ttfb" in breakdowns[0]["payload"]
    finally:
        await _cleanup(task)


@pytest.mark.asyncio
async def test_empty_interruption_gets_the_caller_asked_to_repeat(db_session, async_session):
    setup = await _setup(
        db_session, async_session, "empty-turn", configs={"turn_start_strategy": "default"}
    )
    await asyncio.wait_for(_body_empty_turn(setup, db_session), HARD_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_without_recovery_the_bot_goes_silent_after_an_empty_interruption(
    db_session, async_session
):
    setup = await _setup(
        db_session,
        async_session,
        "empty-turn-off",
        configs={"turn_start_strategy": "default", "empty_turn_recovery": False},
    )
    await asyncio.wait_for(_body_empty_turn(setup, db_session, recovery_on=False), HARD_TIMEOUT_SECONDS)
