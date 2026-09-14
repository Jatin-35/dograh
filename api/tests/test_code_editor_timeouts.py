"""The timeout ladder between a live call and the sandbox.

Four timeouts sit between a caller speaking and user code running:

    voice agent → generated tool      DEFAULT_TOOL_TIMEOUT_MS   (deploy.py)
    api         → handlers service    sandbox timeout + margin  (workspace.py)
    handlers    → sandbox subprocess  timeout_seconds           (executor.py)

Their *order* is the safety property. The sandbox must give up first, so the
normal failure — user code hangs — comes back as a structured error the agent
can speak. The HTTP wait must expire before the calling tool does, so that even
when the handlers service itself wedges we answer before the agent stops
listening, rather than holding an API worker long after the caller has gone.

This got the ordering wrong once: the HTTP wait was a flat 30s, twice the
tool's 15s.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from api.services.code_editor import workspace
from api.services.code_editor.deploy import DEFAULT_TOOL_TIMEOUT_MS

ORG_ID = 3
FILES = {workspace.ENTRY_POINT_PATH: "def all_events_handler(event, context):\n    return {}\n"}


def _http_timeout_for(sandbox_seconds: float) -> float:
    return sandbox_seconds + workspace.EXECUTE_TIMEOUT_MARGIN_SECONDS


def test_the_http_wait_outlasts_the_sandbox():
    """Otherwise the HTTP call gives up first and we report a transport error
    for what is really 'your code took too long' — losing the sandbox's
    structured message in the normal failure case."""
    sandbox = 10.0
    assert _http_timeout_for(sandbox) > sandbox


def test_the_http_wait_expires_before_the_calling_tool_does():
    """The invariant that protects a live call."""
    sandbox = 10.0
    tool_timeout_seconds = DEFAULT_TOOL_TIMEOUT_MS / 1000
    assert _http_timeout_for(sandbox) < tool_timeout_seconds, (
        f"HTTP wait {_http_timeout_for(sandbox)}s must be under the tool's "
        f"{tool_timeout_seconds}s, or a wedged handlers service leaves the "
        "agent with a bare timeout and ties up an API worker past the point "
        "the caller stopped waiting."
    )


def test_the_margin_is_not_so_large_that_the_ladder_collapses():
    """A margin bigger than the gap between the sandbox and the tool would
    silently reintroduce the original bug at the default timeout."""
    tool_timeout_seconds = DEFAULT_TOOL_TIMEOUT_MS / 1000
    assert workspace.EXECUTE_TIMEOUT_MARGIN_SECONDS < tool_timeout_seconds - 10.0 + 0.001


@pytest.mark.asyncio
async def test_the_http_wait_is_derived_from_the_requested_timeout():
    """A longer Test Run must carry the HTTP wait with it. A fixed constant is
    what drifts out of step the moment someone raises one and not the other."""
    seen: dict[str, float] = {}

    class _Client:
        def __init__(self, timeout, **kwargs):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={"statusCode": 200, "result": {}})

    with patch.object(workspace.httpx, "AsyncClient", _Client), patch.object(
        workspace, "resolve_env", AsyncMock(return_value={})
    ):
        await workspace.run_test(ORG_ID, {}, files=FILES, timeout_seconds=25.0)

    assert seen["timeout"] == _http_timeout_for(25.0)


@pytest.mark.asyncio
async def test_a_wedged_service_is_reported_as_a_timeout_not_as_unreachable():
    """'Is the container running?' sends someone to check a container that is
    running perfectly well. The two failures need different messages because
    they need different fixes."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("timed out")

    with patch.object(workspace.httpx, "AsyncClient", _Client), patch.object(
        workspace, "resolve_env", AsyncMock(return_value={})
    ):
        with pytest.raises(workspace.WorkspaceError) as exc:
            await workspace.run_test(ORG_ID, {}, files=FILES)

    message = str(exc.value.message if hasattr(exc.value, "message") else exc.value)
    assert "did not respond" in message
    assert "is the handlers container running" not in message


@pytest.mark.asyncio
async def test_an_unreachable_service_still_says_so():
    """The other half: a refused connection must keep pointing at the container."""

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    with patch.object(workspace.httpx, "AsyncClient", _Client), patch.object(
        workspace, "resolve_env", AsyncMock(return_value={})
    ):
        with pytest.raises(workspace.WorkspaceError) as exc:
            await workspace.run_test(ORG_ID, {}, files=FILES)

    message = str(exc.value.message if hasattr(exc.value, "message") else exc.value)
    assert "Could not reach" in message
