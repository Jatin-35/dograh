"""Two Gemini Live fixes ported from pipecat main.

1. Tool history is sent as text when a session is seeded (pipecat 48c0e281,
   ec11fef1). A node change opens a fresh session seeded from the context,
   which by then holds ``move_to_main_agenda`` and lookups with their results;
   Gemini Live's send_client_content accepts only text and media parts.
2. The end of a user turn does nothing that needs a session while there is
   none (pipecat b3f3730e). A caller who stops speaking during the reconnect a
   node change makes used to raise on ``None.send_client_content`` and lose the
   signal that tells the new session to use its context.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services.pipecat.gemini_json_schema_adapter import (
    DograhGeminiJSONSchemaAdapter,
    DograhGeminiLiveJSONSchemaAdapter,
)
from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService
from api.services.pipecat.realtime.gemini_live_38 import DograhGemini38LiveLLMService
from api.services.pipecat.service_factory import DograhGoogleLLMService


def _call(call_id, name, arguments="{}"):
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


# What a Vectus call's context holds after the opening node hands over.
NODE_CHANGE_CONTEXT = [
    {"role": "assistant", "content": "Namaskar, main Vectus Customer Care se Aastha bol rahi hoon."},
    {"role": "user", "content": "Haan ji, mujhe water tank chahiye."},
    _call("call-1", "move_to_main_agenda"),
    {"role": "tool", "tool_call_id": "call-1", "content": '{"status": "done"}'},
    _call("call-2", "vectus_area_manager_lookup", '{"city": "Ranchi"}'),
    {
        "role": "tool",
        "tool_call_id": "call-2",
        "content": '{"area_manager_name": "Rahul", "area_manager_number": "9000000000"}',
    },
    {"role": "user", "content": "Ranchi mein."},
]


def _messages(adapter, messages):
    return adapter.get_llm_invocation_params(LLMContext(messages=messages))["messages"]


def _parts(messages):
    return [part for message in messages for part in message.parts]


class TestToolHistoryAsText:
    def test_no_function_parts_reach_a_seeded_session(self):
        parts = _parts(_messages(DograhGeminiLiveJSONSchemaAdapter(), NODE_CHANGE_CONTEXT))
        assert parts
        assert all(part.function_call is None for part in parts)
        assert all(part.function_response is None for part in parts)
        text = " ".join(part.text for part in parts)
        assert "move_to_main_agenda" in text
        assert "vectus_area_manager_lookup" in text and "Ranchi" in text
        assert "Rahul" in text and "9000000000" in text  # the lookup result survives

    def test_the_call_is_a_model_turn_and_its_result_a_user_turn(self):
        messages = _messages(DograhGeminiLiveJSONSchemaAdapter(), NODE_CHANGE_CONTEXT[2:4])
        assert [m.role for m in messages] == ["model", "user"]
        assert "[Called function move_to_main_agenda" in messages[0].parts[0].text
        assert "[Function move_to_main_agenda returned" in messages[1].parts[0].text

    def test_parallel_calls_become_one_turn(self):
        both = _call("a", "one")
        both["tool_calls"] += _call("b", "two")["tool_calls"]
        messages = _messages(DograhGeminiLiveJSONSchemaAdapter(), [both])
        assert len(messages) == 1 and len(messages[0].parts) == 1
        assert "one" in messages[0].parts[0].text and "two" in messages[0].parts[0].text

    def test_a_result_without_its_call_gets_the_fallback_name(self):
        messages = _messages(
            DograhGeminiLiveJSONSchemaAdapter(),
            [{"role": "tool", "tool_call_id": "gone", "content": "42"}],
        )
        assert messages[0].parts[0].text == "[Function tool_call_result returned 42]"

    def test_plain_conversation_is_unchanged(self):
        plain = [m for m in NODE_CHANGE_CONTEXT if "tool_calls" not in m and m["role"] != "tool"]
        live = _messages(DograhGeminiLiveJSONSchemaAdapter(), plain)
        base = _messages(GeminiLLMAdapter(), plain)
        assert [(m.role, m.parts[0].text) for m in live] == [
            (m.role, m.parts[0].text) for m in base
        ]

    def test_the_regular_gemini_llm_keeps_real_function_parts(self):
        assert DograhGoogleLLMService.adapter_class is DograhGeminiJSONSchemaAdapter
        parts = _parts(_messages(DograhGeminiJSONSchemaAdapter(), NODE_CHANGE_CONTEXT))
        assert any(part.function_call is not None for part in parts)

    def test_every_live_service_uses_it(self):
        for service in (DograhGeminiLiveLLMService, DograhGemini38LiveLLMService):
            assert service.adapter_class is DograhGeminiLiveJSONSchemaAdapter


class _TestService(DograhGeminiLiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


def _service(session):
    service = _TestService(
        api_key="test-key",
        settings=_TestService.Settings(model="models/gemini-3.1-flash-live-preview"),
    )
    service.start_ttfb_metrics = AsyncMock()
    service._session = session
    service._user_is_speaking = True
    return service


def _session():
    return SimpleNamespace(send_client_content=AsyncMock(), send_realtime_input=AsyncMock())


class TestTurnEndWithoutSession:
    @pytest.mark.asyncio
    async def test_no_session_does_not_raise_and_keeps_the_signal(self):
        service = _service(session=None)
        service._needs_initial_turn_complete_message = True

        await service._handle_user_stopped_speaking(None)

        assert service._user_is_speaking is False
        assert service._needs_initial_turn_complete_message is True  # sent next turn

    @pytest.mark.asyncio
    async def test_with_a_session_the_seeded_context_signal_is_sent_once(self):
        session = _session()
        service = _service(session=session)
        service._needs_initial_turn_complete_message = True

        await service._handle_user_stopped_speaking(None)
        await service._handle_user_stopped_speaking(None)

        session.send_client_content.assert_awaited_once_with(turn_complete=True)
        assert service._needs_initial_turn_complete_message is False

    @pytest.mark.asyncio
    async def test_the_signal_reaches_the_session_once_it_exists(self):
        service = _service(session=None)
        service._needs_initial_turn_complete_message = True
        await service._handle_user_stopped_speaking(None)  # mid-reconnect

        service._session = _session()
        await service._handle_user_stopped_speaking(None)  # next turn

        service._session.send_client_content.assert_awaited_once_with(turn_complete=True)

    @pytest.mark.asyncio
    async def test_client_vad_still_sends_activity_end(self):
        session = _session()
        service = _service(session=session)
        service._vad_disabled = True
        service._ready_for_realtime_input = True
        service._needs_initial_turn_complete_message = False

        await service._handle_user_stopped_speaking(None)

        assert "activity_end" in session.send_realtime_input.await_args.kwargs
        session.send_client_content.assert_not_awaited()


class TestSpokenTextBesideACall:
    def test_what_the_bot_said_with_the_call_is_kept(self):
        said = _call("c", "vectus_area_manager_lookup", '{"city": "Durg"}')
        said["content"] = "Ek second, main check karti hoon."
        messages = _messages(DograhGeminiLiveJSONSchemaAdapter(), [said])
        texts = [m.parts[0].text for m in messages]
        assert texts[0] == "Ek second, main check karti hoon."
        assert "vectus_area_manager_lookup" in texts[-1]
        assert all(p.function_call is None for p in _parts(messages))


class _Live38(DograhGemini38LiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


def _live38(context_messages):
    service = _Live38(api_key="k", settings=_Live38.Settings(model="gemini-3.8-live"))
    service.start_ttfb_metrics = AsyncMock()
    service._context = LLMContext(messages=context_messages)
    service._session = _session()
    return service


class TestNodeChangeReseedEndToEnd:
    """Drives the real 3.8 service's seeding and records what reaches Google."""

    @pytest.mark.asyncio
    async def test_reseed_after_a_node_change_sends_only_text(self):
        service = _live38(NODE_CHANGE_CONTEXT)

        await service._create_initial_response(for_reconnect=True)

        kwargs = service._session.send_client_content.await_args.kwargs
        turns = kwargs["turns"]
        parts = _parts(turns)
        assert all(p.function_call is None and p.function_response is None for p in parts)
        assert any("9000000000" in (p.text or "") for p in parts)
        assert turns[-1].parts[0].text == "Ranchi mein."  # the caller's last words survive
        # 3.x reconnect seeds silently: no new turn is triggered.
        assert kwargs["turn_complete"] is False
        service._session.send_realtime_input.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_roles_alternate_as_gemini_expects(self):
        service = _live38(NODE_CHANGE_CONTEXT)
        await service._create_initial_response(for_reconnect=True)
        roles = [t.role for t in service._session.send_client_content.await_args.kwargs["turns"]]
        assert set(roles) <= {"user", "model"}
        assert roles[0] == "model" and roles[-1] == "user"

    @pytest.mark.asyncio
    async def test_prerecorded_greeting_seed_has_no_function_parts(self):
        service = _live38(NODE_CHANGE_CONTEXT)
        service._handled_initial_context = True

        await service._create_prerecorded_greeting_response("Namaskar ji")

        turns = service._session.send_client_content.await_args.kwargs["turns"]
        assert all(p.function_call is None for p in _parts(turns))
        assert turns[-1].parts[0].text == "Namaskar ji"

    @pytest.mark.asyncio
    async def test_tools_and_system_instruction_are_unaffected(self):
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema

        tools = ToolsSchema(
            standard_tools=[
                FunctionSchema(
                    name="vectus_area_manager_lookup",
                    description="Find the area manager",
                    properties={"city": {"type": "string"}},
                    required=["city"],
                )
            ]
        )
        context = LLMContext(messages=NODE_CHANGE_CONTEXT, tools=tools)
        live = DograhGeminiLiveJSONSchemaAdapter().get_llm_invocation_params(
            context, system_instruction="Be Aastha."
        )
        base = DograhGeminiJSONSchemaAdapter().get_llm_invocation_params(
            context, system_instruction="Be Aastha."
        )
        assert live["tools"] == base["tools"]
        assert live["system_instruction"] == base["system_instruction"]
