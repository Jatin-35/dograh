"""Tests for the start node's "Allow Interruption During Greeting" switch.

By default the caller is muted from the moment the call connects until the
bot finishes its first speech (the greeting). With allow_interrupt_greeting
on, the caller is heard from the start and can barge in on the greeting;
after the greeting, the normal per-node allow_interrupt rule applies again.
"""

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
)
from pipecat.turns.user_mute import (
    CallbackUserMuteStrategy,
    FunctionCallUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)

from api.services.pipecat.run_pipeline import build_user_mute_strategies
from api.services.workflow.dto import (
    EdgeDataDTO,
    EndCallNodeData,
    Position,
    ReactFlowDTO,
    RFEdgeDTO,
    RFNodeDTO,
    StartCallNodeData,
)
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.workflow_graph import WorkflowGraph


def _workflow(
    *, allow_interrupt_greeting: bool | None, allow_interrupt: bool = False
) -> WorkflowGraph:
    start_data = {
        "name": "Start Call",
        "prompt": "Greet the caller.",
        "allow_interrupt": allow_interrupt,
        "add_global_prompt": False,
        "greeting_type": "text",
        "greeting": "Welcome to Think Gas!",
    }
    if allow_interrupt_greeting is not None:
        start_data["allow_interrupt_greeting"] = allow_interrupt_greeting
    dto = ReactFlowDTO(
        nodes=[
            RFNodeDTO(
                id="start",
                type="startCall",
                position=Position(x=0, y=0),
                data=StartCallNodeData(**start_data),
            ),
            RFNodeDTO(
                id="end",
                type="endCall",
                position=Position(x=0, y=200),
                data=EndCallNodeData(name="End Call", prompt="Say bye."),
            ),
        ],
        edges=[
            RFEdgeDTO(
                id="start-end",
                source="start",
                target="end",
                data=EdgeDataDTO(label="End", condition="When done"),
            )
        ],
    )
    return WorkflowGraph(dto)


def _engine(workflow: WorkflowGraph, *, on_agent_node: bool = False) -> PipecatEngine:
    engine = PipecatEngine(workflow=workflow, call_context_vars={}, workflow_run_id=1)
    node_id = "end" if on_agent_node else workflow.start_node_id
    engine._current_node = workflow.nodes[node_id]
    return engine


async def _muted(strategies, frame) -> bool:
    """Mirror the user aggregator: muted if any strategy says so."""
    results = [await s.process_frame(frame) for s in strategies]
    return any(results)


# ── Settings and parsing ──────────────────────────────────────────────


def test_switch_defaults_off():
    assert StartCallNodeData(name="Start", prompt="p").allow_interrupt_greeting is False


def test_old_saved_agent_without_the_field_reads_as_off():
    graph = _workflow(allow_interrupt_greeting=None)
    assert graph.nodes["start"].allow_interrupt_greeting is False


def test_switch_survives_a_save_and_reload():
    graph = _workflow(allow_interrupt_greeting=True)
    import json

    saved = json.loads(json.dumps(graph.nodes["start"].data.model_dump()))
    assert saved["allow_interrupt_greeting"] is True
    assert StartCallNodeData.model_validate(saved).allow_interrupt_greeting is True


def test_settings_form_shows_the_switch_next_to_allow_interruption():
    schema = StartCallNodeData.model_json_schema()["properties"]
    assert schema["allow_interrupt_greeting"]["type"] == "boolean"
    from api.services.workflow.node_specs import all_specs

    spec = next(s for s in all_specs() if s.name == "startCall")
    names = [p.name for p in spec.properties]
    assert names.index("allow_interrupt_greeting") == names.index("allow_interrupt") + 1
    prop = next(p for p in spec.properties if p.name == "allow_interrupt_greeting")
    assert prop.display_name == "Allow Interruption During Greeting"
    assert prop.default is False


# ── Mute strategy stack ───────────────────────────────────────────────


def test_default_stack_keeps_the_greeting_lock():
    strategies = build_user_mute_strategies(lambda f: False)
    assert [type(s) for s in strategies] == [
        MuteUntilFirstBotCompleteUserMuteStrategy,
        FunctionCallUserMuteStrategy,
        CallbackUserMuteStrategy,
    ]


def test_switch_on_drops_only_the_greeting_lock():
    strategies = build_user_mute_strategies(
        lambda f: False, allow_interrupt_greeting=True
    )
    assert [type(s) for s in strategies] == [
        FunctionCallUserMuteStrategy,
        CallbackUserMuteStrategy,
    ]


