"""Ambient sound that plays only at chosen moments of a call.

The ambient noise setting mixes a looping sound file into the audio sent to
the caller. Played for the whole call it can grate, so an agent can instead
play it only while the caller is waiting:

- ``whole_call``: from connect to hangup (the original behaviour).
- ``thinking``: from the end of the caller's turn until the bot speaks,
  including any tool calls in between. It waits for a sign the bot is
  actually replying (the LLM or a tool starting), so a turn the bot doesn't
  answer (a cough on a voice-activity agent) stays silent. Realtime models
  give that sign only as they start speaking, so for them this mode mostly
  fills tool calls.
- ``tool_calls``: only while a tool is running and the bot is quiet.

The sound never plays under the bot's voice or while the caller is talking,
starts only after ``start_delay_secs`` (so a quick reply stays silent), and
fades in and out instead of cutting. Tools that end or transfer the call or
change node are ignored: they are instant, and a transfer plays its own hold
audio.

How it works: the output transport calls the mixer every 20 ms, with the bot's
audio or with silence, so a mixer that scales the sound by a gain can follow
the call exactly. ``AmbientSoundGate`` watches the call's frames and sets that
gain's target directly. pipecat's ``MixerEnableFrame`` would also switch the
mixer, but as a pipeline frame it queues behind audio and text already in
flight and arrives late.

Works the same for cascaded and realtime pipelines: the mixer sits on the
outgoing audio, and both produce the speaking and tool-call frames used here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import numpy as np
from loguru import logger

from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

PLAY_WHOLE_CALL = "whole_call"
PLAY_THINKING = "thinking"
PLAY_TOOL_CALLS = "tool_calls"
PLAY_MODES = (PLAY_WHOLE_CALL, PLAY_THINKING, PLAY_TOOL_CALLS)

DEFAULT_START_DELAY_SECS = 0.5
MAX_START_DELAY_SECS = 5.0
DEFAULT_VOLUME = 0.3
MAX_VOLUME = 1.5
BUILT_IN_SOUND_NAMES = ("office", "typing")
FADE_SECS = 0.15
# The bot's voice cuts the sound the moment it reaches the mixer, with a
# ramp this short (to avoid a click), instead of waiting for the "bot started
# speaking" frame to reach the gate (a chunk or two later) and fading out over
# FADE_SECS, which left the sound over the bot's first word.
VOICE_CUT_SECS = 0.01
# After the voice, the sound stays off this long, so a short pause between
# the bot's words doesn't let it creep back in before the gate catches up.
VOICE_HOLD_SECS = 0.5
# A chunk peaking above this is the bot's voice (TTS silence is digital zero).
VOICE_PEAK = 300
# A wait the bot never ends (an ignored turn, a lost frame) must not leave the
# sound playing for the rest of the call.
MAX_WAIT_SECS = 30.0
# A reply the LLM finished without speech or a tool call is over.
ABANDONED_REPLY_SECS = 3.0


# Each time the sound starts it skips ahead to a part of the file that is
# lively for at least LIVELY_SECS: a wait is often only a second or so, and an
# ambience file has pauses, quiet dips and loud one-off sounds (a door) that
# would make one wait silent and the next one jarring.
LIVELY_SECS = 1.5
# "Lively", measured against the file's typical (median) level so a few loud
# moments don't skew it: the next LIVELY_SECS sits between half and four times
# the typical energy (-3 dB .. +6 dB), the first ONSET_SECS isn't quiet either
# (a short wait still has sound once faded in), it starts on sound, and holds
# no fully silent stretch as long as SILENT_RUN_SECS.
LIVELY_MIN = 0.5
LIVELY_MAX = 4.0
ONSET_SECS = 0.3
SILENT_RUN_SECS = 0.3
_CHUNK_SECS = 0.02


def lively_start_positions(sound: np.ndarray, sample_rate: int) -> np.ndarray:
    """Sample offsets where the next ``LIVELY_SECS`` of ``sound`` is lively.

    Works in 20 ms chunks and wraps around the end, since the sound loops.
    Empty when the file is too short or has no such stretch.
    """
    step = int(sample_rate * _CHUNK_SECS)
    n = len(sound) // step if step else 0
    window = int(LIVELY_SECS / _CHUNK_SECS)
    if n <= window:
        return np.array([], dtype=np.int64)
    energy = (sound[: n * step].astype(np.float64) ** 2).reshape(n, step).mean(axis=1)
    if energy.mean() <= 0:
        return np.array([], dtype=np.int64)
    wrapped = np.concatenate([energy, energy[:window]])
    cum = np.concatenate([[0.0], np.cumsum(wrapped)])

    def mean_over(chunks: int) -> np.ndarray:
        return (cum[chunks : chunks + n] - cum[:n]) / chunks

    window_energy = mean_over(window)
    onset_energy = mean_over(int(ONSET_SECS / _CHUNK_SECS))
    typical = np.median(window_energy)
    if typical <= 0:
        return np.array([], dtype=np.int64)

    silent = (wrapped < energy.mean() * 1e-4).astype(np.int64)
    run = int(SILENT_RUN_SECS / _CHUNK_SECS)
    silent_runs = np.convolve(silent, np.ones(run, dtype=np.int64), mode="valid") == run
    runs_cum = np.concatenate([[0], np.cumsum(silent_runs)])
    span = window - run + 1
    has_silence = runs_cum[np.minimum(np.arange(n) + span, len(silent_runs))] - runs_cum[:n] > 0

    good = (
        (window_energy >= typical * LIVELY_MIN)
        & (window_energy <= typical * LIVELY_MAX)
        & (onset_energy >= typical * LIVELY_MIN * 0.5)
        & ~has_silence
        & ~silent[:n].astype(bool)  # start on sound, not on the tail of a pause
    )
    return np.flatnonzero(good) * step


# Every call loads the same few files; find their lively parts once per file.
# Uploaded files are cached under a path derived from their storage key, so a
# new upload gets a new path and a fresh entry.
_lively_cache: dict[tuple[str, int, int], np.ndarray] = {}


def _cached_lively_start_positions(path: str, sound: np.ndarray, sample_rate: int) -> np.ndarray:
    key = (path, sample_rate, len(sound))
    if key not in _lively_cache:
        if len(_lively_cache) > 64:
            _lively_cache.clear()
        _lively_cache[key] = lively_start_positions(sound, sample_rate)
    return _lively_cache[key]


class GatedSoundfileMixer(SoundfileMixer):
    """A ``SoundfileMixer`` whose sound fades toward an on/off target.

    The gain moves by one fade step per mixed chunk, so fades follow the
    audio clock rather than wall time. Each time the sound starts from
    silence it skips ahead to a lively part of the file (see
    ``lively_start_positions``), so a short wait is never a quiet dip.
    """

    def __init__(self, *, fade_secs: float = FADE_SECS, **kwargs):
        super().__init__(**kwargs)
        self._fade_secs = fade_secs
        self._target = 0.0
        self._gain = 0.0
        self._voice_hold = 0.0  # seconds left in which the voice keeps the sound off
        self._lively: dict[str, np.ndarray] = {}

    async def start(self, sample_rate: int):
        await super().start(sample_rate)
        self._lively = {
            name: _cached_lively_start_positions(str(self._sound_files[name]), sound, sample_rate)
            for name, sound in self._sounds.items()
        }

    def _seek_lively(self) -> None:
        starts = self._lively.get(self._current_sound)
        if starts is None or not len(starts):
            return
        i = int(np.searchsorted(starts, self._sound_pos))
        self._sound_pos = int(starts[i] if i < len(starts) else starts[0])

    @property
    def gain(self) -> float:
        return self._gain

    def set_target(self, on: bool) -> None:
        self._target = 1.0 if on else 0.0

    def _mix_with_sound(self, audio: bytes):
        audio_np = np.frombuffer(audio, dtype=np.int16)
        chunk_size = len(audio_np)
        chunk_secs = chunk_size / self._sample_rate if self._sample_rate else 0.02
        if chunk_size and int(np.abs(audio_np).max()) > VOICE_PEAK:
            return self._cut_for_voice(audio_np, chunk_secs)
        if self._voice_hold > 0:
            self._voice_hold = max(0.0, self._voice_hold - chunk_secs)
            return audio

        step = chunk_secs / self._fade_secs if self._fade_secs > 0 else 1.0
        if self._gain <= 0.0 and self._target > 0.0:
            self._seek_lively()
        self._gain += max(-step, min(step, self._target - self._gain))
        sound_np = self._next_sound(chunk_size)
        if self._gain <= 0.0 or sound_np is None:
            return audio
        mixed = audio_np + sound_np.astype(np.float32) * (self._volume * self._gain)
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()

    def _next_sound(self, chunk_size: int) -> np.ndarray | None:
        """The next ``chunk_size`` samples of the sound, or None if it can't play."""
        if not self._mixing or self._current_sound not in self._sounds:
            return None
        sound = self._sounds[self._current_sound]
        if self._sound_pos + chunk_size > len(sound):
            if not self._loop:
                return None
            self._sound_pos = 0
        sound_np = sound[self._sound_pos : self._sound_pos + chunk_size]
        self._sound_pos += chunk_size
        return sound_np

    def _cut_for_voice(self, audio_np: np.ndarray, chunk_secs: float) -> bytes:
        """The bot is speaking in this chunk: drop the sound at once."""
        self._voice_hold = VOICE_HOLD_SECS
        if self._gain <= 0.0:
            return audio_np.tobytes()
        sound_np = self._next_sound(len(audio_np))
        gain, self._gain = self._gain, 0.0
        if sound_np is None:
            return audio_np.tobytes()
        ramp = max(1, min(len(audio_np), int(self._sample_rate * VOICE_CUT_SECS)))
        envelope = np.zeros(len(audio_np), dtype=np.float32)
        envelope[:ramp] = np.linspace(gain, 0.0, ramp, dtype=np.float32)
        mixed = audio_np + sound_np.astype(np.float32) * (self._volume * envelope)
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()


