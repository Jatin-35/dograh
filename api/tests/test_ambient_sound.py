"""Ambient sound played only while the caller waits (thinking / tool calls)."""

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from pipecat.audio.mixers.silence_mixer import SilenceAudioMixer
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    FunctionCallCancelFrame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pydantic import ValidationError

from api.constants import APP_ROOT_DIR
from api.schemas.workflow_configurations import AmbientNoiseConfigurationDefaults
from api.services.pipecat.ambient_sound import (
    PLAY_THINKING,
    PLAY_TOOL_CALLS,
    AmbientSoundGate,
    GatedSoundfileMixer,
    attach_ambient_sound_gate,
)
from api.services.pipecat.audio_mixer import build_audio_out_mixer

RATE = 8000
CHUNK = b"\x00" * int(RATE * 0.02) * 2  # 20 ms of silence
DELAY = 0.05


async def _mixer(fade_secs=0.1, volume=1.0):
    mixer = GatedSoundfileMixer(
        sound_files={"office": APP_ROOT_DIR / "assets" / f"office-ambience-{RATE}-mono.wav"},
        default_sound="office",
        volume=volume,
        fade_secs=fade_secs,
    )
    await mixer.start(RATE)
    return mixer


def _rms(audio: bytes) -> float:
    a = np.frombuffer(audio, dtype=np.int16).astype(np.float64)
    return float(np.sqrt((a**2).mean()))


async def _level(mixer, chunks=10):
    """Average loudness of what the caller hears over ``chunks`` x 20 ms."""
    return sum([_rms(await mixer.mix(CHUNK)) for _ in range(chunks)]) / chunks


async def _push(gate, frame):
    await gate.on_push_frame(SimpleNamespace(frame=frame))


def _call(name="vectus_area_manager_lookup", tool_call_id="c1"):
    return FunctionCallFromLLM(function_name=name, tool_call_id=tool_call_id, arguments={}, context=None)


def _result(tool_call_id="c1", is_final=True):
    return FunctionCallResultFrame(
        function_name="t",
        tool_call_id=tool_call_id,
        arguments={},
        result={},
        properties=FunctionCallResultProperties(is_final=is_final),
    )


async def _settle(secs=DELAY + 0.03):
    await asyncio.sleep(secs)


# ── The fading mixer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gated_mixer_starts_silent_and_leaves_bot_audio_untouched():
    mixer = await _mixer()
    voice = (np.ones(len(CHUNK) // 2) * 1000).astype(np.int16).tobytes()
    assert await mixer.mix(voice) == voice
    assert await _level(mixer) == 0


@pytest.mark.asyncio
async def test_gated_mixer_fades_in_then_out_instead_of_cutting():
    mixer = await _mixer(fade_secs=0.1)  # 5 chunks of fade
    mixer.set_target(True)
    gains = []
    for _ in range(6):
        await mixer.mix(CHUNK)
        gains.append(round(mixer.gain, 2))
    assert gains == [0.2, 0.4, 0.6, 0.8, 1.0, 1.0]
    assert await _level(mixer) > 100
    mixer.set_target(False)
    await mixer.mix(CHUNK)
    assert 0 < mixer.gain < 1  # fading, not cut
    await _level(mixer)
    assert mixer.gain == 0 and await _level(mixer) == 0


@pytest.mark.asyncio
async def test_gated_mixer_at_full_gain_matches_the_whole_call_mixer():
    gated = await _mixer(fade_secs=0.0, volume=0.3)
    gated.set_target(True)
    plain = SoundfileMixer(
        sound_files={"office": APP_ROOT_DIR / "assets" / f"office-ambience-{RATE}-mono.wav"},
        default_sound="office",
        volume=0.3,
    )
    await plain.start(RATE)
    for _ in range(5):
        assert np.allclose(
            np.frombuffer(await gated.mix(CHUNK), np.int16),
            np.frombuffer(await plain.mix(CHUNK), np.int16),
            atol=1,
        )


# ── Only during tool calls ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_calls_mode_plays_only_while_a_tool_runs():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, UserStoppedSpeakingFrame())  # caller finished, no tool yet
    await _settle()
    assert not gate.playing

    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    assert not gate.playing  # waits out the start delay
    await _settle()
    assert gate.playing and await _level(mixer) > 0

    await _push(gate, _result())
    await _push(gate, BotStartedSpeakingFrame())
    assert not gate.playing


@pytest.mark.asyncio
async def test_a_tool_faster_than_the_start_delay_stays_silent():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _push(gate, _result())
    await _settle()
    assert not gate.playing and await _level(mixer) == 0


@pytest.mark.asyncio
async def test_sound_pauses_while_the_bot_says_one_moment_then_resumes():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle()
    assert gate.playing
    await _push(gate, BotStartedSpeakingFrame())  # "ek minute, check kar rahi hoon"
    assert not gate.playing
    await _push(gate, BotStoppedSpeakingFrame())  # tool still running
    await _settle()
    assert gate.playing


@pytest.mark.asyncio
async def test_caller_interrupting_stops_the_sound():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle()
    await _push(gate, UserStartedSpeakingFrame())
    assert not gate.playing
    await _push(gate, FunctionCallCancelFrame(function_name="t", tool_call_id="c1"))
    await _push(gate, UserStoppedSpeakingFrame())
    await _settle()
    assert not gate.playing  # the cancelled tool no longer counts


@pytest.mark.asyncio
async def test_end_call_transfer_and_node_change_tools_are_ignored():
    mixer = await _mixer()
    gate = AmbientSoundGate(
        mixer,
        mode=PLAY_TOOL_CALLS,
        start_delay_secs=DELAY,
        is_ignored_tool=lambda name: name in {"end_call", "transfer_to_sales"},
    )
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call("end_call", "e1")]))
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call("transfer_to_sales", "t1")]))
    await _settle()
    assert not gate.playing


