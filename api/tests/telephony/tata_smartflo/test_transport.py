"""The SmartFlo transport factory.

The serializer had a unit test for its own construction and the provider had
one for its behaviour, but nothing ever ran ``create_transport`` — so nothing
noticed that the arguments it passed could not build a serializer at all. That
is the specific gap these tests close: this is the only place the real call
site is exercised end to end.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# The client object re-exported from api/db/__init__.py, not the module that
# shares its name — ``transport.py`` imports the object, so that is what has
# to be patched.
from api.db import db_client
from api.services.telephony.providers.tata_smartflo import transport as smartflo_transport
from api.services.telephony.providers.tata_smartflo.serializers import (
    SmartfloFrameSerializer,
)

_CREDENTIALS = {
    "api_base": "https://api-smartflo.tatateleservices.com",
    "email": "ops@example.test",
    "password": "placeholder-password",
}


class _AudioConfig(SimpleNamespace):
    pipeline_sample_rate = 16000
    transport_in_sample_rate = 8000
    transport_out_sample_rate = 8000


def _patches(credentials=None, gathered_context=None):
    """Everything create_transport reaches outside its own package."""
    run = SimpleNamespace(gathered_context=gathered_context or {})
    return (
        patch.object(
            smartflo_transport,
            "load_credentials_for_transport",
            new_callable=AsyncMock,
            return_value=_CREDENTIALS if credentials is None else credentials,
        ),
        patch.object(
            smartflo_transport,
            "build_audio_out_mixer",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(
            smartflo_transport, "FastAPIWebsocketTransport", autospec=True
        ),
        patch.object(
            db_client,
            "get_workflow_run_by_id",
            new_callable=AsyncMock,
            return_value=run,
        ),
    )


async def _create(**overrides):
    creds = overrides.pop("credentials", None)
    gathered = overrides.pop("gathered_context", None)
    load, mixer, transport_cls, _run = _patches(creds, gathered)

    with load, mixer, transport_cls as transport_mock, _run:
        await smartflo_transport.create_transport(
            websocket=SimpleNamespace(),
            workflow_run_id=1,
            audio_config=_AudioConfig(),
            organization_id=9,
            stream_id=overrides.pop("stream_id", "stream-1"),
            call_id=overrides.pop("call_id", "call-1"),
            **overrides,
        )
    # The serializer handed to pipecat.
    return transport_mock.call_args.kwargs["params"].serializer


@pytest.mark.asyncio
async def test_a_transport_can_actually_be_built():
    """The regression.

    While this provider re-exported ``TwilioFrameSerializer``, this call raised
    ``ValueError: auto_hang_up is enabled but missing required parameters:
    account_sid, auth_token`` — Twilio's credential check applied to a provider
    that authenticates with email and password. Every SmartFlo call, inbound
    and outbound, would have failed here before any audio moved.
    """
    serializer = await _create()
    assert isinstance(serializer, SmartfloFrameSerializer)


@pytest.mark.asyncio
async def test_the_serializer_is_wired_to_smartflos_own_strategies():
    """Hangup and transfer must reach SmartFlo's REST API, not Twilio's."""
    serializer = await _create()

    assert type(serializer._hangup_strategy).__name__ == "TataSmartfloHangupStrategy"
    assert type(serializer._transfer_strategy).__name__ == "TataSmartfloTransferStrategy"


@pytest.mark.asyncio
async def test_an_outbound_call_carries_its_ref_id_into_the_serializer():
    """ref_id lands in gathered_context, not extra — reading the wrong one is silent."""
    serializer = await _create(gathered_context={"smartflo_ref_id": "ref-42"})
    assert serializer._ref_id == "ref-42"


@pytest.mark.asyncio
async def test_an_inbound_call_has_no_ref_id_and_uses_the_call_id():
    """There was no initiation call, so nothing ever returned a ref_id."""
    serializer = await _create(gathered_context={})
    assert serializer._ref_id is None
    assert serializer._call_id == "call-1"


@pytest.mark.asyncio
async def test_the_wire_rate_stays_8k_while_the_pipeline_runs_at_its_own_rate():
    """Resampling between the two is the serializer's job; conflating them is silent
    distortion rather than an error."""
    serializer = await _create()

    assert serializer._wire_sample_rate == 8000
    assert serializer._params.sample_rate == _AudioConfig.pipeline_sample_rate


@pytest.mark.asyncio
async def test_missing_credentials_are_refused_before_a_call_starts():
    """Without email/password the call could be answered but never hung up."""
    with pytest.raises(ValueError, match="email and password are required"):
        await _create(credentials={"api_base": _CREDENTIALS["api_base"]})
