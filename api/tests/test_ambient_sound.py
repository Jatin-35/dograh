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
    LIVELY_SECS,
    normalize_ambient_config,
    PLAY_THINKING,
    PLAY_TOOL_CALLS,
    AmbientSoundGate,
    GatedSoundfileMixer,
    _cached_lively_start_positions,
    attach_ambient_sound_gate,
    lively_start_positions,
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
    # The gated mixer skips to a lively part of the file when it starts;
    # compare the two from that same point on.
    first = await gated.mix(CHUNK)
    plain._sound_pos = gated._sound_pos - len(CHUNK) // 2
    assert np.allclose(np.frombuffer(first, np.int16), np.frombuffer(await plain.mix(CHUNK), np.int16), atol=1)
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
    assert AmbientNoiseConfigurationDefaults(volume=1.5).volume == 1.5
    assert AmbientNoiseConfigurationDefaults(volume=0.1).volume == 0.1
    # Agents saved before the range existed must still save: brought into range.
    for stored, saved in ((0, 0.1), (0.05, 0.1), (1.6, 1.5), (3, 1.5)):
        assert AmbientNoiseConfigurationDefaults(volume=stored).volume == saved
    with pytest.raises(ValidationError):
        AmbientNoiseConfigurationDefaults(volume="loud")


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


# ── Every wait lands on a lively part of the sound ────────────────────


def _db(level: float) -> float:
    return 20 * np.log10(max(level, 1e-9) / 32768)


async def _burst_levels(mixer, bursts=60, burst_secs=1.0):
    """Play ``bursts`` short waits; return the loudness of each (after fade-in)."""
    levels = []
    for _ in range(bursts):
        mixer.set_target(True)
        chunks = [await mixer.mix(CHUNK) for _ in range(int(burst_secs / 0.02))]
        levels.append(_rms(b"".join(chunks[8:])))  # skip the fade-in
        mixer.set_target(False)
        for _ in range(10):  # fade out
            await mixer.mix(CHUNK)
        for _ in range(int(np.random.default_rng(len(levels)).uniform(1, 40))):
            await mixer.mix(CHUNK)  # silence between waits (position doesn't move)
    return levels


@pytest.mark.asyncio
@pytest.mark.parametrize("sound", ["office", "typing"])
async def test_no_wait_plays_a_silent_or_quiet_stretch(sound):
    # Before the fix a short wait could land on a pause in the typing (heard
    # nothing) or a dip in the office ambience (10 dB quieter than another call).
    files = {"office": "office-ambience-8000-mono.wav", "typing": "typing-8000-mono.wav"}
    mixer = GatedSoundfileMixer(
        sound_files={sound: APP_ROOT_DIR / "assets" / files[sound]}, default_sound=sound, volume=1.0, fade_secs=0.15
    )
    await mixer.start(RATE)
    levels = [_db(x) for x in await _burst_levels(mixer)]
    assert min(levels) > -60, "a wait played (near) silence"
    assert np.median(levels) - min(levels) < 8, f"waits vary too much: {min(levels):.1f}..{max(levels):.1f} dBFS"


@pytest.mark.asyncio
async def test_seeking_skips_a_long_pause_in_an_uploaded_file(tmp_path):
    import soundfile as sf

    rng = np.random.default_rng(1)
    noise = (rng.standard_normal(RATE * 3) * 2000).astype(np.int16)
    path = tmp_path / "pausey.wav"  # 4 s of silence, then 3 s of sound
    sf.write(path, np.concatenate([np.zeros(RATE * 4, np.int16), noise]), RATE, subtype="PCM_16")
    mixer = GatedSoundfileMixer(sound_files={"custom": path}, default_sound="custom", volume=1.0, fade_secs=0.02)
    await mixer.start(RATE)
    mixer.set_target(True)
    first = [await mixer.mix(CHUNK) for _ in range(10)]
    assert all(_rms(c) > 100 for c in first[1:]), "started inside the silent stretch"


