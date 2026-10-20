"""End-to-end: ambient sound play modes on the real pipeline.

Each test runs ``_run_pipeline`` with the transport's mixer built exactly as
the telephony and WebRTC transports build it, and a mock TTS whose "speech" is
digital silence, so everything the transport sends that isn't zero is the
ambient sound. The tests then check what the caller heard in each phase of the
call: the greeting, the caller's turns, tool calls, the bot's replies.

Set AMBIENT_TEST_EXPORT_DIR to also save what the caller heard as WAV files.
"""

import asyncio
import os
import wave
from dataclasses import dataclass, field

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.tests.mock_transport import MockOutputTransport, MockTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.time import time_now_iso8601
from sqlalchemy import update

from api.db.models import WorkflowDefinitionModel, WorkflowModel
from api.enums import WorkflowRunMode, WorkflowRunState
from api.services.pipecat.ambient_sound import AmbientSoundGate, GatedSoundfileMixer
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.worker_runner import wait_for_pipeline_worker_started
from api.tests.integrations._run_pipeline_helpers import (
    create_workflow_run_rows,
    patch_run_pipeline_externals,
)
from api.tests.integrations.test_run_pipeline_text_greeting import _wait_for
from pipecat.tests import MockLLMService, MockTTSService

HARD_TIMEOUT_SECONDS = 60.0
TOOL_SECS = 2.0
EXPORT_DIR = os.environ.get("AMBIENT_TEST_EXPORT_DIR")
BROWSER = WorkflowRunMode.SMALLWEBRTC.value
# Converting the mock TTS's silent 16 kHz speech to an 8 kHz phone line leaves
# +-1 rounding noise (about -90 dBFS, inaudible); count that as silence.
SILENT = 2.0
# Clearly audible: the soft office ambience at volume 0.5 sits around 70-110
# RMS (and typing louder), against exact silence when nothing plays.
AUDIBLE = 20.0

