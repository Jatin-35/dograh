"""The VoiceLink inbound WebSocket handshake and start-frame routing.

Inbound had no test at all, and inbound is where it broke: VoiceLink's client
opened the socket, our server accepted it and logged HTTP 101, and the client
then closed with 1006 in ~15 ms having sent nothing. VoiceLink's account is
that its side never saw the connection reach "open" — which is what a client
sees when it offers a WebSocket subprotocol and the server does not echo one
back.

The same trap is already handled for chan_websocket in
``api/routes/telephony.py``, which must echo ``media`` or the connection drops.
Nothing guarded it here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import WebSocketDisconnect

from api.services.telephony.providers.voicelink import routes as voicelink_routes

# VoiceLink's documented inbound start frame. The called number is at
# ``start.to`` — nested, not top level.
_START_FRAME = {
    "event": "start",
    "sequence_number": 0,
    "stream_sid": "stream_call-uuid",
    "timestamp": "2026-09-22T15:30:00.000+05:30",
    "start": {
        "stream_sid": "stream_call-uuid",
        "call_sid": "call-uuid",
        "account_sid": "3089",
        "from": "919111111111",
        "to": "919429397383",
        "timestamp": "2026-09-22T15:30:00.000+05:30",
        "custom_parameters": {
            "campaignId": None,
            "callType": "inbound",
            "botId": 617,
            "clientId": 3089,
        },
        "media_format": {"encoding": "audio/alaw", "sample_rate": "8000"},
    },
}


def _websocket(headers: dict | None = None, messages: list[str] | None = None):
    """A socket that hands over the given frames, then disconnects."""
    ws = SimpleNamespace()
    ws.headers = headers or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()

    queue = list(messages or [])

    async def receive_text():
        if queue:
            return queue.pop(0)
        # No more frames: the client went away, exactly as VoiceLink's does.
        raise WebSocketDisconnect(code=1006)

    ws.receive_text = AsyncMock(side_effect=receive_text)
    return ws


def _no_matching_did():
    """Make the DID lookup return nothing, so the handler stops at the join.

    Enough to prove which number was extracted and looked up, without
    standing up a workflow, a run and a pipeline.
    """
    result = MagicMock()
    result.first.return_value = None

    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch.object(voicelink_routes.db_client, "async_session", return_value=ctx)


class TestHandshake:
    """What the server answers the upgrade with."""

    @pytest.mark.asyncio
    async def test_an_offered_subprotocol_is_echoed_back(self):
        """The regression.

        RFC 6455 has the server select one of the protocols the client offered.
        A client that offers one and gets none back can treat the handshake as
        failed, never surface "open" to its application, and close having sent
        nothing — which is precisely the 1006-with-no-frame we saw.
        """
        ws = _websocket(headers={"sec-websocket-protocol": "media"})

        await voicelink_routes.voicelink_inbound_ws(ws)

        ws.accept.assert_awaited_once_with(subprotocol="media")

    @pytest.mark.asyncio
    async def test_the_first_protocol_is_chosen_when_several_are_offered(self):
        """The header is a comma-separated list; the server picks one, not all."""
        ws = _websocket(headers={"sec-websocket-protocol": "media, audio, v1"})

        await voicelink_routes.voicelink_inbound_ws(ws)

        ws.accept.assert_awaited_once_with(subprotocol="media")

    @pytest.mark.asyncio
    async def test_no_offer_means_no_subprotocol(self):
        """Echoing one that was never offered is itself a handshake failure."""
        ws = _websocket(headers={})

        await voicelink_routes.voicelink_inbound_ws(ws)

        ws.accept.assert_awaited_once_with(subprotocol=None)

    @pytest.mark.asyncio
    async def test_a_client_that_sends_nothing_is_logged_not_raised(self):
        """This is the observed failure; it must stay a log line, not a 500."""
        ws = _websocket(headers={})

        with patch.object(voicelink_routes, "logger") as logger:
            await voicelink_routes.voicelink_inbound_ws(ws)

        logged = " ".join(str(c) for c in logger.info.call_args_list)
        assert "1006" in logged


class TestStartFrame:
    """Routing VoiceLink's documented inbound frame."""

    @pytest.mark.asyncio
    async def test_the_called_number_is_read_from_the_nested_start_object(self):
        """``start.to`` is the destination — not ``start.from``, not top level.

        Getting this wrong routes the call to the caller's number, which
        matches no DID, and the call is refused with a misleading 4404.
        """
        import json

        ws = _websocket(
            headers={"sec-websocket-protocol": "media"},
            messages=[json.dumps(_START_FRAME)],
        )

        with _no_matching_did(), patch.object(voicelink_routes, "logger") as logger:
            await voicelink_routes.voicelink_inbound_ws(ws)

        logged = " ".join(str(c) for c in logger.error.call_args_list)
        # Normalized to E.164 before the lookup, which is how DIDs are stored.
        assert "+919429397383" in logged
        assert "+919111111111" not in logged, "routed on the caller, not the callee"

    @pytest.mark.asyncio
    async def test_a_connected_frame_before_start_is_skipped(self):
        """Some clients send ``connected`` first; the DID is in ``start``."""
        import json

        ws = _websocket(
            headers={},
            messages=[json.dumps({"event": "connected"}), json.dumps(_START_FRAME)],
        )

        with _no_matching_did(), patch.object(voicelink_routes, "logger") as logger:
            await voicelink_routes.voicelink_inbound_ws(ws)

        logged = " ".join(str(c) for c in logger.error.call_args_list)
        assert "+919429397383" in logged

    @pytest.mark.asyncio
    async def test_an_unknown_did_is_refused_with_4404(self):
        import json

        ws = _websocket(headers={}, messages=[json.dumps(_START_FRAME)])

        with _no_matching_did():
            await voicelink_routes.voicelink_inbound_ws(ws)

        ws.close.assert_awaited_once()
        kwargs = ws.close.await_args.kwargs
        assert kwargs.get("code") == 4404
