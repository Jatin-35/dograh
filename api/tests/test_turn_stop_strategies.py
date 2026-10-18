"""The pipecat 1.6/1.7 end-of-turn fixes carried by our turn-stop strategies.

Each test drives the strategy with the frames that reproduce one bug; where it
helps, the same sequence is run on pipecat's stock strategy to show the bug.
"""

import asyncio
import time

import pytest
from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer, BaseTurnParams, EndOfTurnState
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from api.services.pipecat.turn_stop_strategies import (
    DograhSpeechTimeoutUserTurnStopStrategy,
    DograhTurnAnalyzerUserTurnStopStrategy,
)

SPEECH_TIMEOUT = 0.1


async def _setup(strategy, stt_timeout: float):
    tm = TaskManager()
    tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    await strategy.setup(tm)
    await strategy.process_frame(STTMetadataFrame(service_name="test", ttfs_p99_latency=stt_timeout))
    stopped = []

    @strategy.event_handler("on_user_turn_stopped")
    async def _on_stopped(strategy, params):
        stopped.append(time.monotonic())

    return stopped


def _text(text, finalized=False):
    return TranscriptionFrame(text=text, user_id="caller", timestamp="", finalized=finalized)


# ── Speech timeout ────────────────────────────────────────────────────


async def _mid_utterance_turn_boundary(cls):
    strategy = cls(user_speech_timeout=SPEECH_TIMEOUT)
    stopped = await _setup(strategy, stt_timeout=0.0)
    await strategy.process_frame(VADUserStartedSpeakingFrame())  # caller starts talking
    await strategy.reset()  # a turn boundary lands while they are still talking
    await strategy.process_frame(_text("mera pincode hai"))
    await asyncio.sleep(SPEECH_TIMEOUT + 0.15)
    return stopped


@pytest.mark.asyncio
async def test_stock_strategy_ends_the_turn_while_the_caller_is_still_talking():
    assert await _mid_utterance_turn_boundary(SpeechTimeoutUserTurnStopStrategy), (
        "expected the bug: stock pipecat v1.5 forgets the caller is speaking"
    )


@pytest.mark.asyncio
async def test_turn_stays_open_while_the_caller_is_still_talking():
    assert await _mid_utterance_turn_boundary(DograhSpeechTimeoutUserTurnStopStrategy) == []


@pytest.mark.asyncio
async def test_turn_still_ends_once_the_caller_stops():
    strategy = DograhSpeechTimeoutUserTurnStopStrategy(user_speech_timeout=SPEECH_TIMEOUT)
    stopped = await _setup(strategy, stt_timeout=0.0)
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    await strategy.reset()
    await strategy.process_frame(_text("mera pincode hai 184", finalized=True))
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
    await asyncio.sleep(SPEECH_TIMEOUT + 0.15)
    assert len(stopped) == 1


async def _finalized_then_more_speech(cls):
    """STT finalizes on a short pause, then the caller keeps talking."""
    strategy = cls(user_speech_timeout=SPEECH_TIMEOUT)
    stopped = await _setup(strategy, stt_timeout=0.6)
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    await strategy.process_frame(_text("Samba", finalized=True))  # finalized on a short pause
    await strategy.process_frame(InterimTranscriptionFrame(text="Jammu", user_id="caller", timestamp=""))
    t0 = time.monotonic()
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
    await asyncio.sleep(SPEECH_TIMEOUT + 0.15)
    return stopped, t0, strategy


@pytest.mark.asyncio
async def test_stock_strategy_skips_the_stt_wait_after_an_early_finalized_transcript():
    stopped, _, _ = await _finalized_then_more_speech(SpeechTimeoutUserTurnStopStrategy)
    assert stopped, "expected the bug: the tail of the sentence is not waited for"


