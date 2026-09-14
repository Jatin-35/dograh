"""Azure OpenAI chat client for the in-product AI assistant.

Plain `openai` SDK usage — mirrors the construction already proven in
`api/services/gen_ai/embedding/azure_openai_service.py`, plus retry/timeout
discipline ported from a later version of this codebase
(vox-fabrix `api/services/agent_orchestrator/llm.py`): a hard per-request
timeout so a half-open connection fails fast into the retry path rather than
hanging a turn forever, and bounded exponential-backoff retries.
"""

import asyncio
from typing import Any

import httpx
from loguru import logger
from openai import APIConnectionError, APITimeoutError, AsyncAzureOpenAI
from openai.types.chat import ChatCompletion

from api.constants import (
    WF_GEN_AZURE_OPENAI_API_KEY,
    WF_GEN_AZURE_OPENAI_API_VERSION,
    WF_GEN_AZURE_OPENAI_DEPLOYMENT,
    WF_GEN_AZURE_OPENAI_ENDPOINT,
)
from api.services.workflow_gen.config import is_workflow_gen_configured
from api.services.workflow_gen.transcript import PROVIDER_MAX_CONTENT_CHARS

REQUEST_TIMEOUT_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 15.0
MAX_LLM_RETRIES = 2

# Transient by nature: the identical request may well succeed a moment later.
# 408 request-timeout, 409 conflict, 429 rate-limited, plus anything 5xx.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})


class WorkflowGenNotConfiguredError(Exception):
    def __init__(self):
        super().__init__(
            "The in-product AI assistant isn't configured on this deployment "
            "(WF_GEN_LLM_PROVIDER / WF_GEN_AZURE_OPENAI_* env vars)."
        )


class WorkflowGenLLMError(Exception):
    """The LLM call failed after exhausting all retries."""


_client: AsyncAzureOpenAI | None = None


def _get_client() -> AsyncAzureOpenAI:
    global _client
    if not is_workflow_gen_configured():
        raise WorkflowGenNotConfiguredError()
    if _client is None:
        _client = AsyncAzureOpenAI(
            api_key=WF_GEN_AZURE_OPENAI_API_KEY,
            azure_endpoint=WF_GEN_AZURE_OPENAI_ENDPOINT,
            api_version=WF_GEN_AZURE_OPENAI_API_VERSION,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            max_retries=1,  # SDK-level, in addition to the app-level loop below
        )
    return _client


def _is_retryable(exc: Exception) -> bool:
    """Whether re-sending the identical request could plausibly succeed.

    Retrying a 400 cannot work — the request is malformed, and it will be just
    as malformed the second and third time. Doing it anyway burns the backoff
    budget (~7s) and buries the real cause under a retry-exhausted message.
    """
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        # An unrecognised failure is far more likely to be a bug here than a
        # blip worth re-sending. Surface it instead of hiding it behind retries.
        return False
    return status in _RETRYABLE_STATUS_CODES or status >= 500


def _oversized_content(messages: list[dict[str, Any]]) -> str | None:
    """Name a message that exceeds the provider's per-string limit.

    Belt-and-braces: `transcript.sanitize_transcript` caps everything far below
    this, so reaching it means a new path is appending uncapped content. Better
    to say exactly which message and how big than to let it come back as an
    opaque 400 three retries later.
    """
    for index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, str) and len(content) > PROVIDER_MAX_CONTENT_CHARS:
            return (
                f"messages[{index}] (role={message.get('role')!r}) is "
                f"{len(content):,} characters, over the provider's "
                f"{PROVIDER_MAX_CONTENT_CHARS:,} limit"
            )
    return None


async def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    retries: int = MAX_LLM_RETRIES,
) -> ChatCompletion:
    """Call the model with the given tools, bounded retries + backoff."""
    # Validated before the client is even constructed: an oversized request is
    # wrong regardless of how the deployment is configured.
    oversized = _oversized_content(messages)
    if oversized:
        logger.error(f"workflow_gen refusing oversized LLM request: {oversized}")
        raise WorkflowGenLLMError(
            f"The conversation contains a message too large to send ({oversized}). "
            "This is a bug — a tool result was stored without being capped."
        )

    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await client.chat.completions.create(
                model=WF_GEN_AZURE_OPENAI_DEPLOYMENT,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not _is_retryable(e):
                logger.warning(f"workflow_gen LLM call failed, not retryable: {e}")
                raise WorkflowGenLLMError(f"LLM call failed: {e}") from e
            logger.warning(f"workflow_gen LLM call attempt {attempt + 1} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(min(2**attempt, 5))
    raise WorkflowGenLLMError(f"LLM call failed after {retries + 1} attempts: {last_exc}")