@pytest.mark.asyncio
async def test_progress_updates_from_an_async_tool_keep_the_sound_going():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle()
    await _push(gate, _result(is_final=False))
    assert gate.playing
    await _push(gate, _result(is_final=True))
    assert not gate.playing


@pytest.mark.asyncio
async def test_parallel_tools_play_until_the_last_one_finishes():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call(tool_call_id="a"), _call(tool_call_id="b")]))
    await _settle()
    await _push(gate, _result("a"))
    assert gate.playing
    await _push(gate, _result("b"))
    assert not gate.playing


# ── While the bot is thinking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_thinking_mode_plays_from_the_callers_turn_end_until_the_bot_speaks():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=DELAY)
    await _push(gate, UserStartedSpeakingFrame())
    await _settle()
    assert not gate.playing  # never over the caller
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, LLMFullResponseStartFrame())  # the bot is working on a reply
    await _settle()
    assert gate.playing
    await _push(gate, BotStartedSpeakingFrame())
    assert not gate.playing
    await _push(gate, BotStoppedSpeakingFrame())  # bot done, waiting on the caller
    await _settle()
    assert not gate.playing


@pytest.mark.asyncio
async def test_a_quick_reply_in_thinking_mode_stays_silent():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=DELAY)
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, BotStartedSpeakingFrame())
    await _settle()
    assert not gate.playing and await _level(mixer) == 0


@pytest.mark.asyncio
async def test_a_wait_the_bot_never_ends_stops_at_the_limit():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=0.0, max_wait_secs=0.1)
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, TTSStartedFrame())  # a reply that then never reaches the caller
    await _settle(0.02)
    assert gate.playing
    await _settle(0.15)
    assert not gate.playing


@pytest.mark.asyncio
async def test_sound_stops_when_the_call_ends():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=0.0)
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, LLMFullResponseStartFrame())
    await _settle(0.02)
    await _push(gate, EndFrame())
    assert not gate.playing


@pytest.mark.asyncio
async def test_each_frame_counts_once_though_observers_see_it_at_every_hop():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    started = FunctionCallsStartedFrame(function_calls=[_call()])
    stopped = BotStoppedSpeakingFrame()
    await _push(gate, BotStartedSpeakingFrame())
    for _ in range(3):
        await _push(gate, started)
        await _push(gate, stopped)
    await _settle()
    assert gate.playing
    await _push(gate, _result())
    assert not gate.playing


@pytest.mark.asyncio
async def test_a_turn_the_bot_never_answers_stays_silent():
    # A cough on a voice-activity agent ends a turn with no words; the bot
    # doesn't answer it, so nothing should play.
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=DELAY)
    await _push(gate, UserStoppedSpeakingFrame())
    await _settle(DELAY * 3)
    assert not gate.playing and await _level(mixer) == 0


@pytest.mark.asyncio
async def test_the_start_delay_counts_from_the_end_of_the_callers_turn():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=DELAY)
    await _push(gate, UserStoppedSpeakingFrame())
    await _settle(DELAY + 0.02)  # LLM slow to start
    await _push(gate, LLMFullResponseStartFrame())
    await _settle(0.01)
    assert gate.playing  # no second delay once the reply is under way


@pytest.mark.asyncio
async def test_in_tool_mode_the_delay_counts_from_the_tool_starting():
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_TOOL_CALLS, start_delay_secs=DELAY)
    await _push(gate, UserStoppedSpeakingFrame())
    await _settle(DELAY + 0.02)  # the LLM took a while to call the tool
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle(0.01)
    assert not gate.playing  # a fast tool still gets its full delay
    await _settle()
    assert gate.playing


@pytest.mark.asyncio
async def test_a_reply_that_ends_without_speech_or_a_tool_stops_the_sound(monkeypatch):
    monkeypatch.setattr("api.services.pipecat.ambient_sound.ABANDONED_REPLY_SECS", 0.05)
    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=0.0)
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, LLMFullResponseStartFrame())
    await _settle(0.02)
    assert gate.playing
    await _push(gate, LLMFullResponseEndFrame())  # nothing to say, no tool
    await _settle(0.1)
    assert not gate.playing


