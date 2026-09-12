import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pipecat.frames.frames import OutputTransportMessageUrgentFrame

from api.services.telephony.providers.voicelink import transport as voicelink_transport
from api.services.telephony.providers.voicelink.provider import (
    VoiceLinkProvider,
    normalize_customer_number,
)


def _provider(**overrides) -> VoiceLinkProvider:
    config = {
        "api_base": "https://app.voicelink.co.in/api",
        "username": "reseller-user",
        "password": "placeholder-password",
        "bearer_token": None,
        "did_number": "919484959244",
        "from_numbers": ["919484959244"],
    }
    config.update(overrides)
    return VoiceLinkProvider(config)


_ADD_LEAD_SUCCESS = (
    201,
    {
        "status": True,
        "message": "Lead added",
        "data": {
            "outbound_queue_id": 991,
            "bot_id": 17,
            "client_id": 474,
            "carrier_id": 3,
        },
    },
)


# ======== NUMBER NORMALIZATION (the 91-strip) ========


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 12-digit starting "91" → strip country code
        ("917340400524", "7340400524"),
        # 11-digit starting "0" → strip trunk prefix
        ("07340400524", "7340400524"),
        # formatted E.164 with spaces → digits only, then 91-strip
        ("+91 73404 00524", "7340400524"),
        ("+91-73404-00524", "7340400524"),
        # bare 10-digit local number → unchanged
        ("7340400524", "7340400524"),
        # 10-digit number that happens to start with 91 → NOT stripped
        ("9184012929", "9184012929"),
        # empty input → empty output
        ("", ""),
    ],
)
def test_normalize_customer_number(raw, expected):
    assert normalize_customer_number(raw) == expected


# ======== ADD_LEAD REQUEST SHAPE ========


@pytest.mark.asyncio
async def test_initiate_call_sends_bare_local_number_and_registered_did():
    provider = _provider()

    with (
        patch.object(
            provider, "_api_request", new_callable=AsyncMock
        ) as api_request,
        patch(
            "api.services.telephony.providers.voicelink.provider.get_backend_endpoints",
            new_callable=AsyncMock,
            return_value=("https://example.test", "wss://example.test"),
        ),
    ):
        api_request.return_value = _ADD_LEAD_SUCCESS

        result = await provider.initiate_call(
            to_number="+91 73404 00524",
            webhook_url="https://example.test/api/v1/telephony/voicelink/events",
            workflow_run_id=123,
            workflow_id=7,
            organization_id=11,
        )

    api_request.assert_awaited_once()
    method, path, payload = api_request.await_args.args
    assert method == "POST"
    assert path == "/v1/add_lead"

    # ⚠️ customer_number must be the BARE 10-digit local number
    assert payload["customer_number"] == "7340400524"
    # did_number keeps its registered (91-prefixed) form
    assert payload["did_number"] == "919484959244"
    assert payload["websocket_url"] == (
        "wss://example.test/api/v1/telephony/ws/7/11/123"
    )
    assert payload["webhook_url"] == (
        "https://example.test/api/v1/telephony/voicelink/events/123"
    )
    # custom_parameters is a JSON string
    custom = json.loads(payload["custom_parameters"])
    assert custom == {
        "workflow_id": 7,
        "organization_id": 11,
        "workflow_run_id": 123,
    }

    assert result.call_id == "991"
    assert result.status == "queued"
    assert result.caller_number == "919484959244"
    assert result.provider_metadata["outbound_queue_id"] == 991
    assert result.provider_metadata["bot_id"] == 17


@pytest.mark.asyncio
async def test_initiate_call_prefers_explicit_from_number():
    provider = _provider()

    with (
        patch.object(
            provider, "_api_request", new_callable=AsyncMock
        ) as api_request,
        patch(
            "api.services.telephony.providers.voicelink.provider.get_backend_endpoints",
            new_callable=AsyncMock,
            return_value=("https://example.test", "wss://example.test"),
        ),
    ):
        api_request.return_value = _ADD_LEAD_SUCCESS

        await provider.initiate_call(
            to_number="7340400524",
            webhook_url="unused",
            workflow_run_id=123,
            from_number="+919876543210",
            workflow_id=7,
            organization_id=11,
        )

    _, _, payload = api_request.await_args.args
    # Explicit caller id wins; formatting stripped but 91 prefix kept.
    assert payload["did_number"] == "919876543210"


