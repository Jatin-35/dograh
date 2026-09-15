"""Retry and timeout discipline for the in-product assistant's LLM calls.

Written against a real production failure: asking the assistant to rewrite a
large node's prompt returned "LLM call failed after 3 attempts: Request timed
out." after several minutes of apparent hanging.

Nothing was down. The request was non-streaming with a 90s read timeout, so the
clock measured how long the model took to *generate* — and rewriting a long
prompt on top of a near-full transcript does not finish in 90s. Every attempt
was therefore killed mid-generation at exactly the same point, and because the
SDK had its own `max_retries=1` on top of the loop's 3 attempts, the user waited
through up to six doomed requests before being told the request "timed out".

The three properties these tests pin:
  * a size-driven timeout is not re-sent over and over (it recurs by nature),
  * a genuinely transient failure still gets the full retry budget,
  * only one layer retries, so the worst-case wait is the one that was chosen.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

from api.services.workflow_gen import llm_client
from api.services.workflow_gen.llm_client import (
    MAX_TIMEOUT_RETRIES,
    WorkflowGenLLMError,
    complete,
)

MESSAGES = [{"role": "user", "content": "rewrite the global node"}]
TOOLS: list[dict] = []


def _timeout() -> APITimeoutError:
    return APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


def _rate_limited() -> Exception:
    exc = Exception("rate limited")
    exc.status_code = 429  # type: ignore[attr-defined]
    return exc


def _bad_request() -> Exception:
    exc = Exception("invalid tool schema")
    exc.status_code = 400  # type: ignore[attr-defined]
    return exc


def _client_raising(*errors: Exception) -> AsyncMock:
    """A stand-in Azure client whose create() raises `errors` in order."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=list(errors))
    return client


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Backoff is real time; the point here is attempt counts, not waiting."""
    monkeypatch.setattr(llm_client.asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
async def test_a_timeout_is_not_retried_to_exhaustion():
    """The production bug. A timeout caused by the size of the work recurs on
    an identical request, so re-sending it three times only multiplies the
    wait before the same failure."""
    client = _client_raising(_timeout(), _timeout(), _timeout())

    with patch.object(llm_client, "_get_client", return_value=client):
        with pytest.raises(WorkflowGenLLMError):
            await complete(MESSAGES, TOOLS)

    assert client.chat.completions.create.await_count == MAX_TIMEOUT_RETRIES + 1 == 2


@pytest.mark.asyncio
async def test_a_timeout_still_gets_one_retry_for_the_transient_case():
    """Not zero retries: a single timeout can be a real blip, and the second
    attempt should be allowed to succeed."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(side_effect=[_timeout(), "completion"])

    with patch.object(llm_client, "_get_client", return_value=client):
        result = await complete(MESSAGES, TOOLS)

    assert result == "completion"
    assert client.chat.completions.create.await_count == 2


@pytest.mark.asyncio
async def test_the_timeout_message_names_the_real_cause():
    """"Request timed out" reads like the service is down and sends people
    looking at infrastructure. The actionable fact is that this piece of work
    is too large for one request."""
    client = _client_raising(_timeout(), _timeout())

    with patch.object(llm_client, "_get_client", return_value=client):
        with pytest.raises(WorkflowGenLLMError) as exc:
            await complete(MESSAGES, TOOLS)

    message = str(exc.value)
    assert "too large" in message
    assert "smaller change" in message
    # And it says how long it actually waited, rather than a bare attempt count.
    assert "180s" in message


@pytest.mark.asyncio
async def test_a_transient_failure_keeps_the_full_retry_budget():
    """Rate limiting and connection errors are genuinely worth re-sending —
    narrowing the timeout budget must not narrow theirs."""
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(
        side_effect=[_rate_limited(), _connection_error(), "completion"]
    )

    with patch.object(llm_client, "_get_client", return_value=client):
        result = await complete(MESSAGES, TOOLS)

    assert result == "completion"
    assert client.chat.completions.create.await_count == 3


@pytest.mark.asyncio
async def test_a_malformed_request_is_not_retried_at_all():
    client = _client_raising(_bad_request())

    with patch.object(llm_client, "_get_client", return_value=client):
        with pytest.raises(WorkflowGenLLMError):
            await complete(MESSAGES, TOOLS)

    assert client.chat.completions.create.await_count == 1


@pytest.mark.asyncio
async def test_only_one_layer_retries():
    """The SDK's own retries are off, so the loop's attempt count is the whole
    story. With both on, a 3-attempt loop was really up to six requests and the
    worst-case wait was double what anyone had chosen."""
    llm_client._client = None
    with patch.object(llm_client, "is_workflow_gen_configured", return_value=True), patch(
        "api.services.workflow_gen.llm_client.AsyncAzureOpenAI"
    ) as constructor:
        llm_client._get_client()

    assert constructor.call_args.kwargs["max_retries"] == 0
    llm_client._client = None


@pytest.mark.asyncio
async def test_the_read_timeout_allows_for_a_long_generation():
    """A non-streaming request returns nothing until the model has finished
    writing, so this budget is generation time, not network silence. It has to
    fit the largest legitimate piece of work the assistant is asked to do."""
    assert llm_client.REQUEST_TIMEOUT_SECONDS >= 180.0
    # The connect timeout stays short — an unreachable endpoint should fail
    # fast rather than sit on the generation budget.
    assert llm_client.CONNECT_TIMEOUT_SECONDS <= 30.0