_GATE_FRAMES = (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    TTSStartedFrame,
    FunctionCallsStartedFrame,
    FunctionCallResultFrame,
    FunctionCallCancelFrame,
    EndFrame,
    CancelFrame,
)


class AmbientSoundGate(BaseObserver):
    """Turns a ``GatedSoundfileMixer`` on while the caller is waiting."""

    def __init__(
        self,
        mixer: GatedSoundfileMixer,
        *,
        mode: str,
        start_delay_secs: float = DEFAULT_START_DELAY_SECS,
        is_ignored_tool: Callable[[str], bool] | None = None,
        max_wait_secs: float = MAX_WAIT_SECS,
    ):
        super().__init__()
        self._mixer = mixer
        self._mode = mode
        self._start_delay = max(0.0, start_delay_secs)
        self._is_ignored_tool = is_ignored_tool or (lambda name: False)
        self._max_wait = max_wait_secs
        self._seen: set[int] = set()
        self._tool_ids: set[str] = set()
        self._bot_speaking = False
        self._user_speaking = False
        self._awaiting_reply = False
        self._reply_started = False
        self._wait_since = 0.0
        self._start_timer: asyncio.TimerHandle | None = None
        self._abandon_timer: asyncio.TimerHandle | None = None
        self._max_timer: asyncio.TimerHandle | None = None
        self._ended = False

    @property
    def playing(self) -> bool:
        return self._mixer._target > 0

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # Most frames are audio; ignore everything but the few that matter
        # before doing any bookkeeping.
        if not isinstance(frame, _GATE_FRAMES):
            return
        # Observers see a frame at every hop; act on each frame once.
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        if len(self._seen) > 10000:
            self._seen.clear()

        loop = asyncio.get_running_loop()
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._end_wait()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            self._end_wait()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            self._awaiting_reply = True
            self._reply_started = False
            self._wait_since = loop.time()
        elif isinstance(frame, (LLMFullResponseStartFrame, TTSStartedFrame)):
            self._reply_started = True
            self._cancel(self._abandon_timer)
            self._abandon_timer = None
        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._awaiting_reply and not self._tool_ids:
                self._cancel(self._abandon_timer)
                self._abandon_timer = loop.call_later(ABANDONED_REPLY_SECS, self._abandon_reply)
        elif isinstance(frame, FunctionCallsStartedFrame):
            counted = [c for c in frame.function_calls if not self._is_ignored_tool(c.function_name)]
            if counted and not self._tool_ids and (self._mode == PLAY_TOOL_CALLS or not self._awaiting_reply):
                self._wait_since = loop.time()
            self._tool_ids.update(c.tool_call_id for c in counted)
            if counted:
                self._reply_started = True
                self._cancel(self._abandon_timer)
                self._abandon_timer = None
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            # Intermediate results of an async tool keep the wait going.
            properties = getattr(frame, "properties", None)
            if getattr(properties, "is_final", True) is not False:
                self._tool_ids.discard(frame.tool_call_id)
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._ended = True
        else:
            return
        self._update()

    def _wants_sound(self) -> bool:
        if self._ended or self._bot_speaking or self._user_speaking:
            return False
        if self._mode == PLAY_TOOL_CALLS:
            return bool(self._tool_ids)
        if self._mode == PLAY_THINKING:
            return bool(self._tool_ids) or (self._awaiting_reply and self._reply_started)
        return False

    def _update(self) -> None:
        if not self._wants_sound():
            self._cancel_timers()
            self._mixer.set_target(False)
            return
        if self.playing or self._start_timer is not None:
            return
        # The delay counts from when the wait began (the caller's turn ending
        # or the tool starting), not from when the bot's reply got going.
        loop = asyncio.get_running_loop()
        remaining = max(0.0, self._start_delay - (loop.time() - self._wait_since))
        self._start_timer = loop.call_later(remaining, self._start)

    def _start(self) -> None:
        self._start_timer = None
        if not self._wants_sound():
            return
        self._mixer.set_target(True)
        self._max_timer = asyncio.get_running_loop().call_later(self._max_wait, self._give_up)

    def _end_wait(self) -> None:
        self._awaiting_reply = False
        self._reply_started = False
        self._cancel(self._abandon_timer)
        self._abandon_timer = None

    def _abandon_reply(self) -> None:
        self._abandon_timer = None
        self._awaiting_reply = False
        self._update()

    def _give_up(self) -> None:
        self._max_timer = None
        logger.debug("Ambient sound: wait ran past its limit, stopping the sound")
        self._awaiting_reply = False
        self._tool_ids.clear()
        self._mixer.set_target(False)

    @staticmethod
    def _cancel(timer: asyncio.TimerHandle | None) -> None:
        if timer is not None:
            timer.cancel()

    def _cancel_timers(self) -> None:
        self._cancel(self._start_timer)
        self._cancel(self._max_timer)
        self._start_timer = None
        self._max_timer = None