DEFINITION = {
    "nodes": [
        {
            "id": "start",
            "type": "startCall",
            "position": {"x": 0, "y": 0},
            "data": {
                "name": "Start",
                "prompt": "You are Aastha. Look up the area manager when asked.",
                "is_start": True,
                "allow_interrupt": True,
                "add_global_prompt": False,
                "greeting": "Namaskar, main Aastha bol rahi hoon.",
                "greeting_type": "text",
            },
        },
        {
            "id": "end",
            "type": "endCall",
            "position": {"x": 0, "y": 200},
            "data": {"name": "End", "prompt": "End politely.", "is_end": True, "add_global_prompt": False},
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

PHASES = {
    BotStartedSpeakingFrame: "bot_started",
    BotStoppedSpeakingFrame: "bot_stopped",
    UserStartedSpeakingFrame: "user_started",
    UserStoppedSpeakingFrame: "user_stopped",
    FunctionCallsStartedFrame: "tool_started",
    FunctionCallResultFrame: "tool_done",
}


class _Timeline(BaseObserver):
    """When each phase of the call happened (loop time)."""

    def __init__(self):
        super().__init__()
        self._seen: set[int] = set()
        self.events: list[tuple[str, float]] = []

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        name = PHASES.get(type(frame))
        if not name:
            return
        now = asyncio.get_running_loop().time()
        # Speaking frames go both upstream and downstream as separate frames.
        if self.events and self.events[-1][0] == name and now - self.events[-1][1] < 0.05:
            return
        self.events.append((name, now))

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.events if n == name)

    def at(self, name: str, nth: int = 0) -> float:
        return [t for n, t in self.events if n == name][nth]

    def first_after(self, name: str, after: float) -> float:
        return [t for n, t in self.events if n == name and t > after][0]


class _ObservedTasks(list):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer

    def append(self, task):
        task.add_observer(self.observer)
        super().append(task)


@dataclass
class Call:
    """A finished test call: what happened when, and what the caller heard."""

    timeline: _Timeline
    rate: int
    written: list[tuple[float, bytes]] = field(default_factory=list)
    gate: AmbientSoundGate | None = None

    def chunks(self, start: float, end: float) -> list[float]:
        levels = [
            float(np.sqrt((np.frombuffer(a, np.int16).astype(np.float64) ** 2).mean()))
            for t, a in self.written
            if start <= t < end
        ]
        assert levels, f"no audio written between {start:.2f} and {end:.2f}"
        return levels

    def loudest(self, start: float, end: float) -> float:
        """Loudest 20 ms the caller heard between two moments (0 = silence)."""
        return max(self.chunks(start, end))

    def share_with_sound(self, start: float, end: float) -> float:
        """Share of chunks carrying sound (typing has gaps between keys)."""
        levels = self.chunks(start, end)
        return sum(1 for level in levels if level > 0) / len(levels)

    def export(self, name: str) -> None:
        if not EXPORT_DIR:
            return
        os.makedirs(EXPORT_DIR, exist_ok=True)
        with wave.open(os.path.join(EXPORT_DIR, f"{name}.wav"), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            wf.writeframes(b"".join(a for _, a in self.written))


async def _run_call(
    db_session, async_session, monkeypatch, *, ambient, llm, script, name, mode=BROWSER, configs=None, voice=False
):
    """Run one call; ``script(say, vad_turn, timeline)`` drives the caller's side."""
    run, user, workflow = await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=DEFINITION,
        name_prefix=f"Ambient {name}",
        provider_id_suffix=f"ambient-{name}",
    )
    workflow_configurations = {"ambient_noise_configuration": ambient, **(configs or {})}
    for model, row_id in ((WorkflowDefinitionModel, run.definition_id), (WorkflowModel, workflow.id)):
        await async_session.execute(
            update(model).where(model.id == row_id).values(workflow_configurations=workflow_configurations)
        )
    await async_session.commit()

    audio_config = create_audio_config(mode)
    rate = audio_config.transport_out_sample_rate
    call = Call(timeline=_Timeline(), rate=rate)
    original_write = MockOutputTransport.write_audio_frame

    async def recording_write(self, frame: OutputAudioRawFrame) -> bool:
        call.written.append((asyncio.get_running_loop().time(), bytes(frame.audio)))
        return await original_write(self, frame)

    monkeypatch.setattr(MockOutputTransport, "write_audio_frame", recording_write)

    if voice:
        tts = MockTTSService(mock_audio_duration_ms=1200, frame_delay=0)  # audible speech
    else:
        tts = MockTTSService(mock_audio_data=b"\x00" * int(rate * 1.2) * 2, frame_delay=0)  # 1.2 s of silent speech
    mixer = await build_audio_out_mixer(rate, ambient)
    transport = MockTransport(
        TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=rate,
            audio_out_mixer=mixer,
        )
    )
    captured = _ObservedTasks(call.timeline)
    task = None

    async def say(text):
        await task.queue_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=time_now_iso8601()))

    async def vad_turn(secs=0.4):
        """A sound from the caller with no words (cough, horn)."""
        await task.queue_frame(VADUserStartedSpeakingFrame())
        await asyncio.sleep(secs)
        await task.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))

    async def body():
        nonlocal task
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
            assert await _wait_for(lambda: bool(captured) or run_task.done(), timeout=5.0)
            task = captured[0]
            await wait_for_pipeline_worker_started(task, timeout=3.0, run_task=run_task)
            gates = [o for o in task._observer._observers if isinstance(o, AmbientSoundGate)]
            call.gate = gates[0] if gates else None
            assert await _wait_for(lambda: call.timeline.count("bot_stopped") >= 1, timeout=8.0)  # greeting
            await script(say, vad_turn, call.timeline)
            await asyncio.wait_for(run_task, timeout=15.0)

    try:
        await asyncio.wait_for(body(), HARD_TIMEOUT_SECONDS)
    finally:
        if task is not None and not task.has_finished():
            await task.cancel()

    refreshed = await db_session.get_workflow_run_by_id(run.id)
    assert refreshed.state == WorkflowRunState.COMPLETED.value
    call.export(name)
    return call


def _lookup_tool(llm, secs=TOOL_SECS):
    async def slow_lookup(params):
        await asyncio.sleep(secs)
        await params.result_callback({"area_manager": "Rohit, 98xxxxxx10"})

    llm.register_function("vectus_area_manager_lookup", slow_lookup)


def _tool_step():
    return MockLLMService.create_function_call_chunks(
        function_name="vectus_area_manager_lookup", arguments={"pincode": "184121"}, tool_call_id="call_lookup"
    )


def _end_step():
    return MockLLMService.create_function_call_chunks(function_name="end_call", arguments={}, tool_call_id="call_end")


def _ambient(play_mode, sound="office", start_delay_secs=0.3):
    return {"enabled": True, "volume": 0.5, "play_mode": play_mode, "start_delay_secs": start_delay_secs, "sound": sound}


