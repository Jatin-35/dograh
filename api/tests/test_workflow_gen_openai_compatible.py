"""Scout on an OpenAI-compatible endpoint (Amazon Bedrock's GPT-6 Luna).

WF_GEN_LLM_PROVIDER=openai_compatible with a key, base URL and model. GPT-6
models get reasoning_effort "none": in Chat Completions they only call
functions with it, and every Scout request carries tools. Azure is unchanged.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import AsyncAzureOpenAI, AsyncOpenAI

from api.services.workflow_gen import config, llm_client

BEDROCK = "https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1"
MESSAGES = [{"role": "user", "content": "add a transfer tool"}]
TOOLS = [{"type": "function", "function": {"name": "get_workflow", "parameters": {}}}]


@pytest.fixture
def settings(monkeypatch):
    """Point both modules at a given provider setup; reset the cached client."""

    def apply(**values):
        for name, value in values.items():
            for module in (config, llm_client):
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, value)
        monkeypatch.setattr(llm_client, "_client", None)

    yield apply
    llm_client._client = None


def bedrock(settings, **overrides):
    values = {
        "WF_GEN_LLM_PROVIDER": "openai_compatible",
        "WF_GEN_OPENAI_API_KEY": "ABSKtest",
        "WF_GEN_OPENAI_BASE_URL": BEDROCK,
        "WF_GEN_OPENAI_MODEL": "global.openai.gpt-6-luna",
        "WF_GEN_OPENAI_REASONING_EFFORT": None,
    }
    values.update(overrides)
    settings(**values)


def test_bedrock_settings_turn_scout_on(settings):
    bedrock(settings)
    assert config.is_workflow_gen_configured() is True


@pytest.mark.parametrize(
    "missing",
    ["WF_GEN_OPENAI_API_KEY", "WF_GEN_OPENAI_BASE_URL", "WF_GEN_OPENAI_MODEL"],
)
def test_each_openai_compatible_setting_is_required(settings, missing):
    bedrock(settings, **{missing: None})
    assert config.is_workflow_gen_configured() is False


def test_an_unknown_provider_keeps_scout_off(settings):
    settings(WF_GEN_LLM_PROVIDER="bedrock_native")
    assert config.is_workflow_gen_configured() is False


def test_the_client_talks_to_the_configured_endpoint(settings):
    bedrock(settings)
    client = llm_client._get_client()
    assert isinstance(client, AsyncOpenAI) and not isinstance(client, AsyncAzureOpenAI)
    assert str(client.base_url).rstrip("/") == BEDROCK
    assert client.api_key == "ABSKtest"
    assert client.max_retries == 0  # the retry loop in complete() owns retrying


async def _sent(settings_apply) -> dict:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value="ok"))
        )
    )
    llm_client._client = client
    assert await llm_client.complete(MESSAGES, TOOLS) == "ok"
    return client.chat.completions.create.await_args.kwargs


@pytest.mark.asyncio
async def test_gpt6_requests_ask_for_no_reasoning_so_tools_work(settings):
    bedrock(settings)
    kwargs = await _sent(settings)
    assert kwargs["model"] == "global.openai.gpt-6-luna"
    assert kwargs["reasoning_effort"] == "none"
    assert kwargs["tools"] == TOOLS and kwargs["tool_choice"] == "auto"
    assert "temperature" not in kwargs


@pytest.mark.asyncio
async def test_the_reasoning_effort_can_be_set_explicitly(settings):
    bedrock(settings, WF_GEN_OPENAI_REASONING_EFFORT="low")
    assert (await _sent(settings))["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_a_non_gpt6_model_gets_no_reasoning_field(settings):
    bedrock(settings, WF_GEN_OPENAI_MODEL="openai.gpt-oss-120b-1:0")
    assert "reasoning_effort" not in await _sent(settings)


@pytest.mark.asyncio
async def test_azure_is_unchanged(settings):
    settings(
        WF_GEN_LLM_PROVIDER="azure_openai",
        WF_GEN_AZURE_OPENAI_API_KEY="k",
        WF_GEN_AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com",
        WF_GEN_AZURE_OPENAI_DEPLOYMENT="gpt-5.5",
        WF_GEN_AZURE_OPENAI_API_VERSION="2025-04-01-preview",
    )
    assert config.is_workflow_gen_configured() is True
    assert isinstance(llm_client._get_client(), AsyncAzureOpenAI)
    llm_client._client = None
    kwargs = await _sent(settings)
    assert kwargs["model"] == "gpt-5.5" and "reasoning_effort" not in kwargs