@pytest.mark.asyncio
async def test_initiate_call_raises_on_provider_error():
    provider = _provider()

    with (
        patch.object(
            provider, "_api_request", new_callable=AsyncMock
        ) as api_request,
        patch(
            "api.services.telephony.providers.voicelink.provider.get_backend_endpoints",
            new_callable=AsyncMock,
            return_value=("https://example.test", "wss://example.test"),
        ),
    ):
        api_request.return_value = (422, {"status": False, "message": "bad DID"})

        with pytest.raises(HTTPException) as exc_info:
            await provider.initiate_call(
                to_number="7340400524",
                webhook_url="unused",
                workflow_run_id=123,
                workflow_id=7,
                organization_id=11,
            )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_initiate_call_requires_routing_ids():
    provider = _provider()

    with pytest.raises(ValueError):
        await provider.initiate_call(
            to_number="7340400524",
            webhook_url="unused",
            workflow_run_id=123,
        )


# ======== 401 → RE-LOGIN → RETRY ========


@pytest.mark.asyncio
async def test_api_request_relogins_and_retries_once_on_401():
    provider = _provider(bearer_token="stale-token")

    with (
        patch.object(provider, "_send_request", new_callable=AsyncMock) as send,
        patch.object(
            provider, "_login", new_callable=AsyncMock, return_value="fresh-token"
        ) as login,
    ):
        send.side_effect = [(401, {"message": "Unauthenticated"}), _ADD_LEAD_SUCCESS]

        status, data = await provider._api_request("POST", "/v1/add_lead", {})

    assert status == 201
    assert data["status"] is True
    login.assert_awaited_once()
    assert send.await_count == 2
    # Retry carries the fresh token
    assert send.await_args_list[1].args[3] == "fresh-token"


@pytest.mark.asyncio
async def test_api_request_does_not_retry_without_login_credentials():
    provider = _provider(username=None, password=None, bearer_token="static-token")

    with (
        patch.object(provider, "_send_request", new_callable=AsyncMock) as send,
        patch.object(provider, "_login", new_callable=AsyncMock) as login,
    ):
        send.return_value = (401, {"message": "Unauthenticated"})

        status, _ = await provider._api_request("POST", "/v1/add_lead", {})

    assert status == 401
    login.assert_not_awaited()
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_api_request_logs_in_first_when_no_token_cached():
    provider = _provider()  # username/password only, no bearer_token

    async def _login():
        provider._access_token = "first-token"
        return "first-token"

    with (
        patch.object(provider, "_send_request", new_callable=AsyncMock) as send,
        patch.object(
            provider, "_login", new_callable=AsyncMock, side_effect=_login
        ) as login,
    ):
        send.return_value = _ADD_LEAD_SUCCESS

        status, _ = await provider._api_request("POST", "/v1/add_lead", {})

    assert status == 201
    login.assert_awaited_once()
    assert send.await_args.args[3] == "first-token"


# ======== WEBHOOK EVENT PARSING → STATUS MAPPING ========


def _event(event: str, **call_overrides) -> dict:
    call = {
        "id": "5b2f9c1e-aaaa-bbbb-cccc-1234567890ab",
        "direction": "outbound",
        "callType": "outbound",
        "from": "919484959244",
        "to": "7340400524",
        "status": "completed",
        "hangupCause": "16",
        "startedAt": "2026-06-11T10:00:00Z",
        "ringingAt": "2026-06-11T10:00:02Z",
        "answeredAt": "2026-06-11T10:00:08Z",
        "endedAt": "2026-06-11T10:01:08Z",
        "ringDurationSec": 6,
        "durationSec": 60,
        "customParameters": {"workflow_run_id": 123},
    }
    call.update(call_overrides)
    return {"event": event, "timestamp": "2026-06-11T10:01:08Z", "call": call}


@pytest.mark.parametrize(
    "event,expected_status",
    [
        ("call.initiated", "initiated"),
        ("call.ringing", "ringing"),
        ("call.answered", "in-progress"),
        ("call.completed", "completed"),
        ("call.ended", "completed"),
        ("call.failed", "failed"),
    ],
)
def test_parse_status_callback_maps_events(event, expected_status):
    provider = _provider()

    parsed = provider.parse_status_callback(_event(event))

    assert parsed["status"] == expected_status
    assert parsed["call_id"] == "5b2f9c1e-aaaa-bbbb-cccc-1234567890ab"
    assert parsed["from_number"] == "919484959244"
    assert parsed["to_number"] == "7340400524"
    assert parsed["direction"] == "outbound"
    assert parsed["duration"] == "60"


def test_parse_status_callback_unknown_event_passes_through():
    provider = _provider()

    parsed = provider.parse_status_callback(_event("call.something_new"))

    assert parsed["status"] == "call.something_new"


@pytest.mark.parametrize("field", ["recordingUrl", "recording_url"])
def test_parse_status_callback_picks_up_recording_url_defensively(field):
    provider = _provider()

    parsed = provider.parse_status_callback(
        _event("call.completed", **{field: "https://cdn.test/rec.mp3"})
    )

    assert parsed["extra"]["recording_url"] == "https://cdn.test/rec.mp3"