# ── Only during tool calls ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,sound",
    [(BROWSER, "typing"), ("twilio", "office")],
    ids=["browser-16k-typing", "phone-8k-office"],
)
async def test_tool_calls_mode_plays_only_while_the_tool_runs(db_session, async_session, monkeypatch, mode, sound):
    llm = MockLLMService(
        mock_steps=[
            _tool_step(),
            MockLLMService.create_text_chunks("Aapke area manager Rohit ji hain."),
            MockLLMService.create_text_chunks("Ji, aur kuch?"),
            _end_step(),
        ],
        chunk_delay=0.001,
    )
    _lookup_tool(llm)

    async def script(say, vad_turn, tl):
        await say("Mera pincode 184121 hai, area manager bataiye")
        assert await _wait_for(lambda: tl.count("tool_done") >= 1, timeout=10.0)
        assert await _wait_for(lambda: tl.count("bot_stopped") >= 2, timeout=8.0)
        await asyncio.sleep(0.5)
        await say("Theek hai")  # quick reply, no tool
        assert await _wait_for(lambda: tl.count("bot_stopped") >= 3, timeout=8.0)
        await asyncio.sleep(0.5)
        await say("Bas, call end kar dijiye")

    call = await _run_call(
        db_session, async_session, monkeypatch, ambient=_ambient("tool_calls", sound), llm=llm,
        script=script, mode=mode, name=f"tool-calls-{sound}-{mode}",
    )
    tl = call.timeline
    assert call.gate is not None
    tool_started, tool_done = tl.at("tool_started"), tl.at("tool_done")
    greeting = (tl.at("bot_started", 0), tl.at("bot_stopped", 0))
    answer = (tl.at("bot_started", 1), tl.at("bot_stopped", 1))

    assert call.loudest(*greeting) < SILENT
    assert call.loudest(greeting[1], tool_started) < SILENT  # caller talking, LLM deciding
    assert call.loudest(tool_started, tool_started + 0.25) < SILENT  # the start delay
    assert call.loudest(tool_started + 0.6, tool_done - 0.1) > AUDIBLE
    assert call.share_with_sound(tool_started + 0.6, tool_done - 0.1) > 0.15
    assert call.loudest(answer[0] + 0.25, answer[1]) < SILENT  # after the 150 ms fade-out
    assert call.loudest(answer[1], tl.at("bot_stopped", 2)) < SILENT  # quick reply, no tool
    # The end_call tool never triggers the sound.
    assert call.loudest(tl.at("bot_stopped", 2), call.written[-1][0] + 0.01) < SILENT


@pytest.mark.asyncio
async def test_caller_talking_during_a_tool_silences_the_sound(db_session, async_session, monkeypatch):
    llm = MockLLMService(
        mock_steps=[_tool_step(), MockLLMService.create_text_chunks("Rohit ji aapke area manager hain."), _end_step()],
        chunk_delay=0.001,
    )
    _lookup_tool(llm, secs=4.0)

    async def script(say, vad_turn, tl):
        await say("Area manager ka number chahiye")
        assert await _wait_for(lambda: tl.count("tool_started") >= 1, timeout=8.0)
        await asyncio.sleep(1.0)  # sound playing
        await say("haan ji main wait kar raha hoon")  # caller speaks over the wait
        assert await _wait_for(lambda: tl.count("tool_done") >= 1, timeout=10.0)
        assert await _wait_for(lambda: tl.count("bot_stopped") >= 2, timeout=8.0)
        await asyncio.sleep(0.3)
        await say("Bas, call end kar dijiye")

    call = await _run_call(
        db_session, async_session, monkeypatch, ambient=_ambient("tool_calls", start_delay_secs=0.2),
        llm=llm, script=script, name="tool-calls-caller-talks",
    )
    tl = call.timeline
    tool_started = tl.at("tool_started")
    talk = tl.first_after("user_started", tool_started)
    talk_end = tl.first_after("user_stopped", talk)
    assert call.loudest(tool_started + 0.4, talk) > AUDIBLE  # playing before the caller cut in
    assert call.loudest(talk + 0.2, talk_end) < SILENT  # silent while the caller talks


