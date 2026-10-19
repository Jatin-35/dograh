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
FADE_SECS = 0.15
# A wait the bot never ends (an ignored turn, a lost frame) must not leave the
# sound playing for the rest of the call.
MAX_WAIT_SECS = 30.0
# A reply the LLM finished without speech or a tool call is over.
ABANDONED_REPLY_SECS = 3.0


class GatedSoundfileMixer(SoundfileMixer):
    """A ``SoundfileMixer`` whose sound fades toward an on/off target.

    The gain moves by one fade step per mixed chunk, so fades follow the
    audio clock rather than wall time.
    """

    def __init__(self, *, fade_secs: float = FADE_SECS, **kwargs):
        super().__init__(**kwargs)
        self._fade_secs = fade_secs
        self._target = 0.0
        self._gain = 0.0

    @property
    def gain(self) -> float:
        return self._gain

    def set_target(self, on: bool) -> None:
        self._target = 1.0 if on else 0.0

    def _mix_with_sound(self, audio: bytes):
        audio_np = np.frombuffer(audio, dtype=np.int16)
        chunk_secs = len(audio_np) / self._sample_rate if self._sample_rate else 0.02
        step = chunk_secs / self._fade_secs if self._fade_secs > 0 else 1.0
        self._gain += max(-step, min(step, self._target - self._gain))
        if self._gain <= 0.0 or not self._mixing or self._current_sound not in self._sounds:
            return audio

        sound = self._sounds[self._current_sound]
        chunk_size = len(audio_np)
        if self._sound_pos + chunk_size > len(sound):
            if not self._loop:
                return audio
            self._sound_pos = 0
        sound_np = sound[self._sound_pos : self._sound_pos + chunk_size]
        self._sound_pos += chunk_size

        mixed = audio_np + sound_np.astype(np.float32) * (self._volume * self._gain)
        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()


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


def attach_ambient_sound_gate(task, transport, ambient_config: dict | None, llm=None) -> None:
    """Start following the call if the transport's mixer plays only at moments.

    Leaves whole-call ambient sound (an ordinary ``SoundfileMixer``) alone.
    """
    mixer = getattr(getattr(transport, "_params", None), "audio_out_mixer", None)
    if not isinstance(mixer, GatedSoundfileMixer):
        return
    config = ambient_config or {}

    def is_ignored_tool(name: str) -> bool:
        item = getattr(llm, "_functions", {}).get(name) if llm is not None else None
        return bool(item and item.is_node_transition)

    gate = AmbientSoundGate(
        mixer,
        mode=config.get("play_mode", PLAY_WHOLE_CALL),
        start_delay_secs=float(config.get("start_delay_secs", DEFAULT_START_DELAY_SECS)),
        is_ignored_tool=is_ignored_tool,
    )
    task.add_observer(gate)
