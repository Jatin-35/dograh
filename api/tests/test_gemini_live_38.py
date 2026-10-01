"""Gemini 3.8 Live as its own provider, isolated from the 3.1 path.

On a Vectus call, 3.8 on the 3.1 code invented the Area Manager instead of
waiting for ``vectus_area_manager_lookup``: 3.8 runs tools NON_BLOCKING unless
a declaration says BLOCKING, and the pinned pipecat never declares BLOCKING.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.configuration.registry import (
    REALTIME_PROVIDERS,
    GeminiLive38LLMConfiguration,
    ServiceProviders,
)
from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService
from api.services.pipecat.realtime.gemini_live_38 import (
    DograhGemini38LiveLLMService,
    tag_tool_behaviors,
)


class _Test38(DograhGemini38LiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


class _Test31(DograhGeminiLiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


def _service(cls, model):
    service = cls(api_key="k", settings=cls.Settings(model=model))
    service.push_error = AsyncMock()
    return service


def _tools():
    return [
        {
            "function_declarations": [
                {"name": "vectus_area_manager_lookup", "parameters": {}},
                {"name": "move_to_main_agenda", "parameters": {}},
                {"name": "log_lead_in_background", "parameters": {}},
            ]
        }
    ]


def _session():
    return SimpleNamespace(send_tool_response=AsyncMock(), send_realtime_input=AsyncMock())


async def _noop(params):
    pass


# ─── tool behaviour ────────────────────────────────────────────────────────


def test_sync_tools_block_and_async_tools_do_not():
    tools = _tools()
    tagged = tag_tool_behaviors(tools, lambda name: name == "log_lead_in_background")
    behaviors = {d["name"]: d["behavior"] for d in tools[0]["function_declarations"]}
    assert behaviors == {
        "vectus_area_manager_lookup": "BLOCKING",
        "move_to_main_agenda": "BLOCKING",
        "log_lead_in_background": "NON_BLOCKING",
    }
    assert tagged == 3


def test_odd_tool_shapes_are_left_alone():
    assert tag_tool_behaviors(None, lambda n: False) == 0
    odd = [object(), {"x": 1}, {"function_declarations": "?"}]
    assert tag_tool_behaviors(odd, lambda n: False) == 0


@pytest.mark.asyncio
async def test_the_session_config_carries_the_behaviours():
    service = _service(_Test38, "gemini-3.8-live")
    service.register_function("log_lead_in_background", _noop, cancel_on_interruption=False)
    service.register_function("vectus_area_manager_lookup", _noop)
    config = SimpleNamespace(tools=_tools())
    with patch.object(
        DograhGeminiLiveLLMService, "_connection_task_handler", AsyncMock()
    ) as parent:
        await service._connection_task_handler(config)
    parent.assert_awaited_once()
    decls = {d["name"]: d.get("behavior") for d in config.tools[0]["function_declarations"]}
    assert decls["vectus_area_manager_lookup"] == "BLOCKING"
    assert decls["log_lead_in_background"] == "NON_BLOCKING"


# ─── tool results ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_result_is_sent_without_the_old_3x_nudge():
    service = _service(_Test38, "gemini-3.8-live")
    session = _session()
    service._session = session
    service._disconnecting = False

    assert await service._tool_result("id-1", "vectus_area_manager_lookup", {"status": "found"})

    sent = session.send_tool_response.await_args.kwargs["function_responses"]
    assert (sent.name, sent.id, sent.response) == (
        "vectus_area_manager_lookup",
        "id-1",
        {"status": "found"},
    )
    session.send_realtime_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_async_tool_result_asks_to_wait_until_idle():
    service = _service(_Test38, "gemini-3.8-live")
    service.register_function("log_lead_in_background", _noop, cancel_on_interruption=False)
    session = _session()
    service._session = session
    service._disconnecting = False
    await service._tool_result("id-2", "log_lead_in_background", {"ok": True})
    sent = session.send_tool_response.await_args.kwargs["function_responses"]
    assert sent.response == {"ok": True, "scheduling": "WHEN_IDLE"}


@pytest.mark.asyncio
async def test_a_result_during_reconnect_is_queued():
    service = _service(_Test38, "gemini-3.8-live")
    service._session = None
    assert await service._tool_result("id-3", "vectus_area_manager_lookup", {"a": 1}) is False
    assert service._pending_tool_results["id-3"] == ("vectus_area_manager_lookup", {"a": 1})


# ─── the 3.1 path is untouched ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gemini_31_keeps_its_behaviour():
    service = _service(_Test31, "gemini-3.1-flash-live-preview")
    assert service._supports_non_blocking_tools is False
    config = SimpleNamespace(tools=_tools())
    with patch(
        "pipecat.services.google.gemini_live.llm.GeminiLiveLLMService._connection_task_handler",
        AsyncMock(),
    ):
        await service._connection_task_handler(config)
    assert all("behavior" not in d for d in config.tools[0]["function_declarations"])

    session = _session()
    service._session = session
    service._disconnecting = False
    await service._tool_result("id-4", "vectus_area_manager_lookup", {"status": "found"})
    session.send_realtime_input.assert_awaited_once_with(text=" ")  # 3.1 still nudges


# ─── provider wiring ───────────────────────────────────────────────────────


def test_gemini_38_is_its_own_realtime_provider():
    config = GeminiLive38LLMConfiguration(api_key="k")
    assert config.provider == ServiceProviders.GEMINI_LIVE_38
    assert config.model == "gemini-3.8-live"
    assert ServiceProviders.GEMINI_LIVE_38.value in REALTIME_PROVIDERS


def test_the_factory_builds_the_38_service_only_for_its_provider():
    from api.services.pipecat.service_factory import create_realtime_llm_service

    def build(provider, model):
        realtime = SimpleNamespace(
            provider=provider, model=model, api_key="k", voice="Puck", language="hi"
        )
        return create_realtime_llm_service(SimpleNamespace(realtime=realtime), None)

    assert type(build("gemini_live_38", "gemini-3.8-live")) is DograhGemini38LiveLLMService
    assert (
        type(build("google_realtime", "gemini-3.1-flash-live-preview"))
        is DograhGeminiLiveLLMService
    )
