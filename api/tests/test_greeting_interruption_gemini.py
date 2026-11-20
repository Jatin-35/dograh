"""Gemini Live: does the caller's audio reach Google during the greeting?

Our Gemini services replace caller audio with silence while the user
aggregator reports the caller as muted (UserMuteStartedFrame), so Google's
input clock keeps running but it hears nothing. Wires the real user aggregator, the
real mute-strategy stack and the real Gemini 3.8 service together, plays a
"greeting" (bot speaking), streams caller audio through it, and counts the
audio chunks actually sent on to Gemini.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.tests.utils import SleepFrame, run_test

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.realtime.gemini_live_38 import DograhGemini38LiveLLMService
from api.services.pipecat.run_pipeline import (
    _create_realtime_user_turn_config,
    build_user_mute_strategies,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.tests.test_greeting_interruption import _workflow


class _Gemini38(DograhGemini38LiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=None))
        )


# Each phase's audio carries its own byte, so what Gemini receives can be
# traced to the phase it was spoken in. Muted audio arrives as zeros.
_PHASE_BYTES = {1: "before_greeting", 2: "during_greeting", 3: "after_greeting"}


def _audio(n, phase_byte):
    return [
        InputAudioRawFrame(
            audio=bytes([phase_byte, 0]) * 320, sample_rate=16000, num_channels=1
        )
        for _ in range(n)
    ]


async def _audio_sent_to_gemini(allow_interrupt_greeting: bool) -> dict:
    graph = _workflow(allow_interrupt_greeting=allow_interrupt_greeting)
    engine = PipecatEngine(workflow=graph, call_context_vars={}, workflow_run_id=1)
    engine.active_agent.current_node = graph.nodes[graph.start_node_id]

    turn_strategies, vad = _create_realtime_user_turn_config(
        ServiceProviders.GEMINI_LIVE_38.value
    )
    user_agg = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=turn_strategies,
            user_mute_strategies=build_user_mute_strategies(
                engine.should_mute_user,
                allow_interrupt_greeting=allow_interrupt_greeting,
            ),
            vad_analyzer=vad,
        ),
    ).user()

    gemini = _Gemini38(
        api_key="k", settings=_Gemini38.Settings(model="gemini-3.8-live")
    )
    gemini.push_error = AsyncMock()

    sent = {"before_greeting": 0, "during_greeting": 0, "after_greeting": 0}
    frames = []
    # Caller talks as soon as the call connects, before the bot speaks.
    frames += _audio(3, 1)
    frames.append(SleepFrame(0.05))
    frames.append(BotStartedSpeakingFrame())
    frames.append(SleepFrame(0.05))
    frames += _audio(5, 2)
    frames.append(SleepFrame(0.05))
    frames.append(BotStoppedSpeakingFrame())
    frames.append(SleepFrame(0.05))
    frames += _audio(4, 3)
    frames.append(SleepFrame(0.05))

    # Count the caller audio Gemini is handed, by the phase it was spoken in.
    async def count_frame(self, frame):
        phase = _PHASE_BYTES.get(max(frame.audio, default=0))
        if phase is not None:
            sent[phase] += 1

    with (
        patch.object(GeminiLiveLLMService, "_send_user_audio", count_frame),
        patch.object(_Gemini38, "_connect", AsyncMock()),
        patch.object(_Gemini38, "_disconnect", AsyncMock()),
    ):
        await run_test(
            Pipeline([user_agg, gemini]),
            frames_to_send=frames,
            expected_down_frames=None,
        )
    return sent


@pytest.mark.asyncio
async def test_switch_off_gemini_hears_nothing_until_the_greeting_ends():
    sent = await _audio_sent_to_gemini(False)
    assert sent["before_greeting"] == 0
    assert sent["during_greeting"] == 0
    assert sent["after_greeting"] == 4


@pytest.mark.asyncio
async def test_switch_on_gemini_hears_the_caller_from_the_start():
    sent = await _audio_sent_to_gemini(True)
    assert sent == {"before_greeting": 3, "during_greeting": 5, "after_greeting": 4}