def test_lively_positions_handle_short_and_silent_files():
    assert len(lively_start_positions(np.zeros(RATE * 5, np.int16), RATE)) == 0
    assert len(lively_start_positions(np.ones(int(RATE * LIVELY_SECS / 2), np.int16), RATE)) == 0


@pytest.mark.parametrize("rate", [8000, 16000])
def test_typing_sound_never_pauses_long_and_does_not_clip(rate):
    import soundfile as sf

    audio, sr = sf.read(APP_ROOT_DIR / "assets" / f"typing-{rate}-mono.wav", dtype="int16")
    silent = np.abs(audio) < 1
    longest, run = 0, 0
    for quiet in silent:
        run = run + 1 if quiet else 0
        longest = max(longest, run)
    assert longest / sr < 0.5
    assert np.abs(audio).max() < 16000  # headroom under the voice, no clipping
    half = sr // 2
    per_half_second = [_db(_rms(audio[i : i + half].tobytes())) for i in range(0, len(audio) - half + 1, half)]
    assert max(per_half_second) - min(per_half_second) < 10  # steady, no dips


# ── Deep checks on the lively-start fix ───────────────────────────────


async def _sound_mixer(sound, rate=RATE, volume=1.0, fade_secs=0.15):
    files = {"office": f"office-ambience-{rate}-mono.wav", "typing": f"typing-{rate}-mono.wav"}
    mixer = GatedSoundfileMixer(
        sound_files={sound: APP_ROOT_DIR / "assets" / files[sound]},
        default_sound=sound,
        volume=volume,
        fade_secs=fade_secs,
    )
    await mixer.start(rate)
    return mixer


@pytest.mark.asyncio
@pytest.mark.parametrize("sound", ["office", "typing"])
@pytest.mark.parametrize("rate", [8000, 16000])
async def test_random_waits_are_never_silent(sound, rate):
    """300 waits of random length (0.3-5 s) with random gaps between them."""
    mixer = await _sound_mixer(sound, rate)
    chunk = b"\x00" * int(rate * 0.02) * 2
    rng = np.random.default_rng(7)
    for _ in range(300):
        secs = float(rng.choice([rng.uniform(0.3, 0.8), rng.uniform(0.8, 2.0), rng.uniform(2.0, 5.0)]))
        mixer.set_target(True)
        wait = [await mixer.mix(chunk) for _ in range(int(secs / 0.02))]
        assert _db(_rms(b"".join(wait))) > -58, f"a {secs:.2f}s wait was (near) silent"
        mixer.set_target(False)
        for _ in range(int(rng.integers(8, 60))):
            await mixer.mix(chunk)


@pytest.mark.asyncio
async def test_seeking_wraps_around_the_end_of_the_file():
    mixer = await _sound_mixer("typing")
    starts = mixer._lively["typing"]
    mixer._sound_pos = int(starts[-1]) + 1  # past the last lively start
    mixer.set_target(True)
    await mixer.mix(CHUNK)
    assert mixer._sound_pos == int(starts[0]) + len(CHUNK) // 2


@pytest.mark.asyncio
async def test_no_jump_when_the_sound_comes_back_before_it_faded_out():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    for _ in range(20):
        await mixer.mix(CHUNK)
    mixer.set_target(False)
    await mixer.mix(CHUNK)  # fading out, not yet silent
    pos = mixer._sound_pos
    mixer.set_target(True)  # back on mid-fade: carry on, don't seek
    await mixer.mix(CHUNK)
    assert mixer._sound_pos == pos + len(CHUNK) // 2


