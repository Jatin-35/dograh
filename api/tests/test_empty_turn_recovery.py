"""Recovery from a caller turn that cut the bot off but carried no words."""

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallFromLLM,
    FunctionCallResultFrame,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services.pipecat.empty_turn_recovery import (
    EMPTY_TURN_INTERRUPTED_PROMPT,
    BotActivityObserver,
    EmptyTurnRecovery,
)


async def _push(observer, frame):
    await observer.on_push_frame(SimpleNamespace(frame=frame))


class _Aggregator:
    def __init__(self, muted=False):
        self.context = LLMContext(messages=[{"role": "assistant", "content": "Aapka city aur state kaunsa hai?"}])
        self._user_is_muted = muted
        self.pushed = 0

    async def push_context_frame(self):
        self.pushed += 1


async def _greeting_done(observer):
    await _push(observer, BotStartedSpeakingFrame())
    await _push(observer, BotStoppedSpeakingFrame())


def _msg(content):
    return SimpleNamespace(content=content)


# ── Bot activity ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_counts_as_busy_until_the_greeting_has_played():
    obs = BotActivityObserver()
    assert obs.busy
    await _greeting_done(obs)
    assert not obs.busy


@pytest.mark.asyncio
async def test_thinking_speaking_and_tools_make_the_bot_busy():
    obs = BotActivityObserver()
    await _greeting_done(obs)
    await _push(obs, LLMFullResponseStartFrame())
    assert obs.busy
    await _push(obs, LLMFullResponseEndFrame())
    assert not obs.busy
    await _push(obs, BotStartedSpeakingFrame())
    assert obs.busy
    await _push(obs, BotStoppedSpeakingFrame())
    call = FunctionCallFromLLM(function_name="vectus_tank_lookup", tool_call_id="c1", arguments={}, context=None)
    await _push(obs, FunctionCallsStartedFrame(function_calls=[call]))
    assert obs.busy
    await _push(obs, FunctionCallResultFrame(function_name="vectus_tank_lookup", tool_call_id="c1", arguments={}, result={}))
    assert not obs.busy


@pytest.mark.asyncio
async def test_the_same_frame_seen_at_several_hops_counts_once():
    obs = BotActivityObserver()
    call = FunctionCallFromLLM(function_name="t", tool_call_id="c1", arguments={}, context=None)
    started = FunctionCallsStartedFrame(function_calls=[call])
    for _ in range(3):  # observers see a frame at every processor hop
        await _push(obs, started)
    assert obs.function_calls == 1


# ── Recovery ──────────────────────────────────────────────────────────


async def _turn(recovery, aggregator, content):
    await recovery.on_user_turn_started()
    await recovery.on_user_turn_stopped(aggregator, _msg(content))


@pytest.mark.asyncio
async def test_empty_turn_that_cut_the_bot_off_asks_the_caller_to_repeat():
    obs = BotActivityObserver()
    await _greeting_done(obs)
    await _push(obs, BotStartedSpeakingFrame())  # bot is mid-reply
    rec, agg = EmptyTurnRecovery(obs), _Aggregator()
    await _turn(rec, agg, None)  # a cough: no transcript
    assert agg.pushed == 1
    assert agg.context.messages[-1] == {"role": "system", "content": EMPTY_TURN_INTERRUPTED_PROMPT}


@pytest.mark.asyncio
async def test_an_empty_turn_during_the_greeting_counts_as_interrupting():
    obs, agg = BotActivityObserver(), _Aggregator()
    rec = EmptyTurnRecovery(obs)
    await _turn(rec, agg, "")
    assert agg.pushed == 1


@pytest.mark.asyncio
async def test_empty_turn_while_the_bot_waits_is_left_alone():
    obs = BotActivityObserver()
    await _greeting_done(obs)  # bot finished and is waiting for the caller
    rec, agg = EmptyTurnRecovery(obs), _Aggregator()
    await _turn(rec, agg, None)
    assert agg.pushed == 0
    assert len(agg.context.messages) == 1


@pytest.mark.asyncio
async def test_a_turn_with_words_is_never_touched():
    obs = BotActivityObserver()
    await _push(obs, BotStartedSpeakingFrame())
    rec, agg = EmptyTurnRecovery(obs), _Aggregator()
    await _turn(rec, agg, "haan ji, Samba")
    assert agg.pushed == 0


@pytest.mark.asyncio
async def test_only_one_empty_turn_in_a_row_is_answered_until_the_caller_speaks():
    obs = BotActivityObserver()
    await _push(obs, BotStartedSpeakingFrame())
    rec, agg = EmptyTurnRecovery(obs), _Aggregator()
    await _turn(rec, agg, None)
    await _turn(rec, agg, None)  # more noise right after
    assert agg.pushed == 1
    await _turn(rec, agg, "Samba, Jammu Kashmir")  # real words reset the limit
    await _turn(rec, agg, None)
    assert agg.pushed == 2


@pytest.mark.asyncio
async def test_no_recovery_while_muted_or_a_tool_is_running():
    obs = BotActivityObserver()
    await _push(obs, BotStartedSpeakingFrame())
    muted = _Aggregator(muted=True)
    await _turn(EmptyTurnRecovery(obs), muted, None)
    assert muted.pushed == 0

    call = FunctionCallFromLLM(function_name="t", tool_call_id="c9", arguments={}, context=None)
    await _push(obs, FunctionCallsStartedFrame(function_calls=[call]))
    agg = _Aggregator()
    await _turn(EmptyTurnRecovery(obs), agg, None)
    assert agg.pushed == 0  # the tool result will run the LLM itself
