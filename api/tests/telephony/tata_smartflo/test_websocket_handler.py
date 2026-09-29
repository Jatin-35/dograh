"""SmartFlo's media socket: the shared /api/v1/telephony/ws route accepts it and
hands it to ``TataSmartfloProvider.handle_websocket``, which reads the opening
messages and starts the agent.

This handler was missing (it raised), so the first live SmartFlo call on
2026-09-29 connected, got a socket, and then dropped. SmartFlo's stream is
Twilio Media Streams in shape: ``connected``, ``start``, then ``media``.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.db import db_client

from api.services.telephony.providers.tata_smartflo import provider as provider_module
from api.services.telephony.providers.tata_smartflo.provider import (
    TataSmartfloProvider,
)


class _Socket:
    def __init__(self, messages, then_block=False):
        self._messages = [m if isinstance(m, str) else json.dumps(m) for m in messages]
        self._then_block = then_block
        self.closed = None

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        if self._then_block:
            await asyncio.sleep(3600)
        raise RuntimeError("socket closed")

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def _provider():
    return TataSmartfloProvider(
        {"email": "ops@example.com", "api_token": "tok", "from_numbers": []}
    )


async def _handle(socket, run_context=None):
    run = SimpleNamespace(gathered_context=run_context or {})
    pipeline = AsyncMock()
    with (
        patch("api.services.pipecat.run_pipeline.run_pipeline_telephony", pipeline),
        patch.object(db_client, "get_workflow_run_by_id", AsyncMock(return_value=run)),
    ):
        await _provider().handle_websocket(socket, 7, 5, 499)
    return pipeline


CONNECTED = {"event": "connected", "protocol": "Call", "version": "1.0.0"}


@pytest.mark.asyncio
async def test_connected_then_start_runs_the_agent_with_the_streams_ids():
    start = {
        "event": "start",
        "streamSid": "MZ-stream-1",
        "start": {"streamSid": "MZ-stream-1", "callSid": "HYD12-T7-1790683445.618022"},
    }
    pipeline = await _handle(_Socket([CONNECTED, start]))
    pipeline.assert_awaited_once()
    kwargs = pipeline.await_args.kwargs
    assert kwargs["provider_name"] == "tata_smartflo"
    assert (
        kwargs["workflow_id"],
        kwargs["organization_id"],
        kwargs["workflow_run_id"],
    ) == (7, 5, 499)
    assert kwargs["call_id"] == "HYD12-T7-1790683445.618022"
    assert kwargs["transport_kwargs"] == {
        "stream_id": "MZ-stream-1",
        "call_id": "HYD12-T7-1790683445.618022",
    }


@pytest.mark.asyncio
async def test_the_stream_id_may_be_only_inside_start_and_the_call_id_named_callId():
    start = {"event": "start", "start": {"streamSid": "S2", "callId": "C2"}}
    pipeline = await _handle(_Socket([start]))
    assert pipeline.await_args.kwargs["transport_kwargs"] == {
        "stream_id": "S2",
        "call_id": "C2",
    }


@pytest.mark.asyncio
async def test_without_a_call_id_on_the_socket_the_connect_requests_is_used():
    start = {"event": "start", "streamSid": "S3", "start": {}}
    pipeline = await _handle(_Socket([start]), run_context={"call_id": "from-connect"})
    assert pipeline.await_args.kwargs["call_id"] == "from-connect"


@pytest.mark.asyncio
async def test_unknown_events_and_junk_before_start_are_skipped():
    start = {"event": "start", "streamSid": "S4", "start": {"callSid": "C4"}}
    pipeline = await _handle(
        _Socket(["not json", "[1,2]", {"event": "mark"}, CONNECTED, start])
    )
    assert pipeline.await_args.kwargs["transport_kwargs"]["stream_id"] == "S4"


@pytest.mark.asyncio
async def test_a_stream_that_starts_with_media_still_runs():
    media = {"event": "media", "streamSid": "S5", "media": {"payload": "AAAA"}}
    pipeline = await _handle(_Socket([CONNECTED, media]), run_context={"call_id": "C5"})
    assert pipeline.await_args.kwargs["transport_kwargs"] == {
        "stream_id": "S5",
        "call_id": "C5",
    }


@pytest.mark.asyncio
async def test_no_start_event_closes_the_socket_instead_of_running_blind():
    socket = _Socket([CONNECTED], then_block=True)
    with patch.object(provider_module, "_PREAMBLE_TIMEOUT_SECONDS", 0.05):
        pipeline = await _handle(socket)
    pipeline.assert_not_awaited()
    assert socket.closed[0] == 4400


@pytest.mark.asyncio
async def test_too_many_messages_without_start_closes_the_socket():
    socket = _Socket([{"event": "mark"}] * 20)
    pipeline = await _handle(socket)
    pipeline.assert_not_awaited()
    assert socket.closed[0] == 4400


@pytest.mark.asyncio
async def test_an_api_token_is_used_as_is_for_hangup_without_a_login():
    from api.services.telephony.providers.tata_smartflo.strategies import (
        TataSmartfloHangupStrategy,
    )

    strategy = TataSmartfloHangupStrategy(
        api_base="https://api-smartflo.example",
        email=None,
        password=None,
        api_token="tok-9",
    )
    sent = {}

    class _Response:
        status = 200

        async def json(self, content_type=None):
            return {"success": True}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(url=url, json=json, headers=headers)
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    from api.services.telephony.providers.tata_smartflo import strategies

    token_cache = AsyncMock()
    with (
        patch.object(strategies.aiohttp, "ClientSession", _Session),
        patch.object(strategies, "token_cache", token_cache),
    ):
        ok = await strategy.execute_hangup({"call_id": "C9"})
    assert ok is True
    assert sent["url"] == "https://api-smartflo.example/v1/call/hangup"
    assert sent["json"] == {"call_id": "C9"}
    assert sent["headers"] == {"Authorization": "Bearer tok-9"}
    token_cache.get_token.assert_not_called()