# ── Building and attaching ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [8000, 16000])
async def test_mixer_choice_follows_the_play_mode_and_sound(rate):
    assert isinstance(await build_audio_out_mixer(rate, {"enabled": False}), SilenceAudioMixer)

    whole = await build_audio_out_mixer(rate, {"enabled": True})
    assert type(whole) is SoundfileMixer
    assert whole._current_sound == "office"

    typing = await build_audio_out_mixer(rate, {"enabled": True, "play_mode": "tool_calls", "sound": "typing"})
    assert isinstance(typing, GatedSoundfileMixer)
    await typing.start(rate)
    assert "typing" in typing._sounds  # the asset exists at this rate

    unknown = await build_audio_out_mixer(rate, {"enabled": True, "sound": "jungle"})
    assert unknown._current_sound == "office"


def test_attaching_does_nothing_for_whole_call_sound():
    added = []
    task = SimpleNamespace(add_observer=added.append)
    whole = SimpleNamespace(_params=SimpleNamespace(audio_out_mixer=SoundfileMixer(sound_files={}, default_sound="x")))
    attach_ambient_sound_gate(task, whole, {"enabled": True})
    assert added == []


@pytest.mark.asyncio
async def test_attaching_follows_a_gated_mixer_and_skips_node_transition_tools():
    added = []
    task = SimpleNamespace(add_observer=added.append)
    mixer = await _mixer()
    transport = SimpleNamespace(_params=SimpleNamespace(audio_out_mixer=mixer))
    llm = SimpleNamespace(
        _functions={
            "end_call": SimpleNamespace(is_node_transition=True),
            "vectus_area_manager_lookup": SimpleNamespace(is_node_transition=False),
        }
    )
    attach_ambient_sound_gate(task, transport, {"play_mode": "tool_calls", "start_delay_secs": 0.0}, llm=llm)
    [gate] = added
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call("end_call", "e1")]))
    await _settle(0.02)
    assert not gate.playing
    await _push(gate, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle(0.02)
    assert gate.playing


def test_schema_defaults_keep_existing_agents_on_whole_call():
    cfg = AmbientNoiseConfigurationDefaults()
    assert (cfg.play_mode, cfg.start_delay_secs, cfg.sound) == ("whole_call", 0.5, "office")
    # Saved configs from before this change still load.
    assert AmbientNoiseConfigurationDefaults(enabled=True, volume=0.2).play_mode == "whole_call"
    with pytest.raises(ValidationError):
        AmbientNoiseConfigurationDefaults(play_mode="sometimes")
    with pytest.raises(ValidationError):
        AmbientNoiseConfigurationDefaults(start_delay_secs=9)


# ── A long call ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_long_call_keeps_memory_bounded_and_state_consistent():
    """~30 minutes of turns and tool calls: the frame-id memory stays capped,
    and after each phase the gate is in the state that phase implies."""
    import tracemalloc

    mixer = await _mixer()
    gate = AmbientSoundGate(mixer, mode=PLAY_THINKING, start_delay_secs=0.0)
    tracemalloc.start()
    baseline = None
    for turn in range(3000):  # a turn every ~0.6 s for 30 minutes
        await _push(gate, BotStoppedSpeakingFrame())
        await _push(gate, UserStartedSpeakingFrame())
        assert not gate.playing
        await _push(gate, UserStoppedSpeakingFrame())
        await _push(gate, LLMFullResponseStartFrame())
        if turn % 5 == 0:
            await _push(gate, FunctionCallsStartedFrame(function_calls=[_call(tool_call_id=f"t{turn}")]))
            await _push(gate, _result(f"t{turn}"))
        await _push(gate, LLMFullResponseEndFrame())
        await _push(gate, BotStartedSpeakingFrame())
        assert not gate.playing and not gate._tool_ids
        for _ in range(3):
            await mixer.mix(CHUNK)
        if turn == 500:
            baseline = tracemalloc.get_traced_memory()[0]
    grown = tracemalloc.get_traced_memory()[0] - baseline
    tracemalloc.stop()
    assert len(gate._seen) <= 10000
    assert grown < 2_000_000, f"memory grew by {grown} bytes over the call"
    # And it still works at the end of the call.
    await _push(gate, BotStoppedSpeakingFrame())
    await _push(gate, UserStoppedSpeakingFrame())
    await _push(gate, LLMFullResponseStartFrame())
    await _settle(0.02)
    assert gate.playing


@pytest.mark.asyncio
async def test_two_calls_at_once_do_not_affect_each_other():
    call_a, call_b = await _mixer(), await _mixer()
    gate_a = AmbientSoundGate(call_a, mode=PLAY_TOOL_CALLS, start_delay_secs=0.0)
    AmbientSoundGate(call_b, mode=PLAY_TOOL_CALLS, start_delay_secs=0.0)
    await _push(gate_a, FunctionCallsStartedFrame(function_calls=[_call()]))
    await _settle(0.02)
    assert await _level(call_a) > 0
    assert await _level(call_b) == 0