@pytest.mark.asyncio
async def test_the_jump_happens_in_silence_so_it_never_clicks():
    mixer = await _sound_mixer("typing", fade_secs=0.15)
    peak = np.abs(mixer._sounds["typing"]).max()
    mixer.set_target(True)
    first = np.abs(np.frombuffer(await mixer.mix(CHUNK), np.int16)).max()
    assert first <= peak * (0.02 / 0.15) + 1  # first chunk is at the first fade step


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [8000, 16000])
async def test_volume_extremes_neither_clip_nor_vanish(rate):
    loud = await _sound_mixer("typing", rate, volume=1.5, fade_secs=0.0)
    loud.set_target(True)
    chunk = b"\x00" * int(rate * 0.02) * 2
    heard = np.concatenate([np.frombuffer(await loud.mix(chunk), np.int16) for _ in range(250)])
    assert np.abs(heard).max() < 32767  # 1.5x the loudest keystroke still fits
    quiet = await _sound_mixer("typing", rate, volume=0.1, fade_secs=0.0)
    quiet.set_target(True)
    heard = b"".join([await quiet.mix(chunk) for _ in range(250)])
    assert _db(_rms(heard)) > -60  # quiet, but there


@pytest.mark.asyncio
async def test_lively_parts_are_found_once_per_file_and_never_mixed_up(tmp_path):
    import soundfile as sf

    a = await _sound_mixer("typing")
    b = await _sound_mixer("typing")
    assert a._lively["typing"] is b._lively["typing"]  # second call reuses the first's work
    office = await _sound_mixer("office")
    assert office._lively["office"] is not a._lively["typing"]
    # Same audio under a different path (a new upload) gets its own entry.
    path = tmp_path / "copy.wav"
    sf.write(path, a._sounds["typing"], RATE, subtype="PCM_16")
    copy = _cached_lively_start_positions(str(path), a._sounds["typing"], RATE)
    assert copy is not a._lively["typing"] and np.array_equal(copy, a._lively["typing"])


# ── Stored settings that were never validated ─────────────────────────


@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, {"play_mode": "whole_call", "sound": "office", "volume": 0.3, "start_delay_secs": 0.5}),
        ({"play_mode": None, "start_delay_secs": None, "volume": None, "sound": None},
         {"play_mode": "whole_call", "sound": "office", "volume": 0.3, "start_delay_secs": 0.5}),
        ({"play_mode": "sometimes", "sound": "jungle", "volume": "loud", "start_delay_secs": "soon"},
         {"play_mode": "whole_call", "sound": "office", "volume": 0.3, "start_delay_secs": 0.5}),
        ({"play_mode": "tool_calls", "sound": "typing", "volume": 9, "start_delay_secs": -2},
         {"play_mode": "tool_calls", "sound": "typing", "volume": 1.5, "start_delay_secs": 0.0}),
        ({"volume": float("nan"), "start_delay_secs": 99}, {"volume": 0.3, "start_delay_secs": 5.0}),
    ],
    ids=["missing", "nulls", "garbage", "out-of-range", "nan-and-huge"],
)
def test_stored_settings_are_made_safe_for_the_call(stored, expected):
    clean = normalize_ambient_config(stored)
    assert {k: clean[k] for k in expected} == expected


@pytest.mark.asyncio
async def test_a_call_with_broken_stored_settings_still_starts():
    broken = {"enabled": True, "play_mode": "tool_calls", "start_delay_secs": None, "volume": None, "sound": None}
    mixer = await build_audio_out_mixer(RATE, broken)  # used to raise on volume=None
    assert isinstance(mixer, GatedSoundfileMixer)
    added = []
    task = SimpleNamespace(add_observer=added.append)
    attach_ambient_sound_gate(task, SimpleNamespace(_params=SimpleNamespace(audio_out_mixer=mixer)), broken)
    [gate] = added  # used to raise on float(None)
    assert gate._start_delay == 0.5 and gate._mode == "tool_calls"


@pytest.mark.asyncio
async def test_audio_frames_are_ignored_without_bookkeeping():
    from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

    gate = AmbientSoundGate(await _mixer(), mode=PLAY_TOOL_CALLS)
    for _ in range(500):
        await _push(gate, InputAudioRawFrame(audio=CHUNK, sample_rate=RATE, num_channels=1))
        await _push(gate, OutputAudioRawFrame(audio=CHUNK, sample_rate=RATE, num_channels=1))
    assert len(gate._seen) == 0


