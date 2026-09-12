"""VoiceLink transport factory.

VoiceLink uses a Twilio-media-streams-style WebSocket protocol:
- G.711 A-law audio at 8 kHz (NOT µ-law)
- Base64-encoded audio in JSON messages
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import WebSocket
from loguru import logger
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.audio_mixer import build_audio_out_mixer
from api.services.pipecat.transport_params import realtime_param_overrides
from api.services.telephony.factory import load_credentials_for_transport

from .serializers import VoiceLinkFrameSerializer

# ======== LIVE CONNECTION REGISTRY ========
#
# `transfer_call()` on VoiceLinkProvider needs to push a message onto *this
# specific call's* already-open media WebSocket, but the provider instance it
# runs on is a fresh one built from stored credentials (see
# `get_telephony_provider_for_run`) with no reference to the live transport.
#
# This in-process, VoiceLink-local registry bridges that gap: the live output
# transport (plus the ids needed to build a wire message) is stashed here,
# keyed by the same call identifier VoiceLink itself uses, for the lifetime
# of the call. `transfer_call()` looks it up to reach the live socket.
#
# This only works because the transfer is requested by code running inside
# the same live call's own pipeline task, in the same process that holds the
# transport - never across a process/worker boundary.


@dataclass
class _ActiveVoiceLinkCall:
    output_transport: Any
    stream_sid: str
    call_sid: str


_active_calls: Dict[str, _ActiveVoiceLinkCall] = {}


def register_active_call(
    call_key: str, output_transport: Any, *, stream_sid: str, call_sid: str
) -> None:
    """Record the live output transport for an in-progress VoiceLink call."""
    if not call_key:
        logger.warning(
            "VoiceLink transport has no call_sid/stream_sid to register "
            "against - transfer_call() won't be able to find this call"
        )
        return
    _active_calls[call_key] = _ActiveVoiceLinkCall(
        output_transport=output_transport, stream_sid=stream_sid, call_sid=call_sid
    )


def unregister_active_call(call_key: str) -> None:
    """Drop the live output transport reference once the call ends."""
    if call_key:
        _active_calls.pop(call_key, None)


def get_active_call(call_key: str) -> Optional[_ActiveVoiceLinkCall]:
    """Look up the live output transport for a VoiceLink call, if still connected."""
    return _active_calls.get(call_key) if call_key else None


async def create_transport(
    websocket: WebSocket,
    workflow_run_id: int,
    audio_config: AudioConfig,
    organization_id: int,
    *,
    ambient_noise_config: dict | None = None,
    telephony_configuration_id: int | None = None,
    is_realtime: bool = False,
    stream_id: str,
    call_id: str,
):
    """Create a transport for VoiceLink connections."""
    logger.info(
        f"[run {workflow_run_id}] Creating VoiceLink transport - "
        f"stream_sid={stream_id}, call_sid={call_id}"
    )

    # The serializer needs no credentials (VoiceLink has no per-call REST
    # hangup), but resolving the config validates the run is bound to a
    # VoiceLink configuration for this organization.
    await load_credentials_for_transport(
        organization_id, telephony_configuration_id, expected_provider="voicelink"
    )

    serializer = VoiceLinkFrameSerializer(
        stream_sid=stream_id,
        call_sid=call_id,
        params=VoiceLinkFrameSerializer.InputParams(
            voicelink_sample_rate=8000,
            sample_rate=audio_config.pipeline_sample_rate,
        ),
    )

    mixer = await build_audio_out_mixer(
        audio_config.transport_out_sample_rate, ambient_noise_config
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=audio_config.transport_out_sample_rate,
            audio_out_mixer=mixer,
            serializer=serializer,
            **realtime_param_overrides(is_realtime),
        ),
    )

    call_key = call_id or stream_id
    register_active_call(
        call_key, transport.output(), stream_sid=stream_id, call_sid=call_id
    )
    logger.info(
        f"[run {workflow_run_id}] VoiceLink transport created successfully "
        f"(registered live call under key={call_key!r})"
    )
    return transport
