"""GPT-6 models (gpt-6-luna) on the OpenAI LLM provider.

They are reasoning models. In Chat Completions, which the pipeline uses,
OpenAI only allows function calling with reasoning_effort "none" (the default
is "medium"), and "minimal" (what gpt-5 gets) is not one of their values. So a
gpt-6 request carries reasoning_effort "none" and no temperature; gpt-5 and
the older models are unchanged.
"""

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from api.services.configuration.registry import OpenAILLMService as OpenAIConfig
from api.services.pipecat.service_factory import (
    create_llm_service,
    create_llm_service_from_provider,
)
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext


def _params(model):
    service = create_llm_service_from_provider("openai", model, "sk-test")
    return service.build_chat_completion_params(
        {"messages": [{"role": "user", "content": "hi"}]}
    )


def test_gpt6_luna_asks_for_no_reasoning_and_sends_no_temperature():
    params = _params("gpt-6-luna")
    assert params["reasoning_effort"] == "none"
    assert params["model"] == "gpt-6-luna"
    assert not isinstance(params.get("temperature"), (int, float))
    assert "verbosity" not in params


def test_every_gpt6_variant_gets_the_same_treatment():
    for model in ("gpt-6-luna", "gpt-6-luna-20260922", "gpt-6-sol"):
        assert _params(model)["reasoning_effort"] == "none", model


def test_gpt5_and_older_models_are_unchanged():
    gpt5 = _params("gpt-5-mini")
    assert (gpt5["reasoning_effort"], gpt5["verbosity"]) == ("minimal", "low")
    gpt41 = _params("gpt-4.1")
    assert gpt41["temperature"] == 0.1 and "reasoning_effort" not in gpt41


def test_newer_gpt5_models_get_none_not_minimal():
    """GPT-5.1+ (e.g. Bedrock's in.openai.gpt-5.6-luna) reject "minimal"."""
    for model in ("in.openai.gpt-5.6-luna", "gpt-5.1", "gpt-5.2-mini"):
        params = _params(model)
        assert (params["reasoning_effort"], params["verbosity"]) == ("none", "low"), model
    for model in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
        assert _params(model)["reasoning_effort"] == "minimal", model


def test_the_model_is_offered_in_the_settings_dropdown():
    examples = OpenAIConfig.model_json_schema()["properties"]["model"]["examples"]
    assert "gpt-6-luna" in examples


def test_an_agents_saved_settings_build_it_with_a_custom_base_url():
    user_config = SimpleNamespace(
        llm=OpenAIConfig(
            api_key="sk-test", model="gpt-6-luna", base_url="https://api.openai.com/v1"
        )
    )
    service = create_llm_service(user_config)
    assert service._settings.extra == {"reasoning_effort": "none"}
    assert str(service._client.base_url).rstrip("/") == "https://api.openai.com/v1"


