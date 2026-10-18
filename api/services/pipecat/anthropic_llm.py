"""Direct Anthropic (Claude) LLM for voice agents.

Our pipecat's ``AnthropicLLMService`` predates the 4.6+ Claude models. Two
behaviours from newer pipecat are carried here so those models work on calls:

- **No assistant prefill.** Claude 4.6 and newer reject a request whose message
  list ends with an assistant message, which is what the context looks like
  right after a tool call. A minimal user turn (".") is appended for every
  model not known to support prefill.
- **Lowest thinking by default.** When the agent configures no ``thinking``,
  each known model gets the lowest thinking setting Anthropic documents for it,
  so a reply isn't delayed by up-front reasoning. Unlisted models run at
  Anthropic's own default.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.anthropic.llm import AnthropicLLMService

# Mirrors pipecat main's _THINKING_DEFAULTS: the request fields to send per
# model name when the caller sets no thinking.
THINKING_DEFAULTS: dict[str, dict[str, Any]] = {
    "claude-opus-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-sonnet-5": {"type": "disabled"},
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-opus-4-8": {"type": "disabled"},
    "claude-opus-4-7": {"type": "disabled"},
    "claude-opus-4-6": {"type": "disabled"},
    "claude-sonnet-4-6": {"type": "disabled"},
    "claude-opus-4-5": {"type": "disabled"},
    "claude-sonnet-4-5": {"type": "disabled"},
    "claude-haiku-4-5": {"type": "disabled"},
}

# Models that still accept assistant prefill (a frozen legacy set: Anthropic
# dropped prefill from the 4.6 generation on).
PREFILL_SUPPORTED_PREFIXES = (
    "claude-2",
    "claude-instant",
    "claude-3",
    "claude-opus-4-0",
    "claude-opus-4-1",
    "claude-sonnet-4-0",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
)


def model_name(model: str) -> str:
    """The bare model name: drops a Bedrock/Vertex wrapper and a dated snapshot.

    ``us.anthropic.claude-sonnet-5-20260630-v1:0``, ``claude-sonnet-5@20260630``
    and ``claude-sonnet-5-20260630`` all name ``claude-sonnet-5``.
    """
    name = model.lower().rsplit(".", 1)[-1].split("@", 1)[0]
    name = re.sub(r"-v\d+:\d+$", "", name)
    return re.sub(r"-\d{8}$", "", name)


def supports_prefill(model: str) -> bool:
    return model_name(model).startswith(PREFILL_SUPPORTED_PREFIXES)


def default_thinking(model: str) -> dict[str, Any] | None:
    return THINKING_DEFAULTS.get(model_name(model))


class DograhAnthropicLLMService(AnthropicLLMService):
    """``AnthropicLLMService`` with the 4.6+ prefill fix and thinking defaults."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        model = self._settings.model or ""
        if not self._settings.thinking:
            thinking = default_thinking(model)
            if thinking is not None:
                self._settings.thinking = self.ThinkingConfig(**thinking)
                logger.debug(f"{self}: thinking {thinking['type']} for {model}")

    def _get_llm_invocation_params(self, context: LLMContext):
        params = super()._get_llm_invocation_params(context)
        self._ensure_last_message_is_user(params["messages"])
        return params

    def _ensure_last_message_is_user(self, messages: list) -> None:
        if supports_prefill(self._settings.model or ""):
            return
        if messages and messages[-1].get("role") == "assistant":
            messages.append({"role": "user", "content": [{"type": "text", "text": "."}]})

    async def run_inference(self, context: LLMContext, **kwargs):
        # run_inference (post-call QA, extraction) builds its own params from
        # the adapter, so patch the adapter call it makes for the same fix.
        adapter = self.get_llm_adapter()
        original = adapter.get_llm_invocation_params

        def patched(*args, **inner):
            params = original(*args, **inner)
            self._ensure_last_message_is_user(params["messages"])
            return params

        adapter.get_llm_invocation_params = patched
        try:
            return await super().run_inference(context, **kwargs)
        finally:
            adapter.get_llm_invocation_params = original