# ── The bot's voice cuts the sound at once ────────────────────────────


def _voice_chunk(rate=RATE, amplitude=4000):
    t = np.arange(int(rate * 0.02)) / rate
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.int16).tobytes()


def _added(out: bytes, src: bytes) -> np.ndarray:
    """What the mixer added on top of the source audio."""
    return np.frombuffer(out, np.int16).astype(np.int32) - np.frombuffer(src, np.int16).astype(np.int32)


@pytest.mark.asyncio
async def test_the_bots_first_word_cuts_the_sound_within_10_ms():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    for _ in range(20):  # sound fully up
        await mixer.mix(CHUNK)
    assert mixer.gain == 1.0
    voice = _voice_chunk()
    added = _added(await mixer.mix(voice), voice)
    cut = int(RATE * 0.01)
    assert np.any(added[:cut] != 0)  # the short ramp down
    assert not np.any(added[cut:]), "sound still under the bot's first word"
    for _ in range(10):  # the rest of the word
        assert not np.any(_added(await mixer.mix(voice), voice))


@pytest.mark.asyncio
async def test_a_pause_between_the_bots_words_keeps_the_sound_off():
    mixer = await _sound_mixer("typing")
    mixer.set_target(True)  # the gate hasn't caught up yet
    for _ in range(20):
        await mixer.mix(CHUNK)
    voice = _voice_chunk()
    await mixer.mix(voice)
    for _ in range(int(0.4 / 0.02)):  # a 0.4 s pause between words
        assert await mixer.mix(CHUNK) == CHUNK
    await mixer.mix(voice)  # the next word resets the hold
    for _ in range(int(0.4 / 0.02)):
        assert await mixer.mix(CHUNK) == CHUNK


@pytest.mark.asyncio
async def test_the_sound_returns_after_the_hold_if_the_wait_goes_on():
    # e.g. "ek minute, check kar rahi hoon" while a tool keeps running
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    await mixer.mix(_voice_chunk())
    held = [await mixer.mix(CHUNK) for _ in range(int(0.5 / 0.02))]
    assert all(c == CHUNK for c in held)
    after = [await mixer.mix(CHUNK) for _ in range(20)]
    assert any(c != CHUNK for c in after)
    assert mixer.gain > 0.9  # faded back in, not jumped


@pytest.mark.asyncio
async def test_a_voice_during_the_fade_in_cuts_it_too():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    for _ in range(3):  # partway into the fade-in
        await mixer.mix(CHUNK)
    assert 0 < mixer.gain < 1
    voice = _voice_chunk()
    added = _added(await mixer.mix(voice), voice)
    assert not np.any(added[int(RATE * 0.01):])
    assert mixer.gain == 0.0


@pytest.mark.asyncio
async def test_quiet_rounding_noise_is_not_mistaken_for_the_voice():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    noise = (np.random.default_rng(1).integers(-2, 3, int(RATE * 0.02))).astype(np.int16).tobytes()
    for _ in range(20):
        await mixer.mix(noise)
    assert mixer.gain == 1.0

# ── Voice detection edge cases ────────────────────────────────────────


def _tone(rate, secs, amplitude, freq=220.0):
    t = np.arange(int(rate * secs)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [8000, 16000])
@pytest.mark.parametrize("chunk_ms", [10, 20, 40, 33])
async def test_voice_cut_and_hold_work_for_any_chunk_size(rate, chunk_ms):
    """Transports send 10-40 ms chunks (33 ms checks an odd size)."""
    mixer = await _sound_mixer("office", rate)
    silence = b"\x00" * (int(rate * chunk_ms / 1000) * 2)
    voice = _tone(rate, chunk_ms / 1000, 4000).tobytes()
    mixer.set_target(True)
    for _ in range(int(0.4 / (chunk_ms / 1000)) + 1):
        await mixer.mix(silence)
    assert mixer.gain == 1.0
    out = await mixer.mix(voice)
    added = _added(out, voice)
    assert not np.any(added[int(rate * 0.01):])
    # Held off for 0.5 s of silence whatever the chunk size, then back.
    held = int(0.5 / (chunk_ms / 1000))
    for _ in range(held - 1):
        assert await mixer.mix(silence) == silence
    back = [await mixer.mix(silence) for _ in range(int(0.4 / (chunk_ms / 1000)) + 2)]
    assert any(chunk != silence for chunk in back)


