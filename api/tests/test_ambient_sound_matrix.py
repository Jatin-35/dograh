"""Every play mode x sound x sample rate x start delay through one scripted call.

The script holds each phase of a call that matters: greeting, the caller's
turns, a quick reply, a slow reply, a tool call, waiting for the caller, and
hanging up. Each combination runs it through the real mixer (as the call's
transport builds it) and the real gate, with the bot's voice as digital
silence, so whatever the caller hears is the ambient sound. Every phase is
then checked for sound where the mode should play and silence everywhere
else. Combinations run concurrently, in real time, and phases are matched by
wall-clock time.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest
import soundfile as sf
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from api.services.pipecat.ambient_sound import AmbientSoundGate, GatedSoundfileMixer
from api.services.pipecat.audio_mixer import build_audio_out_mixer

MODES = ("whole_call", "thinking", "tool_calls")
SOUNDS = ("office", "typing", "custom")
RATES = (8000, 16000)
DELAYS = (0.0, 0.3, 1.0)
# The fade runs on the audio clock (one step per mixed 20 ms chunk), so its
# allowance is counted in chunks: 0.15 s = 7.5 chunks, plus margin. Many
# combinations share one event loop, so chunks can lag wall time a little.
FADE_CHUNKS = 10
HEARD = 0.5  # RMS above which a window carries sound (the voice is exact silence)
# Typing has natural gaps between keystrokes of up to ~0.3 s, so "sound"
# is only judged over windows long enough to hold a keystroke.
MIN_SOUND_WINDOW = 0.35
# ...and over at least this much mixed audio: under load fewer chunks are
# mixed per wall-clock second, and a short stretch can fall between keys.
MIN_SOUND_CHUNKS = int(0.45 / 0.02)


def _call():
    return FunctionCallFromLLM(function_name="vectus_area_manager_lookup", tool_call_id="t1", arguments={}, context=None)


def _script():
    call = _call()
    # (phase, seconds, bot speaking, frames fired as the phase starts)
    return [
        ("greeting", 1.2, True, [BotStartedSpeakingFrame()]),
        ("caller_1", 0.8, False, [BotStoppedSpeakingFrame(), UserStartedSpeakingFrame()]),
        ("quick_reply_gap", 0.2, False, [UserStoppedSpeakingFrame(), LLMFullResponseStartFrame()]),
        ("answer_1", 0.8, True, [LLMFullResponseEndFrame(), BotStartedSpeakingFrame()]),
        ("caller_2", 0.8, False, [BotStoppedSpeakingFrame(), UserStartedSpeakingFrame()]),
        ("slow_reply_gap", 2.0, False, [UserStoppedSpeakingFrame(), LLMFullResponseStartFrame()]),
        ("answer_2", 0.8, True, [LLMFullResponseEndFrame(), BotStartedSpeakingFrame()]),
        ("caller_3", 0.8, False, [BotStoppedSpeakingFrame(), UserStartedSpeakingFrame()]),
        ("before_tool", 0.3, False, [UserStoppedSpeakingFrame(), LLMFullResponseStartFrame()]),
        ("tool", 2.2, False, [LLMFullResponseEndFrame(), FunctionCallsStartedFrame(function_calls=[call])]),
        (
            "answer_3",
            0.8,
            True,
            [
                FunctionCallResultFrame(function_name=call.function_name, tool_call_id="t1", arguments={}, result={}),
                LLMFullResponseStartFrame(),
                BotStartedSpeakingFrame(),
            ],
        ),
        ("waiting_for_caller", 1.0, False, [BotStoppedSpeakingFrame(), LLMFullResponseEndFrame()]),
        ("hung_up", 0.6, False, [EndFrame()]),
    ]


def _expectations(mode: str, delay: float, sound: str) -> dict[str, tuple[str, float]]:
    """Phase -> ("sound", from-offset) | ("silent", 0) | ("any", 0)."""
    silent = {p: ("silent", 0.0) for p, *_ in _script()}
    if mode == "whole_call":
        if sound == "custom":
            # Whole-call plays an upload verbatim, its own silences included.
            return {p: ("any", 0.0) for p in silent} | {"greeting": ("sound", 0.1)}
        return {p: ("sound", 0.1) for p in silent} | {"hung_up": ("any", 0.0)}
    on = delay + 0.25  # delay, then most of the fade-in
    if mode == "thinking":
        exp = silent | {
            "quick_reply_gap": ("any", 0.0) if delay < 0.2 else ("silent", 0.0),
            "slow_reply_gap": ("sound", on),
            "before_tool": ("any", 0.0),
            # the wait started at the caller's turn end, 0.3 s before the tool
            "tool": ("sound", max(0.1, on - 0.3)),
        }
        return exp
    return silent | {"tool": ("sound", on)}  # tool_calls


async def _run(mode, sound, rate, delay, custom_path, voice=False):
    cfg = {"enabled": True, "volume": 1.0, "play_mode": mode, "start_delay_secs": delay, "sound": sound}
    if sound == "custom":
        cfg |= {"sound": "office", "storage_key": "ambient/upload.wav", "storage_backend": "minio"}
        with patch(
            "api.services.pipecat.audio_mixer.get_cached_ambient_noise_path", AsyncMock(return_value=custom_path)
        ):
            mixer = await build_audio_out_mixer(rate, cfg)
    else:
        mixer = await build_audio_out_mixer(rate, cfg)
    await mixer.start(rate)
    gate = AmbientSoundGate(mixer, mode=mode, start_delay_secs=delay) if isinstance(mixer, GatedSoundfileMixer) else None
    assert (gate is None) == (mode == "whole_call")

    loop = asyncio.get_running_loop()
    chunk = b"\x00" * int(rate * 0.02) * 2
    t = np.arange(int(rate * 0.02)) / rate
    voice_chunk = (4000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16).tobytes()
    heard: list[tuple[float, float]] = []  # (time, RMS of what the mixer added)
    phases: list[tuple[str, float, float, bool]] = []
    for name, secs, speaking, frames in _script():
        start = loop.time()
        for frame in frames:
            if gate:
                await gate.on_push_frame(SimpleNamespace(frame=frame))
        end = start + secs
        src = voice_chunk if (voice and speaking) else chunk
        while loop.time() < end:
            out = np.frombuffer(await mixer.mix(src), np.int16).astype(np.float64)
            added = out - np.frombuffer(src, np.int16)
            heard.append((loop.time(), float(np.sqrt((added**2).mean()))))
            await asyncio.sleep(0.02)
        phases.append((name, start, end, speaking))

    problems = []
    for name, start, end, speaking in phases:
        kind, offset = _expectations(mode, delay, sound)[name]
        if voice and speaking and mode != "whole_call":
            # Under an audible voice the sound must be gone from the first
            # chunk on (that chunk holds only a 10 ms ramp), with no fade tail.
            under = [lv for t, lv in heard if start <= t < end][1:]
            if any(lv > 0 for lv in under):
                problems.append(f"{name}: sound under the bot's voice ({max(under):.0f})")
            continue
        if kind == "any":
            continue
        hi = end - 0.05
        if kind == "sound":
            lo = start + offset
            if hi - lo < MIN_SOUND_WINDOW:
                continue  # too short to judge after the delay
            levels = [lv for t, lv in heard if lo <= t < hi]
            if len(levels) < MIN_SOUND_CHUNKS:
                continue
        else:
            levels = [lv for t, lv in heard if start <= t < hi][FADE_CHUNKS:]
        if len(levels) < 3:
            continue
        rms = float(np.sqrt(np.mean(np.square(levels))))
        if kind == "sound" and rms <= HEARD:
            problems.append(f"{name}: expected sound, heard nothing")
        if kind == "silent" and max(levels) > HEARD:
            problems.append(f"{name}: expected silence, heard {max(levels):.0f}")
    return f"{mode}/{sound}/{rate}/delay={delay}/voice={voice}", problems


@pytest.fixture(scope="module")
def uploads(tmp_path_factory):
    """An uploaded file with a long silent stretch in the middle, per rate."""
    paths = {}
    rng = np.random.default_rng(5)
    for rate in RATES:
        noise = lambda secs: (rng.standard_normal(int(rate * secs)) * 1500).astype(np.int16)  # noqa: E731
        audio = np.concatenate([noise(2.5), np.zeros(rate * 3, np.int16), noise(2.5)])
        path = tmp_path_factory.mktemp("upload") / f"upload_{rate}.wav"
        sf.write(path, audio, rate, subtype="PCM_16")
        paths[rate] = str(path)
    return paths


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True], ids=["silent-voice", "audible-voice"])
@pytest.mark.parametrize("sound", SOUNDS)
@pytest.mark.parametrize("rate", RATES)
async def test_every_mode_and_delay_plays_only_where_it_should(sound, rate, voice, uploads):
    results = await asyncio.gather(
        *[_run(mode, sound, rate, delay, uploads[rate], voice) for mode in MODES for delay in DELAYS]
    )
    failures = [f"{combo}: {p}" for combo, problems in results for p in problems]
    assert not failures, "\n".join(failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("sound", ["office", "typing"])
@pytest.mark.parametrize("rate", RATES)
async def test_volume_scales_the_sound_exactly(sound, rate):
    levels = {}
    for volume in (0.1, 0.5, 1.0, 1.5):
        mixer = await build_audio_out_mixer(
            rate, {"enabled": True, "play_mode": "tool_calls", "sound": sound, "volume": volume}
        )
        await mixer.start(rate)
        mixer._fade_secs = 0.0
        mixer.set_target(True)
        chunk = b"\x00" * int(rate * 0.02) * 2
        out = np.concatenate([np.frombuffer(await mixer.mix(chunk), np.int16) for _ in range(100)]).astype(float)
        levels[volume] = np.sqrt((out**2).mean())
        assert np.abs(out).max() < 32767, f"clipped at volume {volume}"
    for volume in (0.1, 0.5, 1.5):
        assert levels[volume] / levels[1.0] == pytest.approx(volume, rel=0.02)
