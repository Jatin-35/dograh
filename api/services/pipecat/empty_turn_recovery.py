"""Recover from a caller turn that cut the bot off but carried no words.

A user turn can start on voice activity alone and end with no transcript: a
cough, a horn, background chatter or speech the STT couldn't make out. If that
turn interrupted the bot (while it was thinking, speaking or running a tool),
the bot's reply was cut and nothing runs the LLM again, so the bot falls
silent mid-conversation until the caller speaks up.

This is pipecat 1.12's ``EmptyUserTurnConfig`` behaviour, carried on our
pinned pipecat: for such a turn a short instruction is added to the context
and the LLM runs once, so the bot briefly asks the caller to repeat and repeats
anything of its own they may have missed. A turn while the bot was idle and
waiting is left alone (answering what may be noise would be intrusive), and
only one empty turn in a row is answered.

The instruction uses the ``system`` role: some OpenAI-compatible providers
(Sarvam) reject ``developer``, and the Claude and Gemini adapters turn a later
system message into a user message themselves.

Cascaded (STT -> LLM -> TTS) pipelines only; a realtime model hears the audio
itself and decides how to respond.
"""

from __future__ import annotations

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallCancelFrame,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

EMPTY_TURN_INTERRUPTED_PROMPT = (
    "The user may have said something while you were responding, but it was not "
    "recognized. Your response was cut off there, and the conversation only "
    "includes the part they heard, which may be none of it. Briefly ask them to "
    "repeat what they said, and repeat any question of yours they may have missed."
)
MAX_CONSECUTIVE_RECOVERIES = 1


class BotActivityObserver(BaseObserver):
    """Tracks whether the bot is busy: thinking, speaking or running a tool.

    Before the bot has first finished speaking (the greeting) it also counts
    as busy, as in pipecat 1.12.
    """

    def __init__(self):
        super().__init__()
        self._seen: set[int] = set()
        self.thinking = False
        self.tts_active = False
        self.speaking = False
        self.function_calls = 0
        self.finished_first_speech = False

    @property
    def busy(self) -> bool:
        return (
            self.thinking
            or self.tts_active
            or self.speaking
            or self.function_calls > 0
            or not self.finished_first_speech
        )

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # Observers see a frame at every hop; act on each frame once.
        if frame.id in self._seen:
            return
        self._seen.add(frame.id)
        if len(self._seen) > 10000:
            self._seen.clear()
        if isinstance(frame, LLMFullResponseStartFrame):
            self.thinking = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.thinking = False
        elif isinstance(frame, TTSStartedFrame):
            self.tts_active = True
        elif isinstance(frame, TTSStoppedFrame):
            self.tts_active = False
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.speaking = False
            self.tts_active = False
            self.finished_first_speech = True
        elif isinstance(frame, FunctionCallsStartedFrame):
            self.function_calls += len(frame.function_calls)
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self.function_calls = max(0, self.function_calls - 1)


class EmptyTurnRecovery:
    """Hooks a user aggregator so an empty, interrupting turn gets one answer."""

    def __init__(self, activity: BotActivityObserver, *, prompt: str = EMPTY_TURN_INTERRUPTED_PROMPT):
        self._activity = activity
        self._prompt = prompt
        self._interrupted_bot = False
        self._consecutive = 0
        self.recoveries = 0

    def attach(self, user_aggregator) -> None:
        @user_aggregator.event_handler("on_user_turn_started")
        async def _on_started(aggregator, strategy):
            await self.on_user_turn_started()

        @user_aggregator.event_handler("on_user_turn_stopped")
        async def _on_stopped(aggregator, strategy, message):
            await self.on_user_turn_stopped(aggregator, message)

    async def on_user_turn_started(self) -> None:
        # Read before the interruption this turn sends lands: was the bot
        # thinking, speaking or running a tool when the caller cut in?
        self._interrupted_bot = self._activity.busy

    async def on_user_turn_stopped(self, aggregator, message) -> None:
        interrupted_bot = self._interrupted_bot
        self._interrupted_bot = False
        content = getattr(message, "content", None)
        if content and str(content).strip():
            self._consecutive = 0
            return
        if not interrupted_bot:
            return
        if self._consecutive >= MAX_CONSECUTIVE_RECOVERIES:
            logger.debug("Empty user turn left unanswered (too many in a row)")
            return
        # A pending tool result runs the LLM itself, and a muted caller
        # shouldn't be asked to speak.
        if getattr(aggregator, "_user_is_muted", False) or self._activity.function_calls > 0:
            return
        self._consecutive += 1
        self.recoveries += 1
        logger.info("Empty user turn interrupted the bot; asking the caller to repeat")
        aggregator.context.add_message({"role": "system", "content": self._prompt})
        await aggregator.push_context_frame()