# ── End to end: real strategies + engine callback ─────────────────────


@pytest.mark.asyncio
async def test_switch_off_caller_muted_until_greeting_ends():
    engine = _engine(_workflow(allow_interrupt_greeting=False))
    strategies = build_user_mute_strategies(engine.should_mute_user)

    # Call connects, caller speaks before / during the greeting: muted.
    assert await _muted(strategies, UserStartedSpeakingFrame())
    assert await _muted(strategies, BotStartedSpeakingFrame())
    assert await _muted(strategies, UserStartedSpeakingFrame())
    # Greeting ends: caller is heard.
    assert not await _muted(strategies, BotStoppedSpeakingFrame())
    assert not await _muted(strategies, UserStartedSpeakingFrame())


@pytest.mark.asyncio
async def test_switch_on_caller_heard_from_the_start_and_during_greeting():
    engine = _engine(_workflow(allow_interrupt_greeting=True))
    strategies = build_user_mute_strategies(
        engine.should_mute_user, allow_interrupt_greeting=True
    )

    assert not await _muted(strategies, UserStartedSpeakingFrame())
    assert not await _muted(strategies, BotStartedSpeakingFrame())
    assert not await _muted(strategies, UserStartedSpeakingFrame())


@pytest.mark.asyncio
async def test_switch_on_later_turns_follow_the_node_allow_interrupt():
    # Start node blocks normal interruptions: after the greeting, the caller
    # is muted while the bot speaks again, exactly as before.
    engine = _engine(_workflow(allow_interrupt_greeting=True, allow_interrupt=False))
    strategies = build_user_mute_strategies(
        engine.should_mute_user, allow_interrupt_greeting=True
    )
    await _muted(strategies, BotStartedSpeakingFrame())
    assert not await _muted(strategies, BotStoppedSpeakingFrame())

    assert await _muted(strategies, BotStartedSpeakingFrame())
    assert await _muted(strategies, UserStartedSpeakingFrame())
    assert not await _muted(strategies, BotStoppedSpeakingFrame())


@pytest.mark.asyncio
async def test_switch_on_with_allow_interrupt_on_never_mutes_for_speech():
    engine = _engine(_workflow(allow_interrupt_greeting=True, allow_interrupt=True))
    strategies = build_user_mute_strategies(
        engine.should_mute_user, allow_interrupt_greeting=True
    )
    for frame in (
        BotStartedSpeakingFrame(),
        BotStoppedSpeakingFrame(),
        BotStartedSpeakingFrame(),
        UserStartedSpeakingFrame(),
    ):
        assert not await _muted(strategies, frame)


@pytest.mark.asyncio
async def test_switch_on_interrupted_greeting_counts_as_finished():
    # Caller barges in, the bot stops: the greeting phase is over and the
    # node's own rule takes over for the bot's next reply.
    engine = _engine(_workflow(allow_interrupt_greeting=True, allow_interrupt=False))
    await engine.should_mute_user(BotStartedSpeakingFrame())
    await engine.should_mute_user(UserStartedSpeakingFrame())
    await engine.should_mute_user(BotStoppedSpeakingFrame())
    assert engine._first_bot_speech_done is True
    assert await engine.should_mute_user(BotStartedSpeakingFrame()) is True


@pytest.mark.asyncio
async def test_switch_on_still_mutes_when_the_call_is_shutting_down():
    engine = _engine(_workflow(allow_interrupt_greeting=True))
    engine._mute_pipeline = True
    assert await engine.should_mute_user(UserStartedSpeakingFrame()) is True


@pytest.mark.asyncio
async def test_switch_on_still_mutes_queued_transition_speech():
    engine = _engine(_workflow(allow_interrupt_greeting=True))
    engine._queued_speech_mute_state = "waiting"
    assert await engine.should_mute_user(UserStartedSpeakingFrame()) is True


@pytest.mark.asyncio
async def test_switch_off_engine_callback_unchanged():
    # Without the switch the engine callback behaves exactly as before:
    # muted while the bot speaks on a node with allow_interrupt off.
    engine = _engine(_workflow(allow_interrupt_greeting=False))
    assert await engine.should_mute_user(BotStartedSpeakingFrame()) is True
    assert await engine.should_mute_user(BotStoppedSpeakingFrame()) is False
