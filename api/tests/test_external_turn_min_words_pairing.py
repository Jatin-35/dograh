"""Turn strategies for an STT that owns turn detection.

run_pipeline gives an STT that owns turn detection (Deepgram Flux, Cartesia
ink-2, Soniox turn detection) external start AND stop strategies, whatever the
agent's turn_start_strategy says (upstream dograh #767): pairing our min_words
start with the STT's stop can lose the stop when the caller barges in, so the
bot never answers. Every caller turn must reach the LLM.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    EndWorkerFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests import MockLLMService
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from api.services.pipecat.run_pipeline import (
    _create_non_realtime_user_turn_start_strategies,
    _create_non_realtime_user_turn_stop_strategies,
)
from api.services.pipecat.worker_runner import run_pipeline_worker


class _Injector(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

    async def inject(self, frame, direction=FrameDirection.DOWNSTREAM):
        await self.push_frame(frame, direction)


async def _two_turns(start_strategies, stop_strategies) -> int:
    """Play two externally bounded caller turns; return LLM generations."""
    context = LLMContext()
    pair = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=start_strategies, stop=stop_strategies
            )
        ),
    )
    llm = MockLLMService(
        mock_steps=[
            MockLLMService.create_text_chunks(text="First reply."),
            MockLLMService.create_text_chunks(text="Second reply."),
        ],
        chunk_delay=0.001,
    )
    injector = _Injector()
    worker = PipelineWorker(
        Pipeline([injector, pair.user(), llm, pair.assistant()]),
        params=PipelineParams(),
        enable_rtvi=False,
    )

    async def turns():
        await asyncio.sleep(0.05)
        for step, text in ((1, "I need a water tank"), (2, "five hundred litres please")):
            # An external-turn STT: start, final transcript, stop.
            await injector.inject(UserStartedSpeakingFrame())
            await asyncio.sleep(0)
            await injector.inject(
                TranscriptionFrame(text, "caller", time_now_iso8601(), finalized=True)
            )
            await asyncio.sleep(0.02)
            await injector.inject(UserStoppedSpeakingFrame())
            try:
                async with asyncio.timeout(2.0):
                    while llm.get_current_step() < step:
                        await asyncio.sleep(0.01)
            except TimeoutError:
                break
        await injector.inject(EndWorkerFrame(), direction=FrameDirection.UPSTREAM)

    await asyncio.wait_for(asyncio.gather(run_pipeline_worker(worker), turns()), 20)
    return llm.get_current_step()


@pytest.mark.asyncio
async def test_external_start_and_stop_answer_every_turn():
    generations = await _two_turns(
        [ExternalUserTurnStartStrategy(enable_interruptions=True)],
        [ExternalUserTurnStopStrategy()],
    )
    assert generations == 2


@pytest.mark.asyncio
async def test_min_words_start_with_external_stop_answers_every_turn():
    generations = await _two_turns(
        [MinWordsUserTurnStartStrategy(min_words=2)],
        [ExternalUserTurnStopStrategy()],
    )
    assert generations == 2


@pytest.mark.asyncio
async def test_production_strategies_for_external_turn_stt_answer_every_turn():
    """The exact strategies run_pipeline builds, for an unconfigured agent and
    for one that asked for min_words: the STT decides the start in both."""
    for configs in ({}, {"turn_start_strategy": "min_words"}):
        starts = _create_non_realtime_user_turn_start_strategies(
            configs, uses_external_turns=True
        )
        assert [type(s) for s in starts] == [ExternalUserTurnStartStrategy]
    configs: dict = {}
    generations = await _two_turns(
        _create_non_realtime_user_turn_start_strategies(
            configs, uses_external_turns=True
        ),
        _create_non_realtime_user_turn_stop_strategies(
            configs, uses_external_turns=True
        ),
    )
    assert generations == 2
