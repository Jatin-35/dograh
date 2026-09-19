"""The TATA SmartFlo lifecycle webhook.

Note what is *not* tested here: an inbound connect endpoint. There isn't one.
SmartFlo's voice-streaming request is claimed on the shared
``/api/v1/telephony/inbound/run`` dispatcher via ``can_handle_webhook``, so the
DID lookup, signature check, concurrency slot, run creation and
``authorize_workflow_run_start`` are the shared implementation rather than a
copy. Upstream PR #731 wrote its own endpoint and omitted every one of them.

What remains here is the webhook that closes a run out, and its job is mostly
to be hard to knock over: SmartFlo retries a non-response twice, so a parsing
bug of ours must not become a retry storm.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.telephony.providers.tata_smartflo import routes as smartflo_routes
from api.services.telephony.providers.tata_smartflo.provider import (
    TataSmartfloProvider,
)


def _request(body: bytes = b"{}", url: str = "https://example.test/hook"):
    request = SimpleNamespace()
    request.body = AsyncMock(return_value=body)
    request.url = url
    return request


def _run(run_id: int = 123, workflow_id: int = 7):
    return SimpleNamespace(id=run_id, workflow_id=workflow_id)


def _provider(verified: bool = True):
    """A stub whose parsing is the *real* implementation.

    Hand-rolling the parse here once hid a missing ``call_id`` and made the
    handoff assertion pass against a shape the provider never produces.
    """
    provider = SimpleNamespace()
    provider.verify_webhook_signature = AsyncMock(return_value=verified)
    provider.parse_status_callback = TataSmartfloProvider.parse_status_callback.__get__(
        provider, SimpleNamespace
    )
    return provider


_HANGUP_EVENT = {
    "$ref_id": "504bb41c",
    "$status": "hangup",
    "$duration": "42",
    "$hangup_cause": "NORMAL_CLEARING",
    "$caller_id_number": "919111111111",
    "$call_to_number": "919484959244",
}


@pytest.mark.asyncio
async def test_a_hangup_event_is_matched_to_its_run_by_ref_id():
    with (
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ) as by_call_id,
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_by_id",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(organization_id=11),
        ),
        patch.object(
            smartflo_routes,
            "get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=_provider(),
        ),
        patch.object(
            smartflo_routes, "_process_status_update", new_callable=AsyncMock
        ) as process,
    ):
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(_HANGUP_EVENT).encode())
        )

    # The indexed lookup, not a scan.
    by_call_id.assert_awaited_once_with("504bb41c")
    assert result == {"status": "success"}

    # The figures only SmartFlo knows reach the run through the shared
    # processor. Without this handoff the call still ends when the socket
    # closes, but billed duration, hangup cause and the recording are lost.
    process.assert_awaited_once()
    run_id, update = process.await_args.args
    assert run_id == 123
    assert update.call_id == "504bb41c"
    assert update.duration == "42"
    assert update.extra["hangup_cause"] == "NORMAL_CLEARING"


@pytest.mark.asyncio
async def test_custom_identifier_wins_over_ref_id():
    """Outbound echoes the run id through custom_identifier.

    Preferring it avoids a database round-trip, and it is the only route home
    for a call whose ref_id was never persisted.
    """
    event = {**_HANGUP_EVENT, "custom_identifier": {"workflow_run_id": "4242"}}

    with (
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
        ) as by_call_id,
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=_run(run_id=4242),
        ) as by_id,
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_by_id",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(organization_id=11),
        ),
        patch.object(
            smartflo_routes,
            "get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=_provider(),
        ),
        patch.object(
            smartflo_routes, "_process_status_update", new_callable=AsyncMock
        ) as process,
    ):
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(event).encode())
        )

    by_call_id.assert_not_awaited()
    by_id.assert_awaited_once_with(4242)
    assert result == {"status": "success"}


@pytest.mark.asyncio
async def test_a_non_numeric_custom_identifier_falls_back_to_ref_id():
    """Never coerce junk into a run id — it would address another run."""
    event = {**_HANGUP_EVENT, "custom_identifier": {"workflow_run_id": "not-a-number"}}

    with (
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ) as by_call_id,
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_by_id",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(organization_id=11),
        ),
        patch.object(
            smartflo_routes,
            "get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=_provider(),
        ),
        patch.object(
            smartflo_routes, "_process_status_update", new_callable=AsyncMock
        ) as process,
    ):
        await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(event).encode())
        )

    by_call_id.assert_awaited_once_with("504bb41c")


@pytest.mark.asyncio
async def test_an_event_for_an_unknown_call_is_ignored_not_an_error():
    """SmartFlo fires 19-odd triggers; several are for calls we never placed."""
    with patch.object(
        smartflo_routes.db_client,
        "get_workflow_run_by_call_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(_HANGUP_EVENT).encode())
        )

    assert result == {"status": "ignored", "reason": "run_not_found"}


@pytest.mark.asyncio
async def test_a_failed_secret_check_is_refused():
    with (
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_by_id",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(organization_id=11),
        ),
        patch.object(
            smartflo_routes,
            "get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=_provider(verified=False),
        ),
    ):
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(_HANGUP_EVENT).encode())
        )

    assert result == {"status": "error", "reason": "verification_failed"}


class TestMalformedPayloads:
    """SmartFlo retries a non-response twice (30s, then 10s).

    So every one of these must answer rather than raise — our own parsing bug
    should not become three requests instead of one.
    """

    @pytest.mark.asyncio
    async def test_invalid_json_answers_instead_of_raising(self):
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(b"{not json")
        )
        assert result == {"status": "error", "reason": "invalid_json"}

    @pytest.mark.asyncio
    async def test_an_empty_body_answers(self):
        with patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await smartflo_routes.handle_tata_smartflo_events(_request(b""))
        assert result["status"] in {"ignored", "error"}

    @pytest.mark.asyncio
    async def test_a_json_array_is_reported_as_a_content_type_problem(self):
        """Means the webhook is configured form-encoded in the SmartFlo portal."""
        result = await smartflo_routes.handle_tata_smartflo_events(
            _request(b'["not", "an", "object"]')
        )
        assert result == {"status": "error", "reason": "unexpected_payload"}


@pytest.mark.asyncio
async def test_phone_numbers_are_never_logged():
    """PR #731 was flagged for logging caller and customer numbers verbatim."""
    with (
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_call_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=_run(),
        ),
        patch.object(
            smartflo_routes.db_client,
            "get_workflow_by_id",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(organization_id=11),
        ),
        patch.object(
            smartflo_routes,
            "get_telephony_provider_for_run",
            new_callable=AsyncMock,
            return_value=_provider(),
        ),
        patch.object(smartflo_routes, "logger") as logger,
        patch.object(smartflo_routes, "_process_status_update", new_callable=AsyncMock),
    ):
        await smartflo_routes.handle_tata_smartflo_events(
            _request(json.dumps(_HANGUP_EVENT).encode())
        )

    logged = " ".join(str(c) for c in logger.info.call_args_list)
    assert "919111111111" not in logged
    assert "919484959244" not in logged
    # ...but the operationally useful fields are there.
    assert "hangup" in logged or "NORMAL_CLEARING" in logged