def test_parse_status_callback_tolerates_missing_call_object():
    provider = _provider()

    parsed = provider.parse_status_callback({"event": "call.failed"})

    assert parsed["status"] == "failed"
    assert parsed["call_id"] == ""
    assert parsed["duration"] is None


# ======== CONFIG VALIDATION ========


def test_validate_config_accepts_username_password():
    assert _provider(bearer_token=None).validate_config() is True


def test_validate_config_accepts_bearer_token_only():
    provider = _provider(username=None, password=None, bearer_token="token")
    assert provider.validate_config() is True


def test_validate_config_rejects_missing_auth():
    provider = _provider(username=None, password=None, bearer_token=None)
    assert provider.validate_config() is False


def test_validate_config_rejects_missing_did():
    assert _provider(did_number=None).validate_config() is False


# ======== CALL TRANSFER ========


@pytest.fixture(autouse=True)
def _clear_active_call_registry():
    """Prevent state from leaking between tests via the module-level registry."""
    voicelink_transport._active_calls.clear()
    yield
    voicelink_transport._active_calls.clear()


def test_supports_transfers_is_true():
    # Plumbing to reach the live socket is real and complete; whether
    # VoiceLink acts on the message is unconfirmed (see transfer_call docstring).
    assert _provider().supports_transfers() is True


@pytest.mark.asyncio
async def test_transfer_call_sends_transfer_event_over_registered_live_transport():
    provider = _provider()

    output_transport = AsyncMock()
    voicelink_transport.register_active_call(
        "5b2f9c1e-call-sid",
        output_transport,
        stream_sid="MZ-stream-1",
        call_sid="5b2f9c1e-call-sid",
    )

    result = await provider.transfer_call(
        destination="+91 73404 00524",
        transfer_id="transfer-uuid-1",
        conference_name="transfer-5b2f9c1e-call-sid",
        timeout=30,
    )

    output_transport.queue_frame.assert_awaited_once()
    (sent_frame,), _ = output_transport.queue_frame.await_args
    assert isinstance(sent_frame, OutputTransportMessageUrgentFrame)
    assert sent_frame.message == {
        "event": "transfer",
        "stream_sid": "MZ-stream-1",
        "call_sid": "5b2f9c1e-call-sid",
        # target is normalized to the bare local number, matching add_lead's
        # customer_number convention.
        "target": "7340400524",
    }

    assert result["status"] == "initiated"
    assert result["provider"] == "voicelink"
    assert result["call_sid"] == "5b2f9c1e-call-sid"


@pytest.mark.asyncio
async def test_transfer_call_raises_when_no_live_connection_registered():
    provider = _provider()

    with pytest.raises(Exception, match="No live VoiceLink connection registered"):
        await provider.transfer_call(
            destination="7340400524",
            transfer_id="transfer-uuid-2",
            conference_name="transfer-unregistered-call-sid",
            timeout=30,
        )


@pytest.mark.asyncio
async def test_transfer_call_raises_on_unexpected_conference_name_shape():
    provider = _provider()

    with pytest.raises(ValueError, match="Unexpected conference_name shape"):
        await provider.transfer_call(
            destination="7340400524",
            transfer_id="transfer-uuid-3",
            conference_name="not-a-transfer-name",
            timeout=30,
        )


@pytest.mark.asyncio
async def test_transfer_call_raises_when_original_call_sid_missing():
    provider = _provider()

    # Mirrors pipecat_engine_custom_tools.py building
    # conference_name = f"transfer-{original_call_sid}" when
    # gathered_context["call_id"] was never populated.
    with pytest.raises(ValueError, match="Could not recover the original call id"):
        await provider.transfer_call(
            destination="7340400524",
            transfer_id="transfer-uuid-4",
            conference_name="transfer-None",
            timeout=30,
        )


def test_register_active_call_transport_lifecycle():
    output_transport = object()
    voicelink_transport.register_active_call(
        "call-key-1", output_transport, stream_sid="stream-1", call_sid="call-key-1"
    )

    active = voicelink_transport.get_active_call("call-key-1")
    assert active is not None
    assert active.output_transport is output_transport
    assert active.stream_sid == "stream-1"

    voicelink_transport.unregister_active_call("call-key-1")
    assert voicelink_transport.get_active_call("call-key-1") is None


def test_register_active_call_ignores_empty_key():
    voicelink_transport.register_active_call(
        "", object(), stream_sid="stream-1", call_sid=""
    )
    assert voicelink_transport.get_active_call("") is None