@pytest.mark.asyncio
async def test_a_soft_voice_still_cuts_the_sound():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    for _ in range(20):
        await mixer.mix(CHUNK)
    soft = _tone(RATE, 0.02, 400).tobytes()  # about -38 dBFS peak, far below normal TTS
    assert not np.any(_added(await mixer.mix(soft), soft)[int(RATE * 0.01):])


@pytest.mark.asyncio
async def test_a_voice_starting_late_in_a_chunk_still_cuts_it():
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    for _ in range(20):
        await mixer.mix(CHUNK)
    late = np.zeros(int(RATE * 0.02), np.int16)
    late[-40:] = 4000  # the word begins in the last 5 ms of the chunk
    src = late.tobytes()
    added = _added(await mixer.mix(src), src)
    assert not np.any(added[int(RATE * 0.01):])  # cut before the word, not after


@pytest.mark.asyncio
async def test_faint_hiss_between_words_does_not_count_as_voice():
    # Some TTS services send low-level noise instead of digital silence.
    mixer = await _sound_mixer("office")
    mixer.set_target(True)
    hiss = (np.random.default_rng(2).normal(0, 60, int(RATE * 0.02))).clip(-250, 250).astype(np.int16).tobytes()
    for _ in range(30):
        out = await mixer.mix(hiss)
    assert mixer.gain == 1.0 and np.any(_added(out, hiss))  # still plays over hiss


@pytest.mark.asyncio
async def test_hold_ends_exactly_after_half_a_second_of_silence():
    mixer = await _sound_mixer("office", fade_secs=0.02)
    mixer.set_target(True)
    await mixer.mix(_voice_chunk())
    for _ in range(int(0.5 / 0.02) - 1):  # 0.48 s: still held
        assert await mixer.mix(CHUNK) == CHUNK
    await mixer.mix(CHUNK)  # the 0.5 s mark
    assert await mixer.mix(CHUNK) != CHUNK  # sound is back right after


@pytest.mark.asyncio
async def test_transfer_hold_music_or_a_recorded_greeting_also_keeps_the_sound_off():
    # Anything the bot plays (TTS, a recorded greeting, transfer hold music)
    # reaches the mixer as outgoing audio and must not have ambient under it.
    mixer = await _sound_mixer("typing")
    mixer.set_target(True)
    music = _tone(RATE, 3.0, 3000, freq=440)
    step = int(RATE * 0.02)
    added = [
        _added(await mixer.mix(music[i : i + step].tobytes()), music[i : i + step].tobytes())
        for i in range(0, len(music) - step + 1, step)
    ]
    assert not any(np.any(a) for a in added[1:])


@pytest.mark.asyncio
async def test_whole_call_mode_still_plays_under_the_voice():
    whole = await build_audio_out_mixer(RATE, {"enabled": True, "play_mode": "whole_call", "sound": "office", "volume": 1.0})
    await whole.start(RATE)
    assert type(whole) is not GatedSoundfileMixer
    voice = _voice_chunk()
    added = [_added(await whole.mix(voice), voice) for _ in range(50)]
    assert any(np.any(a) for a in added)


def test_voice_detection_costs_almost_nothing():
    import time

    async def run():
        mixer = await _sound_mixer("office")
        mixer.set_target(True)
        voice, n = _voice_chunk(), 20000
        start = time.perf_counter()
        for i in range(n):
            await mixer.mix(voice if i % 3 else CHUNK)
        return (time.perf_counter() - start) / n

    per_chunk = asyncio.run(run())
    assert per_chunk < 0.0002, f"{per_chunk * 1e6:.0f} us per 20 ms chunk"  # < 1% of real time

