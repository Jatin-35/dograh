"""TATA SmartFlo provider behaviour.

The upstream attempt at this provider (dograh-hq/dograh#731) was closed carrying
five P1 findings, two of which were cross-tenant audio misrouting. Several tests
here exist specifically because of those: they pin the decisions that make the
same mistakes impossible rather than merely absent.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.errors.telephony_errors import TelephonyError
from api.services.telephony.providers.tata_smartflo.provider import (
    TataSmartfloProvider,
)


def _provider(**overrides) -> TataSmartfloProvider:
    config = {
        "api_base": "https://api-smartflo.tatateleservices.com",
        "email": "ops@example.test",
        "password": "placeholder-password",
        "api_key": "click-to-call-key",
        "caller_id": "919484959244",
        "from_numbers": ["919484959244"],
        "connect_secret": None,
    }
    config.update(overrides)
    return TataSmartfloProvider(config)


_CLICK_TO_CALL_OK = (
    200,
    {
        "success": True,
        "message": "Originate successfully queued",
        "ref_id": "504bb41c-c2ae-4ec4-9b9e-b1c0f10dcbc2",
    },
)


# ======== OUTBOUND: Click-to-Call ========


@pytest.mark.asyncio
async def test_initiate_call_sends_the_payload_smartflo_requires():
    provider = _provider()

    with patch.object(provider, "_authed_post", new_callable=AsyncMock) as post:
        post.return_value = _CLICK_TO_CALL_OK
        await provider.initiate_call(
            to_number="919111111111",
            webhook_url="https://example.test/unused",
            workflow_run_id=123,
        )

    path, payload = post.await_args.args
    assert path == "/v1/click_to_call_support"
    assert payload["customer_number"] == "919111111111"
    assert payload["api_key"] == "click-to-call-key"
    # SmartFlo supports no other value; synchronous mode does not exist.
    assert payload["async"] == 1
    assert payload["caller_id"] == "919484959244"


@pytest.mark.asyncio
async def test_ref_id_is_stored_under_both_keys():
    """The linchpin of the whole integration.

    ``ref_id`` arrives only in the Click-to-Call response, and transfer, hangup
    and webhook correlation all key on it. It is written twice on purpose:
    ``call_id`` is the generic key that ``get_workflow_run_by_call_id`` looks up
    through ``idx_workflow_runs_call_id``, and ``smartflo_ref_id`` says what the
    value actually is.

    Lose either and the failure is silent — calls connect fine and transfers
    stop working weeks later.
    """
    provider = _provider()

    with patch.object(provider, "_authed_post", new_callable=AsyncMock) as post:
        post.return_value = _CLICK_TO_CALL_OK
        result = await provider.initiate_call(
            to_number="919111111111", webhook_url="", workflow_run_id=123
        )

    ref_id = "504bb41c-c2ae-4ec4-9b9e-b1c0f10dcbc2"
    assert result.provider_metadata["call_id"] == ref_id
    assert result.provider_metadata["smartflo_ref_id"] == ref_id
    assert result.call_id == ref_id
    assert result.caller_number == "919484959244"


@pytest.mark.asyncio
async def test_workflow_run_id_travels_in_custom_identifier():
    """Inbound has no ref_id, so the run id is how a webhook finds its run."""
    provider = _provider()

    with patch.object(provider, "_authed_post", new_callable=AsyncMock) as post:
        post.return_value = _CLICK_TO_CALL_OK
        await provider.initiate_call(
            to_number="919111111111", webhook_url="", workflow_run_id=4242
        )

    _, payload = post.await_args.args
    assert payload["custom_identifier"] == {"workflow_run_id": "4242"}


@pytest.mark.asyncio
async def test_outbound_without_api_key_is_refused_before_the_request():
    """caller_id cannot place a call on its own — the Click-to-Call key does."""
    provider = _provider(api_key=None, caller_id=None)

    with pytest.raises(ValueError, match="Click-to-Call"):
        await provider.initiate_call(to_number="919111111111", webhook_url="")


@pytest.mark.asyncio
async def test_rate_limit_is_surfaced_not_swallowed():
    """SmartFlo's limit is shared across every API and equals the account CPS.

    Raising lets the campaign dispatcher apply backpressure. Retrying here would
    spend the budget that a queued call is waiting for.
    """
    provider = _provider()

    with patch.object(provider, "_authed_post", new_callable=AsyncMock) as post:
        post.return_value = (429, {"message": "Too many requests"})
        with pytest.raises(RuntimeError, match="rate limit"):
            await provider.initiate_call(to_number="919111111111", webhook_url="")


@pytest.mark.asyncio
async def test_a_refused_call_raises_rather_than_returning_a_blank_result():
    provider = _provider()

    with patch.object(provider, "_authed_post", new_callable=AsyncMock) as post:
        post.return_value = (400, {"success": False, "message": "Invalid number"})
        with pytest.raises(RuntimeError, match="Invalid number"):
            await provider.initiate_call(to_number="nonsense", webhook_url="")


# ======== INBOUND: claiming the shared dispatcher ========


class TestWebhookClaim:
    """``can_handle_webhook`` decides what the shared /inbound/run hands us.

    Too narrow and SmartFlo calls are never answered. Too broad and this
    provider swallows another provider's webhook — which would route one
    customer's call into another's workflow.
    """

    def test_claims_the_streaming_connect_payload(self):
        assert TataSmartfloProvider.can_handle_webhook(
            {
                "callId": "c-3f9e17",
                "fromNumber": "919111111111",
                "toNumber": "919484959244",
            },
            {},
        )

    def test_claims_the_dollar_prefixed_webhook_payload(self):
        assert TataSmartfloProvider.can_handle_webhook(
            {"$call_id": "c-1", "$call_to_number": "919484959244"}, {}
        )

    def test_does_not_claim_a_twilio_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"CallSid": "CA123", "From": "+15551234567", "To": "+15557654321"}, {}
        )

    def test_does_not_claim_a_plivo_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"CallUUID": "abc", "From": "919111111111", "To": "919484959244"}, {}
        )

    def test_does_not_claim_an_empty_body(self):
        assert not TataSmartfloProvider.can_handle_webhook({}, {})

    def test_does_not_claim_a_payload_missing_the_called_number(self):
        # Without a called number there is no DID to route on, so claiming it
        # would only produce a rejection further down.
        assert not TataSmartfloProvider.can_handle_webhook({"callId": "c-1"}, {})


class TestInboundNormalization:
    def test_parses_the_camelcase_connect_payload(self):
        data = TataSmartfloProvider.parse_inbound_webhook(
            {
                "callId": "c-3f9e17",
                "fromNumber": "919111111111",
                "toNumber": "919484959244",
                "status": "ringing",
            }
        )
        assert data.provider == "tata_smartflo"
        assert data.call_id == "c-3f9e17"
        assert data.from_number == "919111111111"
        assert data.to_number == "919484959244"
        assert data.direction == "inbound"

    def test_parses_the_dollar_prefixed_webhook_variant(self):
        data = TataSmartfloProvider.parse_inbound_webhook(
            {
                "$uuid": "u-1",
                "$caller_id_number": "919111111111",
                "$call_to_number": "919484959244",
            }
        )
        assert data.call_id == "u-1"
        assert data.to_number == "919484959244"

    def test_direction_is_always_inbound_here(self):
        # This path only ever runs for inbound; a payload claiming otherwise
        # must not flip it, or the run is created with the wrong direction.
        data = TataSmartfloProvider.parse_inbound_webhook(
            {"callId": "c-1", "toNumber": "919484959244", "direction": "outbound"}
        )
        assert data.direction == "inbound"


# ======== INBOUND: the refusal contract ========


class TestRefusalShape:
    """SmartFlo's contract is exact: HTTP 200, JSON, and the body decides.

    Their documentation states any deviation hangs the call up immediately. So a
    quota rejection has to look like this, or the caller hears a drop with no
    reason and the run is never created.
    """

    def test_a_quota_rejection_is_http_200_with_success_false(self):
        import json

        response = TataSmartfloProvider.generate_validation_error_response(
            TelephonyError.QUOTA_EXCEEDED
        )
        assert response.status_code == 200
        body = json.loads(response.body.decode())
        assert body["success"] is False
        assert body["message"]

    def test_every_rejection_reason_produces_a_message(self):
        import json

        for error in (
            TelephonyError.QUOTA_EXCEEDED,
            TelephonyError.CONCURRENT_CALL_LIMIT,
            TelephonyError.PHONE_NUMBER_NOT_CONFIGURED,
            TelephonyError.WORKFLOW_NOT_FOUND,
            TelephonyError.SIGNATURE_VALIDATION_FAILED,
        ):
            response = TataSmartfloProvider.generate_validation_error_response(error)
            body = json.loads(response.body.decode())
            assert body["success"] is False
            assert body["message"], f"no message for {error}"

    @pytest.mark.asyncio
    async def test_admission_returns_the_url_under_the_documented_key(self):
        """SmartFlo requires exactly ``success`` and ``wss_url``."""
        provider = _provider()
        result = await provider.start_inbound_stream(
            websocket_url="wss://example.test/api/v1/telephony/ws/7/11/123",
            workflow_run_id=123,
            normalized_data=TataSmartfloProvider.parse_inbound_webhook(
                {"callId": "c-1", "toNumber": "919484959244"}
            ),
            backend_endpoint="https://example.test",
        )
        assert result == {
            "success": True,
            "wss_url": "wss://example.test/api/v1/telephony/ws/7/11/123",
        }


# ======== INBOUND: request verification ========


class TestInboundVerification:
    """The seam that closes PR #731's worst finding.

    Theirs minted a media token from a caller-supplied ``workflow_run_id``.
    Nothing here accepts an identifier from the request: the shared dispatcher
    resolves the call from the dialled DID, and this only decides whether the
    request is allowed to be considered at all.
    """

    @pytest.mark.asyncio
    async def test_accepts_the_configured_secret(self):
        provider = _provider(connect_secret="s3cret")
        assert await provider.verify_inbound_signature(
            "https://example.test", {}, {"x-smartflo-secret": "s3cret"}, ""
        )

    @pytest.mark.asyncio
    async def test_rejects_a_wrong_secret(self):
        provider = _provider(connect_secret="s3cret")
        assert not await provider.verify_inbound_signature(
            "https://example.test", {}, {"x-smartflo-secret": "wrong"}, ""
        )

    @pytest.mark.asyncio
    async def test_rejects_a_missing_secret_when_one_is_configured(self):
        provider = _provider(connect_secret="s3cret")
        assert not await provider.verify_inbound_signature(
            "https://example.test", {}, {}, ""
        )

    @pytest.mark.asyncio
    async def test_allows_everything_when_no_secret_is_configured(self):
        """Deliberate, and the config UI says so.

        SmartFlo documents no request signing, so an operator who has not set a
        secret has no mechanism. Refusing every call would take the provider
        down rather than secure it.
        """
        provider = _provider(connect_secret=None)
        assert await provider.verify_inbound_signature(
            "https://example.test", {}, {}, ""
        )

    @pytest.mark.asyncio
    async def test_header_name_is_matched_case_insensitively(self):
        provider = _provider(connect_secret="s3cret")
        assert await provider.verify_inbound_signature(
            "https://example.test", {}, {"X-Smartflo-Secret": "s3cret"}, ""
        )


# ======== LIFECYCLE WEBHOOK ========


def test_status_callback_normalizes_the_fields_a_run_needs():
    provider = _provider()
    normalized = provider.parse_status_callback(
        {
            "$ref_id": "504bb41c",
            "$status": "hangup",
            "$duration": "42",
            "$billsec": "38",
            "$hangup_cause": "NORMAL_CLEARING",
            "$recording_url": "https://rec.example.test/1.mp3",
        }
    )
    assert normalized["call_id"] == "504bb41c"
    assert normalized["duration"] == "42"
    assert normalized["hangup_cause"] == "NORMAL_CLEARING"
    assert normalized["recording_url"] == "https://rec.example.test/1.mp3"


def test_status_callback_accepts_unprefixed_names_too():
    provider = _provider()
    normalized = provider.parse_status_callback(
        {"ref_id": "abc", "status": "answered", "duration": 10}
    )
    assert normalized["call_id"] == "abc"
    assert normalized["status"] == "answered"


# ======== CAPABILITIES ========


def test_config_is_valid_for_inbound_only():
    """Inbound needs credentials — for hangup — but never the Click-to-Call key."""
    assert _provider(api_key=None, caller_id=None).validate_config()


def test_config_without_credentials_is_invalid():
    assert not _provider(email=None, password=None).validate_config()


def test_transfers_are_supported():
    assert _provider().supports_transfers()


def test_available_numbers_come_from_configuration():
    assert _provider().get_available_phone_numbers is not None


class TestTransferFindsTheLiveCall:
    """SmartFlo moves the *existing* call, so it must know which one.

    Conference-style providers dial a fresh leg and never need the original
    id, which is why the shared transfer tool passes only
    ``destination``/``transfer_id``/``conference_name``/``timeout``. Taking
    that signature at face value left SmartFlo with no identifier at all and
    every transfer returning an error.
    """

    @staticmethod
    def _transfer_context(original_call_sid):
        manager = SimpleNamespace(
            get_transfer_context=AsyncMock(
                return_value=(
                    SimpleNamespace(original_call_sid=original_call_sid)
                    if original_call_sid
                    else None
                )
            )
        )
        return patch(
            "api.services.telephony.call_transfer_manager.get_call_transfer_manager",
            new_callable=AsyncMock,
            return_value=manager,
        )

    @pytest.mark.asyncio
    async def test_the_call_id_is_recovered_from_the_stored_transfer_context(self):
        provider = _provider()

        with (
            self._transfer_context("ref-42"),
            patch.object(
                provider,
                "_authed_post",
                new_callable=AsyncMock,
                return_value=(200, {"success": True}),
            ) as post,
        ):
            result = await provider.transfer_call(
                destination="919111111111",
                transfer_id="tx-1",
                conference_name="transfer-ref-42",
            )

        assert result["status"] == "success"
        path, payload = post.await_args.args
        assert path == "/v1/call/options"
        assert payload == {
            "type": 4,
            "ref_id": "ref-42",
            "intercom": "919111111111",
        }

    @pytest.mark.asyncio
    async def test_an_explicit_ref_id_argument_still_wins(self):
        provider = _provider()

        with (
            self._transfer_context("ref-stored"),
            patch.object(
                provider,
                "_authed_post",
                new_callable=AsyncMock,
                return_value=(200, {"success": True}),
            ) as post,
        ):
            await provider.transfer_call(
                destination="919111111111",
                transfer_id="tx-1",
                conference_name="c",
                ref_id="ref-explicit",
            )

        assert post.await_args.args[1]["ref_id"] == "ref-explicit"

    @pytest.mark.asyncio
    async def test_no_identifier_anywhere_fails_without_calling_smartflo(self):
        provider = _provider()

        with (
            self._transfer_context(None),
            patch.object(provider, "_authed_post", new_callable=AsyncMock) as post,
        ):
            result = await provider.transfer_call(
                destination="919111111111", transfer_id="tx-1", conference_name="c"
            )

        assert result["status"] == "error"
        post.assert_not_awaited()


# ======== COLLISION SAFETY ========


class TestDoesNotStealOtherProvidersWebhooks:
    """``_detect_provider`` returns the FIRST provider whose claim is true, and
    registration order puts ``tata_smartflo`` fourth of nine:

        ari, cloudonix, plivo, tata_smartflo, telnyx, twilio, vobiz,
        voicelink, vonage

    So an over-broad claim here silently intercepts inbound calls belonging to
    telnyx, twilio, vobiz, voicelink and vonage — routing one customer's call
    into another provider's handling. These payloads mirror the shapes those
    providers actually detect on.
    """

    def test_does_not_claim_a_vonage_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {
                "uuid": "aaaaaaaa-bbbb-cccc-dddd-0123456789ab",
                "conversation_uuid": "CON-1",
                "from": "919111111111",
                "to": "919484959244",
            },
            {},
        )

    def test_does_not_claim_a_voicelink_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {
                "event": "call.answered",
                "call": {"id": "vl-1", "to": "919484959244", "from": "919111111111"},
            },
            {},
        )

    def test_does_not_claim_a_telnyx_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {
                "data": {
                    "record_type": "event",
                    "event_type": "call.initiated",
                    "payload": {"to": "919484959244", "call_control_id": "v3:abc"},
                }
            },
            {"telnyx-signature-ed25519": "sig"},
        )

    def test_does_not_claim_a_vobiz_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"From": "919111111111", "To": "919484959244", "CallUUID": "vb-1"},
            {"user-agent": "vobiz/1.0"},
        )

    def test_does_not_claim_a_cloudonix_webhook(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"From": "919111111111", "To": "919484959244", "CallSid": "cx-1"},
            {"user-agent": "cloudonix-sip/2"},
        )

    def test_a_foreign_marker_beats_a_matching_smartflo_shape(self):
        """The guard fails safe in the direction that matters.

        A payload carrying BOTH SmartFlo-looking keys and another provider's
        marker is refused. A missed SmartFlo call is a support ticket; a stolen
        Twilio call is an outage for a different customer.
        """
        assert not TataSmartfloProvider.can_handle_webhook(
            {"toNumber": "919484959244", "callId": "c-1", "CallSid": "CA123"}, {}
        )

    def test_a_nested_envelope_is_refused_even_with_matching_keys(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"toNumber": "919484959244", "callId": "c-1", "call": {"id": "vl-1"}}, {}
        )

    def test_a_signed_header_from_another_provider_is_refused(self):
        assert not TataSmartfloProvider.can_handle_webhook(
            {"toNumber": "919484959244", "callId": "c-1"},
            {"X-Twilio-Signature": "sig"},
        )

    def test_a_clean_smartflo_payload_still_claims(self):
        """The guard must not have made the provider unreachable."""
        assert TataSmartfloProvider.can_handle_webhook(
            {"callId": "c-1", "fromNumber": "919111111111", "toNumber": "919484959244"},
            {},
        )
