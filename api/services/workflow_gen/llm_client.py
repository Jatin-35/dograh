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
from openai import AsyncAzureOpenAI
from openai.types.chat import ChatCompletion

from api.constants import (
    WF_GEN_AZURE_OPENAI_API_KEY,
    WF_GEN_AZURE_OPENAI_API_VERSION,
    WF_GEN_AZURE_OPENAI_DEPLOYMENT,
    WF_GEN_AZURE_OPENAI_ENDPOINT,
)
from api.services.workflow_gen.config import is_workflow_gen_configured

REQUEST_TIMEOUT_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 15.0
MAX_LLM_RETRIES = 2


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


async def complete(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    retries: int = MAX_LLM_RETRIES,
) -> ChatCompletion:
    """Call the model with the given tools, bounded retries + backoff."""
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
            logger.warning(f"workflow_gen LLM call attempt {attempt + 1} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(min(2**attempt, 5))
    raise WorkflowGenLLMError(f"LLM call failed after {retries + 1} attempts: {last_exc}")
