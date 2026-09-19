"""Hangup and transfer strategies for TATA SmartFlo.

These run *during a live call*, which shapes every decision in them:

* SmartFlo's streaming protocol has no hangup event — an integrator may send
  only ``media``, ``mark`` and ``clear`` — so ending a call the agent decided to
  end has to go out over REST.
* That REST call shares one rate-limit budget with dialling, so a 429 is
  surfaced rather than retried.
* A 401 is re-minted exactly once. Our clock is not SmartFlo's, so one stale
  token is worth retrying; a loop during a live call is not.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.tata_smartflo.strategies import (
    TataSmartfloHangupStrategy,
    TataSmartfloTransferStrategy,
)

_ARGS = {
    "api_base": "https://api-smartflo.tatateleservices.com",
    "email": "ops@example.test",
    "password": "placeholder-password",
}


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Replays a scripted list of responses and records each request."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return _Response(*self._responses.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch(session):
    return patch(
        "api.services.telephony.providers.tata_smartflo.strategies.aiohttp.ClientSession",
        return_value=session,
    )


def _patch_token(token="tok-1"):
    return patch(
        "api.services.telephony.providers.tata_smartflo.strategies.token_cache.get_token",
        new_callable=AsyncMock,
        return_value=token,
    )


# ======== HANGUP ========


@pytest.mark.asyncio
async def test_hangup_uses_the_dedicated_endpoint_not_call_options():
    """There is no hangup ``type`` on /v1/call/options — it is its own endpoint.

    Assuming otherwise is easy: transfer, monitor, whisper and barge all live on
    call/options, so hangup looks like it should be type 5. It is not; the
    options API has only types 1-4.
    """
    session = _Session([(200, {"success": True, "message": "Call hangup successful"})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id="ref-1")

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({"call_id": "cs-1"}) is True

    assert session.calls[0]["url"].endswith("/v1/call/hangup")
    assert session.calls[0]["json"] == {"ref_id": "ref-1"}
    assert session.calls[0]["headers"]["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_hangup_falls_back_to_call_id_when_there_is_no_ref_id():
    """Inbound calls never had an initiation response, so no ref_id exists.

    SmartFlo accepts either identifier, and the serializer supplies both under
    SmartFlo's own names — there is no ``call_sid`` anywhere in this provider.
    """
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id=None)

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({"call_id": "cs-9"}) is True

    assert session.calls[0]["json"] == {"call_id": "cs-9"}


@pytest.mark.asyncio
async def test_a_ref_id_on_the_context_is_used_when_the_strategy_has_none():
    """The serializer supplies both ids, so a strategy built without one still works."""
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id=None)

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({"ref_id": "ref-ctx"}) is True

    assert session.calls[0]["json"] == {"ref_id": "ref-ctx"}


@pytest.mark.asyncio
async def test_a_ref_id_outranks_a_call_id_on_the_same_context():
    """SmartFlo accepts either, but ref_id addresses the call we actually placed."""
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id=None)

    with _patch(session), _patch_token():
        await strategy.execute_hangup({"ref_id": "ref-ctx", "call_id": "cs-9"})

    assert session.calls[0]["json"] == {"ref_id": "ref-ctx"}


@pytest.mark.asyncio
async def test_a_twilio_shaped_context_is_not_understood():
    """Guards the rename: this provider speaks ref_id/call_id, not call_sid.

    Before the serializer became SmartFlo's own, the context arrived with
    Twilio's key names. Accepting them now would let that coupling creep back
    in unnoticed.
    """
    session = _Session([])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id=None)

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({"call_sid": "cs-9"}) is False

    assert session.calls == [], "no request should be attempted"


@pytest.mark.asyncio
async def test_hangup_with_no_identifier_at_all_fails_without_calling_out():
    session = _Session([])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id=None)

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({}) is False

    assert session.calls == [], "no request should be attempted"


@pytest.mark.asyncio
async def test_a_rate_limited_hangup_is_not_retried():
    """Retrying would spend budget a queued call is waiting for."""
    session = _Session([(429, {"message": "Too many requests"})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id="ref-1")

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({}) is False

    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_a_401_is_reminted_once_and_then_succeeds():
    session = _Session([(401, {"message": "Unauthenticated"}), (200, {"success": True})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id="ref-1")

    with (
        _patch(session),
        patch(
            "api.services.telephony.providers.tata_smartflo.strategies.token_cache.get_token",
            new_callable=AsyncMock,
            side_effect=["stale", "fresh"],
        ),
        patch(
            "api.services.telephony.providers.tata_smartflo.strategies.token_cache.invalidate"
        ) as invalidate,
    ):
        assert await strategy.execute_hangup({}) is True

    invalidate.assert_called_once()
    assert session.calls[0]["headers"]["Authorization"] == "Bearer stale"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer fresh"


@pytest.mark.asyncio
async def test_a_401_that_survives_a_fresh_token_gives_up():
    """A second 401 is a credential problem; looping only burns rate limit."""
    session = _Session([(401, {}), (401, {})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id="ref-1")

    with (
        _patch(session),
        patch(
            "api.services.telephony.providers.tata_smartflo.strategies.token_cache.get_token",
            new_callable=AsyncMock,
            side_effect=["stale", "fresh"],
        ),
        patch(
            "api.services.telephony.providers.tata_smartflo.strategies.token_cache.invalidate"
        ),
    ):
        assert await strategy.execute_hangup({}) is False

    assert len(session.calls) == 2, "exactly one retry, not a loop"


@pytest.mark.asyncio
async def test_http_200_with_success_false_is_a_failure():
    """SmartFlo answers 200 for rejected operations; the body decides."""
    session = _Session([(200, {"success": False, "message": "Call already ended"})])
    strategy = TataSmartfloHangupStrategy(**_ARGS, ref_id="ref-1")

    with _patch(session), _patch_token():
        assert await strategy.execute_hangup({}) is False


# ======== TRANSFER ========


@pytest.mark.asyncio
async def test_transfer_to_a_smartflo_agent_uses_agent_id():
    session = _Session([(200, {"success": True, "message": "Transfer succeeded"})])
    strategy = TataSmartfloTransferStrategy(**_ARGS, ref_id="ref-1", agent_id="05080114")

    with _patch(session), _patch_token():
        assert await strategy.execute_transfer({}) is True

    assert session.calls[0]["url"].endswith("/v1/call/options")
    assert session.calls[0]["json"] == {
        "type": 4,
        "ref_id": "ref-1",
        "agent_id": "05080114",
    }


def _patch_transfer_context(target_number: str | None):
    """Stand in for the Redis TransferContext the transfer tool stores.

    This — not the context dict the serializer passes — is where a
    runtime-resolved destination actually lives. ``pipecat_engine_custom_tools``
    writes it keyed by ``gathered_context["call_id"]`` before dispatching, which
    for SmartFlo is the ref_id outbound and the call id inbound.
    """
    manager = SimpleNamespace(
        find_transfer_context_for_call=AsyncMock(
            return_value=(
                SimpleNamespace(target_number=target_number) if target_number else None
            )
        )
    )
    return patch(
        "api.services.telephony.call_transfer_manager.get_call_transfer_manager",
        new_callable=AsyncMock,
        return_value=manager,
    ), manager


@pytest.mark.asyncio
async def test_transfer_to_an_external_number_uses_intercom():
    """SmartFlo will not accept a phone number as agent_id.

    ``agent_id`` is for SmartFlo agents; ``intercom`` covers external mobiles,
    extensions, departments, queues, IVRs and SIP trunks.
    """
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloTransferStrategy(**_ARGS, ref_id="ref-1")
    ctx_patch, manager = _patch_transfer_context("919111111111")

    with _patch(session), _patch_token(), ctx_patch:
        assert await strategy.execute_transfer({"ref_id": "ref-1"}) is True

    # Looked up by the same identifier the call is addressed with.
    manager.find_transfer_context_for_call.assert_awaited_once_with("ref-1")
    assert session.calls[0]["json"] == {
        "type": 4,
        "ref_id": "ref-1",
        "intercom": "919111111111",
    }


@pytest.mark.asyncio
async def test_a_runtime_destination_beats_the_configured_one():
    """A transfer tool that resolved a destination mid-call must win.

    The configured intercom is a fallback; the stored context describes the
    call actually in progress.
    """
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloTransferStrategy(
        **_ARGS, ref_id="ref-1", intercom="919000000000"
    )
    ctx_patch, _ = _patch_transfer_context("919111111111")

    with _patch(session), _patch_token(), ctx_patch:
        await strategy.execute_transfer({})

    assert session.calls[0]["json"]["intercom"] == "919111111111"


@pytest.mark.asyncio
async def test_the_configured_destination_is_used_when_nothing_was_stored():
    session = _Session([(200, {"success": True})])
    strategy = TataSmartfloTransferStrategy(
        **_ARGS, ref_id="ref-1", intercom="919000000000"
    )
    ctx_patch, _ = _patch_transfer_context(None)

    with _patch(session), _patch_token(), ctx_patch:
        assert await strategy.execute_transfer({}) is True

    assert session.calls[0]["json"]["intercom"] == "919000000000"


@pytest.mark.asyncio
async def test_a_transfer_context_lookup_failure_does_not_kill_the_call():
    """Redis being unreachable must fail the transfer, not raise into the pipeline."""
    session = _Session([])
    strategy = TataSmartfloTransferStrategy(**_ARGS, ref_id="ref-1")

    with (
        _patch(session),
        _patch_token(),
        patch(
            "api.services.telephony.call_transfer_manager.get_call_transfer_manager",
            new_callable=AsyncMock,
            side_effect=RuntimeError("redis down"),
        ),
    ):
        assert await strategy.execute_transfer({}) is False

    assert session.calls == []


@pytest.mark.asyncio
async def test_transfer_without_any_destination_fails_without_calling_out():
    session = _Session([])
    strategy = TataSmartfloTransferStrategy(**_ARGS, ref_id="ref-1")
    ctx_patch, _ = _patch_transfer_context(None)

    with _patch(session), _patch_token(), ctx_patch:
        assert await strategy.execute_transfer({}) is False

    assert session.calls == []