def _number(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return min(high, max(low, number))


def normalize_ambient_config(config: dict | None) -> dict:
    """The stored ambient settings with every value usable at call time.

    Settings saved through the settings API are validated, but other writers
    (MCP and SDK saves, duplicated or imported agents) store them as-is, and
    agents saved before a field existed or before its range was set can hold
    anything. A bad value must never fail a call: an unknown play mode or
    sound falls back to the default, and numbers are clamped into range.
    """
    config = dict(config or {})
    mode = config.get("play_mode")
    config["play_mode"] = mode if mode in PLAY_MODES else PLAY_WHOLE_CALL
    sound = config.get("sound")
    config["sound"] = sound if sound in BUILT_IN_SOUND_NAMES else "office"
    config["volume"] = _number(config.get("volume"), DEFAULT_VOLUME, 0.0, MAX_VOLUME)
    config["start_delay_secs"] = _number(
        config.get("start_delay_secs"), DEFAULT_START_DELAY_SECS, 0.0, MAX_START_DELAY_SECS
    )
    return config


def attach_ambient_sound_gate(task, transport, ambient_config: dict | None, llm=None) -> None:
    """Start following the call if the transport's mixer plays only at moments.

    Leaves whole-call ambient sound (an ordinary ``SoundfileMixer``) alone.
    """
    mixer = getattr(getattr(transport, "_params", None), "audio_out_mixer", None)
    if not isinstance(mixer, GatedSoundfileMixer):
        return
    config = normalize_ambient_config(ambient_config)

    def is_ignored_tool(name: str) -> bool:
        item = getattr(llm, "_functions", {}).get(name) if llm is not None else None
        return bool(item and item.is_node_transition)

    gate = AmbientSoundGate(
        mixer,
        mode=config["play_mode"],
        start_delay_secs=config["start_delay_secs"],
        is_ignored_tool=is_ignored_tool,
    )
    task.add_observer(gate)
