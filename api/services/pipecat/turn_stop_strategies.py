"""User-turn stop strategies with the pipecat 1.6/1.7 fixes our pipecat lacks.

Our pinned pipecat (v1.5) has two end-of-turn bugs that newer pipecat fixed.
These subclasses carry the fixes without touching the submodule:

Speech timeout (the default "transcription" turn end) and Smart Turn:

- **Live VAD state survives a turn boundary.** Our controller calls
  ``reset()`` on every stop strategy at each turn start and stop, and
  ``reset()`` cleared ``_vad_user_speaking``. VAD only reports speaking on a
  start/stop transition, so a turn begun mid-utterance (from a transcript)
  left the strategy thinking the caller was silent and the next transcript
  could end the turn while they were still talking (pipecat 1.6).
- **An interim transcript re-opens the STT wait.** A transcript the STT
  finalized during a pause too short for VAD to report a stop left
  ``_transcript_finalized`` set, so the next VAD stop skipped the STT safety
  net (or, for Smart Turn, fired at once) while the tail of the sentence was
  still in flight (pipecat 1.6).

Smart Turn only (pipecat 1.6/1.7):

- **No phantom end-of-turn after an externally ended turn.** When a turn ends
  some other way (the stop watchdog, a forced stop), the analyzer's buffered
  speech is dropped so a stale silence timer can't fire later.
- **The STT safety net keeps its budget.** It is anchored to when the caller
  actually stopped (VAD's timestamp minus ``stop_secs``), so time spent in
  Smart Turn inference before the timer starts no longer delays the turn.
- **A transcript that finalized during inference is acted on at once,** by
  re-checking the trigger as soon as the analyzer's verdict is known, instead
  of waiting out the safety-net timer.
"""

from __future__ import annotations

import time

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, VADUserStoppedSpeakingFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)


class DograhSpeechTimeoutUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """Speech-timeout turn end with the pipecat 1.6 fixes."""

    async def reset(self):
        # Whether the caller is speaking belongs to the caller, not the turn.
        speaking = self._vad_user_speaking
        await super().reset()
        self._vad_user_speaking = speaking

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, InterimTranscriptionFrame):
            # More transcription is on the way, so an earlier finalized
            # transcript no longer covers everything the caller said.
            self._transcript_finalized = False
        return await super().process_frame(frame)


class DograhTurnAnalyzerUserTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Smart Turn (turn analyzer) turn end with the pipecat 1.6/1.7 fixes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # The controller resets stop strategies at each turn start and stop,
        # strictly alternating (it ignores a start inside a turn and a stop
        # outside one), so this tells the two boundaries apart.
        self._turn_open = False

    async def reset(self):
        speaking = self._vad_user_speaking
        await super().reset()
        self._vad_user_speaking = speaking
        self._turn_open = not self._turn_open
        if not self._turn_open:
            # The turn just ended, possibly not by the analyzer: drop its
            # buffered speech so a stale silence timer can't end a phantom turn.
            # (A no-op after the analyzer's own COMPLETE, which clears itself.)
            self._turn_analyzer.clear()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, InterimTranscriptionFrame):
            self._transcript_finalized = False
        return await super().process_frame(frame)

    async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame):
        self._vad_user_speaking = False
        self._stop_secs = frame.stop_secs
        self._vad_stopped_time = frame.timestamp
        # The STT budget runs from when the caller actually stopped speaking,
        # which VAD reports stop_secs later; fix the deadline before inference.
        stt_deadline = frame.timestamp - frame.stop_secs + self._stt_timeout

        state, prediction = await self._turn_analyzer.analyze_end_of_turn()
        await self._handle_prediction_result(prediction)
        self._turn_complete = state == EndOfTurnState.COMPLETE

        timeout = max(0.0, stt_deadline - time.time())
        self._timeout_task = self.task_manager.create_task(
            self._timeout_handler(timeout), f"{self}::_timeout_handler"
        )
        # A finalized transcript may already have arrived while the analyzer
        # was running; its verdict is only known now, so check right away.
        await self._maybe_trigger_user_turn_stopped()
