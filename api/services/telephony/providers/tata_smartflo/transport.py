"""TATA SmartFlo transport factory.

Builds the websocket transport for a SmartFlo call: SmartFlo's own serializer
(``serializers.py``) wired to SmartFlo's own call-control strategies
(``strategies.py``), with credentials resolved per-organization.

``auto_hang_up`` stays on because the agent deciding to end a call has to reach
SmartFlo somehow and their media socket carries no hangup event — the hangup
strategy is what makes that reachable, which is why the serializer refuses to
be built with one enabled and the other missing.
"""

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

from .serializers import SMARTFLO_WIRE_SAMPLE_RATE, SmartfloFrameSerializer
from .strategies import TataSmartfloHangupStrategy, TataSmartfloTransferStrategy


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
    """Create a transport for a TATA SmartFlo connection."""
    logger.info(
        f"[run {workflow_run_id}] Creating SmartFlo transport - "
        f"stream_id={stream_id}, call_id={call_id}"
    )

    config = await load_credentials_for_transport(
        organization_id, telephony_configuration_id, expected_provider="tata_smartflo"
    )

    api_base = config.get("api_base")
    email = config.get("email")
    password = config.get("password")

    if not email or not password:
        raise ValueError(
            f"Incomplete SmartFlo configuration for organization {organization_id}: "
            "email and password are required to mint the bearer token used for "
            "hangup and transfer."
        )

    # ref_id is only known for outbound calls, where the Click-to-Call response
    # returned it and the run recorded it. Inbound has none, so the strategies
    # fall back to the callSid the serializer already holds.
    ref_id = await _ref_id_for_run(workflow_run_id)

    strategy_args = {
        "api_base": api_base,
        "email": email,
        "password": password,
        "ref_id": ref_id,
    }

    serializer = SmartfloFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        ref_id=ref_id,
        hangup_strategy=TataSmartfloHangupStrategy(**strategy_args),
        transfer_strategy=TataSmartfloTransferStrategy(**strategy_args),
        params=SmartfloFrameSerializer.InputParams(
            smartflo_sample_rate=SMARTFLO_WIRE_SAMPLE_RATE,
            sample_rate=audio_config.pipeline_sample_rate,
            auto_hang_up=True,
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

    logger.info(f"[run {workflow_run_id}] SmartFlo transport created successfully")
    return transport


async def _ref_id_for_run(workflow_run_id: int) -> str | None:
    """Read back the SmartFlo ``ref_id`` recorded when the call was placed.

    It lives in ``gathered_context``, not ``extra``: the outbound route merges
    ``CallInitiationResult.provider_metadata`` into ``gathered_context``
    (api/routes/telephony.py), so whatever ``initiate_call`` returns there is
    where it lands.

    Read from the database rather than held in memory because the pipeline
    worker serving this socket is not necessarily the process that placed the
    call. Absent for inbound — there was no initiation — and absent for outbound
    only if initiation failed; in both cases the strategies fall back to
    ``call_id``.
    """
    from api.db import db_client

    try:
        run = await db_client.get_workflow_run_by_id(workflow_run_id)
    except Exception as exc:  # noqa: BLE001 - never fail a live call over this
        logger.warning(f"[run {workflow_run_id}] Could not read SmartFlo ref_id: {exc}")
        return None

    if run is None:
        return None

    gathered = getattr(run, "gathered_context", None) or {}
    ref_id = gathered.get("smartflo_ref_id")
    if ref_id:
        logger.debug(f"[run {workflow_run_id}] SmartFlo ref_id={ref_id}")
    return ref_id
