"""Dograh-driven turn detection for a realtime service in manual mode.

Used when an Inworld Realtime agent sets turn detection to ``dograh_local``:
Inworld's own turn detection is off, and Dograh decides turns from the
caller's audio (after RNNoise) with Silero VAD, as pipecat's
``realtime-inworld-locally-driven-turns`` example does.

Inworld's server-side detection cancels a reply on any sound it takes for
speech, and with Soniox a turn opened that way only closes when the caller
speaks again (runs 1091-1100: replies cancelled 93 ms after the caller stopped,
turns held open 15-20 s). Here a turn always ends on the caller's silence, and
while the bot is speaking the caller interrupts only with real words, or with
sustained speech if no transcript arrives in time.
"""

from api.services.pipecat.silero_vad import DograhSileroVADAnalyzer
from api.services.pipecat.turn_stop_strategies import (
    DograhSpeechTimeoutUserTurnStopStrategy,
)
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

DEFAULT_MIN_WORDS = 2
DEFAULT_BARGE_IN_SECS = 1.2
DEFAULT_SILENCE_SECS = 0.6
# Pauses shorter than this inside the caller's speech (between words, after a
# comma) keep counting toward an interruption; a longer silence starts over.
SPEECH_GAP_SECS = 0.6


def audio_secs(frame: InputAudioRawFrame) -> float:
    """Duration of a 16-bit PCM audio frame."""
    return len(frame.audio) / (frame.sample_rate * 2 * max(1, frame.num_channels))


class WordGatedVADUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Start on voice when the bot is quiet; interrupt it only with real speech.

    - Bot quiet: the turn starts as soon as VAD hears the caller (or a word is
      transcribed), so the caller's turn is never missed.
    - Bot speaking: the turn (and the interruption) starts only once the
      caller has said ``min_words`` words, or has been speaking for
      ``min_barge_in_secs`` in total, counting across pauses shorter than
      ``SPEECH_GAP_SECS`` (for when no transcript arrives during the bot's
      speech). Noise, breaths and echo blips do neither, so they no longer
      cut the bot off. Time is measured from the audio itself.
    """

    def __init__(
        self,
        *,
        min_words: int = DEFAULT_MIN_WORDS,
        min_barge_in_secs: float | None = DEFAULT_BARGE_IN_SECS,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._min_words = max(1, int(min_words))
        self._min_barge_in_secs = min_barge_in_secs
        self._bot_speaking = False
        self._vad_active = False
        self._voiced_secs = 0.0
        self._gap_secs = 0.0

    async def reset(self):
        # Whether the bot or the caller is speaking belongs to the call, not
        # the turn.
        await super().reset()
        self._voiced_secs = 0.0
        self._gap_secs = 0.0

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._voiced_secs = 0.0
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_active = True
            self._gap_secs = 0.0
            if not self._bot_speaking:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_active = False
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            # Only words heard while the caller is speaking count: the final
            # transcript of a turn arrives after it ended (on commit) and must
            # not open a new one, which would cancel the reply just asked for.
            if not self._vad_active:
                return ProcessFrameResult.CONTINUE
            needed = self._min_words if self._bot_speaking else 1
            if len((frame.text or "").split()) >= needed:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
        elif isinstance(frame, InputAudioRawFrame) and self._bot_speaking:
            secs = audio_secs(frame)
            if self._vad_active:
                self._voiced_secs += secs
                self._gap_secs = 0.0
            else:
                self._gap_secs += secs
                if self._gap_secs >= SPEECH_GAP_SECS:
                    self._voiced_secs = 0.0
            if (
                self._min_barge_in_secs is not None
                and self._voiced_secs >= self._min_barge_in_secs
            ):
                self._voiced_secs = 0.0
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
        return ProcessFrameResult.CONTINUE


def dograh_local_turn_config(
    *,
    min_words: int | None = None,
    min_barge_in_secs: float | None = None,
    silence_secs: float | None = None,
) -> tuple[UserTurnStrategies, DograhSileroVADAnalyzer]:
    """Turn strategies and VAD for a realtime service whose turns Dograh decides."""
    return (
        UserTurnStrategies(
            start=[
                WordGatedVADUserTurnStartStrategy(
                    min_words=DEFAULT_MIN_WORDS if min_words is None else min_words,
                    min_barge_in_secs=(
                        DEFAULT_BARGE_IN_SECS
                        if min_barge_in_secs is None
                        else min_barge_in_secs
                    ),
                )
            ],
            stop=[
                DograhSpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=(
                        DEFAULT_SILENCE_SECS if silence_secs is None else silence_secs
                    ),
                    wait_for_transcript=False,
                )
            ],
        ),
        DograhSileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
    )