@pytest.mark.asyncio
async def test_the_request_openai_receives_allows_tool_calls():
    """End to end to the HTTP body, with a tool, as an agent node sends it."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-6-luna",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                },
            },
        )

    service = create_llm_service_from_provider("openai", "gpt-6-luna", "sk-test")
    service._client = openai.AsyncOpenAI(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    tool = FunctionSchema(
        name="end_call", description="End the call", properties={}, required=[]
    )
    context = LLMContext(
        messages=[{"role": "user", "content": "bye"}],
        tools=ToolsSchema(standard_tools=[tool]),
    )
    assert await service.run_inference(context) == "ok"

    (body,) = seen
    assert body["model"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "none"  # tools are allowed only with none
    assert body["tools"][0]["function"]["name"] == "end_call"
    assert "temperature" not in body


# ─── through Amazon Bedrock's OpenAI-compatible endpoint ───────────────────

BEDROCK = "https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1"


def test_the_bedrock_model_id_gets_the_gpt6_treatment():
    """In ap-south-1 GPT-6 Luna is only reachable as the global profile."""
    for model in (
        "global.openai.gpt-6-luna",
        "us.openai.gpt-6-luna",
        "openai.gpt-6-luna",
    ):
        params = _params(model)
        assert params["reasoning_effort"] == "none", model
        assert not isinstance(params.get("temperature"), (int, float)), model


def test_a_bedrock_config_points_the_client_at_bedrock():
    user_config = SimpleNamespace(
        llm=OpenAIConfig(
            api_key="ABSKtest", model="global.openai.gpt-6-luna", base_url=BEDROCK
        )
    )
    service = create_llm_service(user_config)
    assert str(service._client.base_url).rstrip("/") == BEDROCK
    assert service._settings.extra == {"reasoning_effort": "none"}


def _fake_client(monkeypatch, *, list_error=None, chat_error=None):
    from api.services.configuration import check_validity

    calls = {}

    class Models:
        def list(self):
            calls["list"] = True
            if list_error:
                raise list_error

    class Completions:
        def create(self, **kwargs):
            calls["chat"] = kwargs
            if chat_error:
                raise chat_error

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.models = Models()
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr(check_validity.openai, "OpenAI", FakeOpenAI)
    return calls


def _status_error(cls, status, message):
    request = httpx.Request("POST", BEDROCK + "/chat/completions")
    response = httpx.Response(status, request=request)
    return cls(message, response=response, body={"error": {"message": message}})


def test_saving_a_bedrock_key_checks_it_with_a_tiny_chat_request(monkeypatch):
    from api.services.configuration.check_validity import UserConfigurationValidator

    not_found = _status_error(openai.NotFoundError, 404, "UnknownOperationException")
    calls = _fake_client(monkeypatch, list_error=not_found)
    cfg = OpenAIConfig(
        api_key="ABSKtest", model="global.openai.gpt-6-luna", base_url=BEDROCK
    )
    assert UserConfigurationValidator()._validate_service(cfg, "llm") == []
    assert calls["client"]["base_url"] == BEDROCK
    assert calls["chat"]["model"] == "global.openai.gpt-6-luna"
    assert calls["chat"]["max_completion_tokens"] == 16
    assert calls["chat"]["reasoning_effort"] == "none"


def test_missing_bedrock_model_access_shows_aws_reason(monkeypatch):
    from api.services.configuration.check_validity import UserConfigurationValidator

    reason = (
        "Model access is denied due to IAM user or service role is not authorized "
        "to perform the required AWS Marketplace actions"
    )
    _fake_client(
        monkeypatch,
        list_error=_status_error(
            openai.NotFoundError, 404, "UnknownOperationException"
        ),
        chat_error=_status_error(openai.PermissionDeniedError, 403, reason),
    )
    cfg = OpenAIConfig(
        api_key="ABSKtest", model="global.openai.gpt-6-luna", base_url=BEDROCK
    )
    (status,) = UserConfigurationValidator()._validate_service(cfg, "llm")
    assert "accepted the key but refused access" in status["message"]
    assert "AWS Marketplace" in status["message"]


def test_a_wrong_bedrock_key_is_still_reported_as_invalid(monkeypatch):
    from api.services.configuration.check_validity import UserConfigurationValidator

    _fake_client(
        monkeypatch,
        list_error=_status_error(
            openai.NotFoundError, 404, "UnknownOperationException"
        ),
        chat_error=_status_error(
            openai.AuthenticationError, 401, "Invalid API Key format"
        ),
    )
    cfg = OpenAIConfig(
        api_key="ABSKwrong", model="global.openai.gpt-6-luna", base_url=BEDROCK
    )
    (status,) = UserConfigurationValidator()._validate_service(cfg, "llm")
    assert "Invalid OpenAI API key" in status["message"]


def test_real_openai_still_validates_by_listing_models(monkeypatch):
    from api.services.configuration.check_validity import UserConfigurationValidator

    calls = _fake_client(monkeypatch)
    cfg = OpenAIConfig(api_key="sk-test", model="gpt-6-luna")
    assert UserConfigurationValidator()._validate_service(cfg, "llm") == []
    assert calls.get("list") and "chat" not in calls  # no paid request