# ── While the bot is thinking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_thinking_mode_fills_the_gap_before_a_slow_reply(db_session, async_session, monkeypatch):
    slow_reply = MockLLMService.create_text_chunks(
        "Ji bilkul, main aapki madad kar sakti hoon. Aapka pincode kya hai?", chunk_size=6
    )
    llm = MockLLMService(mock_steps=[slow_reply, _end_step()], chunk_delay=0.15)  # ~1 s to the first sentence

    async def script(say, vad_turn, tl):
        await say("Mujhe ek water tank chahiye")
        assert await _wait_for(lambda: tl.count("bot_stopped") >= 2, timeout=10.0)
        await asyncio.sleep(0.5)
        await say("Bas, call end kar dijiye")

    call = await _run_call(
        db_session, async_session, monkeypatch, ambient=_ambient("thinking"), llm=llm,
        script=script, name="thinking-slow-reply",
    )
    tl = call.timeline
    turn_end, reply = tl.at("user_stopped", 0), tl.at("bot_started", 1)
    assert reply - turn_end > 0.8, "the reply should have been slow"
    assert call.loudest(tl.at("bot_started", 0), tl.at("bot_stopped", 0)) < SILENT  # greeting
    assert call.loudest(tl.at("bot_stopped", 0), turn_end) < SILENT  # caller's turn
    assert call.loudest(turn_end + 0.5, reply - 0.05) > AUDIBLE  # thinking: sound
    assert call.loudest(reply + 0.25, tl.at("bot_stopped", 1)) < SILENT  # under the bot's voice


@pytest.mark.asyncio
async def test_thinking_mode_ignores_a_noise_turn_the_bot_does_not_answer(db_session, async_session, monkeypatch):
    # Voice-activity turns: a cough while the bot waits opens and closes a
    # turn with no words, and the bot (rightly) doesn't answer it. The sound
    # must not start and then run on with no reply coming.
    llm = MockLLMService(mock_steps=[_end_step()], chunk_delay=0.001)

    async def script(say, vad_turn, tl):
        await asyncio.sleep(0.5)
        await vad_turn()  # cough while the bot is idle
        assert await _wait_for(lambda: tl.count("user_stopped") >= 1, timeout=8.0)
        await asyncio.sleep(4.0)  # nothing answers
        await say("Bas, call end kar dijiye")

    call = await _run_call(
        db_session, async_session, monkeypatch, ambient=_ambient("thinking"), llm=llm,
        script=script, name="thinking-noise-turn", configs={"turn_start_strategy": "default"},
    )
    tl = call.timeline
    noise_end = tl.at("user_stopped", 0)
    next_turn = tl.first_after("user_started", noise_end)
    # At most a short blip right after the noise; never sound running on.
    assert call.loudest(noise_end + 2.5, next_turn) < SILENT

# ── The bot's first word ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_bots_first_word_is_not_under_the_sound(db_session, async_session, monkeypatch):
    # "While the agent is thinking": the sound plays right up to the moment
    # the bot starts its answer (here after a tool call). With an audible voice, what the mixer adds to each chunk must be
    # nothing from 10 ms into the bot's first word.
    mixes: list[tuple[bytes, bytes]] = []
    original_mix = GatedSoundfileMixer._mix_with_sound

    def recording_mix(self, audio):
        out = original_mix(self, audio)
        mixes.append((audio, out))
        return out

    monkeypatch.setattr(GatedSoundfileMixer, "_mix_with_sound", recording_mix)
    llm = MockLLMService(
        mock_steps=[_tool_step(), MockLLMService.create_text_chunks("Rohit ji aapke area manager hain."), _end_step()],
        chunk_delay=0.001,
    )
    _lookup_tool(llm)

    async def script(say, vad_turn, tl):
        await say("Area manager ka number chahiye")
        assert await _wait_for(lambda: tl.count("tool_done") >= 1, timeout=10.0)
        assert await _wait_for(lambda: tl.count("bot_stopped") >= 2, timeout=8.0)
        await asyncio.sleep(0.3)
        await say("Bas, call end kar dijiye")

    await _run_call(
        db_session, async_session, monkeypatch, ambient=_ambient("thinking", "typing", start_delay_secs=0.2),
        llm=llm, script=script, name="first-word-thinking", voice=True,
    )
    is_voice = [int(np.abs(np.frombuffer(src, np.int16)).max()) > 300 for src, _ in mixes]
    added = [
        np.frombuffer(out, np.int16).astype(np.int32) - np.frombuffer(src, np.int16).astype(np.int32)
        for src, out in mixes
    ]
    playing = [bool(np.any(a)) for a in added]
    # The answer after the tool: the first voice chunk right after sound was playing.
    first_answer = next(i for i in range(1, len(mixes)) if is_voice[i] and any(playing[max(0, i - 5) : i]))
    assert sum(playing[first_answer - 25 : first_answer]) > 5, "sound wasn't playing before the answer"
    samples_per_chunk = len(np.frombuffer(mixes[first_answer][0], np.int16))
    cut = int(samples_per_chunk / 0.02 * 0.01)  # 10 ms
    assert not np.any(added[first_answer][cut:]), "sound under the bot's first word"
    for i in range(first_answer + 1, len(mixes)):
        if is_voice[i]:
            assert not np.any(added[i]), f"sound under the bot's voice at chunk {i}"
