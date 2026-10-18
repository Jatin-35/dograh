"""Tests for the direct Anthropic (Claude) LLM provider."""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from pydantic import TypeAdapter

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    AnthropicLLMConfiguration,
    LLMConfig,
    ServiceProviders,
)
from api.services.pipecat.anthropic_llm import (
    DograhAnthropicLLMService,
    default_thinking,
    model_name,
    supports_prefill,
)
from api.services.pipecat.service_factory import create_llm_service_from_provider

# ── Settings / dropdown ───────────────────────────────────────────────


def test_anthropic_is_an_llm_provider_with_a_fast_default():
    cfg = TypeAdapter(LLMConfig).validate_python(
        {"provider": "anthropic", "api_key": "sk-ant-test"}
    )
    assert isinstance(cfg, AnthropicLLMConfiguration)
    assert cfg.model == "claude-haiku-5-5"
    schema = AnthropicLLMConfiguration.model_json_schema()
    examples = schema["properties"]["model"]["examples"]
    assert "claude-haiku-5-5" in examples and "claude-sonnet-5-5" in examples


def test_anthropic_accepts_a_custom_model_id():
    cfg = AnthropicLLMConfiguration(api_key="k", model="claude-opus-5-5-20261001")
    assert cfg.model == "claude-opus-5-5-20261001"


# ── Model names, thinking and prefill rules ──────────────────────────


@pytest.mark.parametrize(
    "model_id,name",
    [
        ("claude-sonnet-5-20260630", "claude-sonnet-5"),
        ("claude-sonnet-5@20260630", "claude-sonnet-5"),
        ("us.anthropic.claude-sonnet-5-20260630-v1:0", "claude-sonnet-5"),
        ("claude-haiku-5-5", "claude-haiku-5-5"),
    ],
)
def test_model_name_strips_wrappers_and_snapshots(model_id, name):
    assert model_name(model_id) == name


def test_thinking_defaults_follow_newer_pipecat():
    assert default_thinking("claude-haiku-5-5") == {"type": "disabled"}
    assert default_thinking("claude-sonnet-5-5") == {"type": "between_tools"}
    assert default_thinking("claude-haiku-4-5-20251001") == {"type": "disabled"}
    assert default_thinking("claude-mystery-9") is None


@pytest.mark.parametrize(
    "model,prefill",
    [
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-3-5-haiku-latest", True),
        ("claude-sonnet-4-6", False),
        ("claude-haiku-5-5", False),
        ("claude-opus-5-5", False),
    ],
)
def test_prefill_support_is_limited_to_legacy_models(model, prefill):
    assert supports_prefill(model) is prefill


# ── Factory ───────────────────────────────────────────────────────────


def test_factory_builds_the_dograh_anthropic_service():
    svc = create_llm_service_from_provider(
        ServiceProviders.ANTHROPIC.value, "claude-haiku-5-5", "sk-ant-test"
    )
    assert isinstance(svc, DograhAnthropicLLMService)
    assert svc._settings.model == "claude-haiku-5-5"
    assert svc._settings.enable_prompt_caching is True
    assert svc._settings.thinking.type == "disabled"


def test_unlisted_model_runs_at_anthropic_default_thinking():
    svc = create_llm_service_from_provider("anthropic", "claude-mystery-9", "k")
    assert not svc._settings.thinking


def test_explicit_thinking_is_kept():
    svc = DograhAnthropicLLMService(
        api_key="k",
        settings=DograhAnthropicLLMService.Settings(
            model="claude-haiku-5-5",
            thinking=DograhAnthropicLLMService.ThinkingConfig(
                type="enabled", budget_tokens=2048
            ),
        ),
    )
    assert svc._settings.thinking.type == "enabled"


# ── Requests ending on an assistant turn (after a tool call) ──────────


def _context_ending_with_assistant():
    return LLMContext(
        messages=[
            {"role": "user", "content": "Mujhe tank chahiye"},
            {"role": "assistant", "content": "Ji, aapka city aur state kaunsa hai?"},
        ]
    )


def test_newer_claude_gets_a_trailing_user_turn():
    svc = DograhAnthropicLLMService(
        api_key="k", settings=DograhAnthropicLLMService.Settings(model="claude-haiku-5-5")
    )
    messages = svc._get_llm_invocation_params(_context_ending_with_assistant())["messages"]
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][0]["text"] == "."


def test_legacy_claude_keeps_the_assistant_last():
    svc = DograhAnthropicLLMService(
        api_key="k", settings=DograhAnthropicLLMService.Settings(model="claude-haiku-4-5")
    )
    messages = svc._get_llm_invocation_params(_context_ending_with_assistant())["messages"]
    assert messages[-1]["role"] == "assistant"


def test_a_context_ending_with_the_user_is_unchanged():
    svc = DograhAnthropicLLMService(
        api_key="k", settings=DograhAnthropicLLMService.Settings(model="claude-haiku-5-5")
    )
    ctx = LLMContext(messages=[{"role": "user", "content": "Hello"}])
    messages = svc._get_llm_invocation_params(ctx)["messages"]
    assert len(messages) == 1 and messages[0]["role"] == "user"


@pytest.mark.asyncio
async def test_run_inference_sends_the_fix_and_thinking_off():
    sent = {}

    class Messages:
        async def create(self, **params):
            sent.update(params)
            return SimpleNamespace(content=[SimpleNamespace(text="ok")])

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    svc = DograhAnthropicLLMService(
        api_key="k",
        client=client,
        settings=DograhAnthropicLLMService.Settings(model="claude-haiku-5-5"),
    )
    assert await svc.run_inference(_context_ending_with_assistant()) == "ok"
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["messages"][-1]["role"] == "user"
    assert sent["thinking"] == {"type": "disabled"}
    # The adapter is restored afterwards.
    messages = svc.get_llm_adapter().get_llm_invocation_params(
        _context_ending_with_assistant(), enable_prompt_caching=False, system_instruction=None
    )["messages"]
    assert messages[-1]["role"] == "assistant"


# ── Key check on save ─────────────────────────────────────────────────


def _validate(api_key="sk-ant-test"):
    return UserConfigurationValidator()._validate_service(
        AnthropicLLMConfiguration(api_key=api_key), "llm"
    )


def _resp(status):
    return httpx.Response(status, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))


def test_valid_key_saves():
    with patch("api.services.configuration.check_validity.httpx.get", return_value=_resp(200)):
        assert _validate() == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_blocks_the_save(status):
    with patch("api.services.configuration.check_validity.httpx.get", return_value=_resp(status)):
        (err,) = _validate()
    assert "Invalid Anthropic API key" in err["message"]


def test_other_statuses_do_not_block_the_save():
    with patch("api.services.configuration.check_validity.httpx.get", return_value=_resp(529)):
        assert _validate() == []


def test_unreachable_api_is_reported():
    with patch(
        "api.services.configuration.check_validity.httpx.get",
        side_effect=httpx.ConnectError("down"),
    ):
        (err,) = _validate()
    assert "Could not connect to the Anthropic API" in err["message"]