@pytest.mark.asyncio
async def test_interim_transcript_reopens_the_stt_wait():
    stopped, _, strategy = await _finalized_then_more_speech(DograhSpeechTimeoutUserTurnStopStrategy)
    assert stopped == []  # still waiting for the rest of "Samba, Jammu Kashmir"
    await strategy.process_frame(_text(" Kashmir", finalized=True))
    await asyncio.sleep(0.05)
    assert len(stopped) == 1


# ── Smart Turn (turn analyzer) ───────────────────────────────────────


class _FakeAnalyzer(BaseTurnAnalyzer):
    """Answers COMPLETE after ``delay`` seconds of 'inference'."""

    def __init__(self, delay: float):
        super().__init__(sample_rate=8000)
        self.delay = delay
        self.cleared = 0

    @property
    def speech_triggered(self) -> bool:
        return False

    @property
    def params(self) -> BaseTurnParams:
        return BaseTurnParams()

    def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        await asyncio.sleep(self.delay)
        return EndOfTurnState.COMPLETE, None

    def clear(self):
        self.cleared += 1


async def _transcript_during_inference(cls):
    analyzer = _FakeAnalyzer(delay=0.2)
    strategy = cls(turn_analyzer=analyzer)
    stopped = await _setup(strategy, stt_timeout=1.0)
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    t0 = time.monotonic()

    async def finalize_during_inference():
        await asyncio.sleep(0.05)
        await strategy.process_frame(_text("haan ji, sahi hai", finalized=True))

    asyncio.create_task(finalize_during_inference())
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
    await asyncio.sleep(0.15)
    return stopped, t0


@pytest.mark.asyncio
async def test_stock_smart_turn_waits_out_the_safety_net():
    stopped, _ = await _transcript_during_inference(TurnAnalyzerUserTurnStopStrategy)
    assert stopped == [], "expected the bug: ~0.8 s extra wait before the turn ends"


@pytest.mark.asyncio
async def test_smart_turn_acts_on_a_transcript_finalized_during_inference():
    stopped, t0 = await _transcript_during_inference(DograhTurnAnalyzerUserTurnStopStrategy)
    assert len(stopped) == 1
    assert stopped[0] - t0 < 0.4  # right after the 0.2 s verdict, not ~1 s


@pytest.mark.asyncio
async def test_smart_turn_safety_net_is_not_delayed_by_inference():
    # 0.3 s inference, 0.5 s STT budget, 0.2 s stop_secs: the caller stopped
    # 0.2 s before VAD said so, so the budget ends right as inference does.
    analyzer = _FakeAnalyzer(delay=0.3)
    strategy = DograhTurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)
    stopped = await _setup(strategy, stt_timeout=0.5)
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    await strategy.process_frame(_text("haan ji"))  # text in hand, never finalized
    t0 = time.monotonic()
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2))
    await asyncio.sleep(0.25)
    assert len(stopped) == 1
    assert stopped[0] - t0 < 0.45  # stock pipecat: 0.3 + (0.5 - 0.2) = 0.6 s


@pytest.mark.asyncio
async def test_smart_turn_drops_buffered_speech_only_when_a_turn_ends():
    analyzer = _FakeAnalyzer(delay=0.0)
    strategy = DograhTurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)
    await _setup(strategy, stt_timeout=0.0)
    await strategy.reset()  # turn starts: keep the pre-speech buffer
    assert analyzer.cleared == 0
    await strategy.reset()  # turn ends (maybe not by the analyzer): drop it
    assert analyzer.cleared == 1
    await strategy.reset()  # next turn starts
    assert analyzer.cleared == 1


@pytest.mark.asyncio
async def test_smart_turn_keeps_live_vad_state_across_a_boundary():
    strategy = DograhTurnAnalyzerUserTurnStopStrategy(turn_analyzer=_FakeAnalyzer(delay=0.0))
    await _setup(strategy, stt_timeout=0.0)
    await strategy.process_frame(VADUserStartedSpeakingFrame())
    await strategy.reset()
    assert strategy._vad_user_speaking is True
