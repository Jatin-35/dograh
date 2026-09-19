"""TATA SmartFlo frame serializer.

SmartFlo's media protocol is a Twilio-Media-Streams-shaped envelope — the same
``connected``/``start``/``media``/``dtmf``/``mark``/``stop`` events, the same
``streamSid`` keying and the same base64 8 kHz mu-law payloads. The *wire keys
below are therefore camelCase*, because that is what SmartFlo puts on the
socket; that similarity is a fact about their protocol, not a dependency on
another provider's code.

This is a full implementation rather than a re-export of
``TwilioFrameSerializer`` because the two protocols only *look* alike:

- **Call control is entirely different.** SmartFlo has no equivalent of
  Twilio's ``POST /Calls/{sid}.json`` REST hangup, so the Twilio serializer's
  built-in termination path is dead weight here — ending a call has to go
  through ``strategies.py``.
- **The credentials are different.** Twilio's serializer validates
  ``account_sid``/``auth_token`` whenever ``auto_hang_up`` is on, and SmartFlo
  has neither: it authenticates with a short-lived bearer minted from
  email/password. Passing SmartFlo's arguments to that constructor raises
  ``ValueError`` before a single frame moves.
- **The identifiers are different.** SmartFlo calls are addressed by ``ref_id``
  (outbound, from Click-to-Call) or ``call_id`` (inbound), not by a Call SID.

So the validation here checks what SmartFlo actually needs, and the strategy
context carries SmartFlo's own identifiers under their own names.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.utils.enums import EndTaskReason

if TYPE_CHECKING:
    from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy

# SmartFlo streams G.711 mu-law at 8 kHz in both directions.
SMARTFLO_WIRE_SAMPLE_RATE = 8000


class SmartfloFrameSerializer(FrameSerializer):
    """Translate between Pipecat frames and SmartFlo's media-stream protocol.

    Outbound we emit ``media`` (audio), ``clear`` (barge-in) and pass through
    ``mark``. Inbound we consume ``media`` and ``dtmf``; ``connected``,
    ``start``, ``mark`` and ``stop`` are lifecycle events with no frame of their
    own, except that ``start`` re-latches the stream id (see ``deserialize``).
    """

    class InputParams(FrameSerializer.InputParams):
        """Configuration for :class:`SmartfloFrameSerializer`.

        Parameters:
            smartflo_sample_rate: Wire sample rate, 8000 Hz.
            sample_rate: Override for the pipeline input rate; defaults to the
                rate on the ``StartFrame``.
            auto_hang_up: End the SmartFlo call when the pipeline finishes.
                Requires a ``hangup_strategy`` — there is no REST fallback.
        """

        smartflo_sample_rate: int = SMARTFLO_WIRE_SAMPLE_RATE
        sample_rate: int | None = None
        auto_hang_up: bool = True

    def __init__(
        self,
        stream_id: str,
        call_id: str | None = None,
        ref_id: str | None = None,
        transfer_strategy: "TransferStrategy | None" = None,
        hangup_strategy: "HangupStrategy | None" = None,
        params: InputParams | None = None,
    ):
        """Initialize the serializer.

        Args:
            stream_id: SmartFlo's ``streamSid`` for this socket.
            call_id: SmartFlo's call identifier. Present for inbound; for
                outbound the authoritative identifier is ``ref_id``.
            ref_id: The reference returned by Click-to-Call. Outbound only.
            transfer_strategy: Handles ``EndTaskReason.TRANSFER_CALL``.
            hangup_strategy: Ends the call. Required when ``auto_hang_up``
                is set, because SmartFlo's streaming protocol carries no
                hangup event and this serializer has no REST fallback of its
                own — without it the agent could decide to end a call and the
                caller would simply be left on a silent line.
            params: Configuration parameters.
        """
        params = params or SmartfloFrameSerializer.InputParams()
        super().__init__(params)
        self._params: SmartfloFrameSerializer.InputParams = params

        if self._params.auto_hang_up and hangup_strategy is None:
            raise ValueError(
                "auto_hang_up is enabled but no hangup_strategy was supplied. "
                "SmartFlo's media protocol has no hangup event and this "
                "serializer has no REST fallback, so the call could never be "
                "ended from our side."
            )

        if not stream_id:
            raise ValueError("SmartFlo serializer requires a stream_id.")

        if not (call_id or ref_id):
            raise ValueError(
                "SmartFlo serializer requires call_id or ref_id — one of them "
                "is needed to address the call for hangup and transfer."
            )

        self._stream_id = stream_id
        self._call_id = call_id
        self._ref_id = ref_id
        self._transfer_strategy = transfer_strategy
        self._hangup_strategy = hangup_strategy

        self._wire_sample_rate = self._params.smartflo_sample_rate
        self._sample_rate = 0  # Pipeline input rate; set in setup().

        self._input_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._output_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._hangup_attempted = False
        self._transfer_attempted = False

    async def setup(self, frame: StartFrame):
        """Adopt the pipeline's input sample rate."""
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate

    def _call_context(self) -> dict[str, Any]:
        """Identifiers handed to the call-control strategies.

        SmartFlo's own names. ``ref_id`` addresses an outbound call and
        ``call_id`` an inbound one; ``strategies.py`` prefers ref_id and falls
        back to call_id, which also matches how the transfer tool keys its
        Redis context off ``gathered_context["call_id"]``.
        """
        return {
            "ref_id": self._ref_id,
            "call_id": self._call_id,
            "stream_id": self._stream_id,
        }

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Convert a Pipecat frame into a SmartFlo socket message."""
        if isinstance(frame, (EndFrame, CancelFrame)):
            frame_reason = getattr(frame, "reason", None)
            logger.debug(
                f"SmartFlo serializer handling {type(frame).__name__} "
                f"reason={frame_reason}"
            )

            if (
                frame_reason == EndTaskReason.TRANSFER_CALL.value
                and not self._transfer_attempted
            ):
                self._transfer_attempted = True
                if self._transfer_strategy:
                    if not await self._transfer_strategy.execute_transfer(
                        self._call_context()
                    ):
                        logger.error(
                            f"SmartFlo transfer failed for {self._identifier_label()}"
                        )
                else:
                    logger.warning(
                        f"No SmartFlo transfer strategy for {self._identifier_label()}"
                    )
                return None

            # A transfer already moved the call away from us; hanging up here
            # would cut off the leg we just handed to someone else.
            if (
                self._params.auto_hang_up
                and not self._hangup_attempted
                and frame_reason != EndTaskReason.TRANSFER_CALL.value
            ):
                self._hangup_attempted = True
                # __init__ rejects auto_hang_up without a strategy, so this is
                # never None here — checked rather than asserted because `-O`
                # strips assertions and this runs while a caller is on the line.
                if self._hangup_strategy and not await self._hangup_strategy.execute_hangup(
                    self._call_context()
                ):
                    logger.error(
                        f"SmartFlo hangup failed for {self._identifier_label()}"
                    )
            return None

        if isinstance(frame, InterruptionFrame):
            # Barge-in: drop whatever SmartFlo has buffered for playback.
            return json.dumps({"event": "clear", "streamSid": self._stream_id})

        if isinstance(frame, AudioRawFrame):
            serialized = await pcm_to_ulaw(
                frame.audio,
                frame.sample_rate,
                self._wire_sample_rate,
                self._output_resampler,
            )
            if not serialized:
                return None

            return json.dumps(
                {
                    "event": "media",
                    "streamSid": self._stream_id,
                    "media": {"payload": base64.b64encode(serialized).decode("utf-8")},
                }
            )

        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Convert a SmartFlo socket message into a Pipecat frame.

        Every unknown or malformed message is dropped rather than raised on: a
        live call must not die because SmartFlo sent an event we do not model.
        """
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(f"SmartFlo sent a non-JSON frame: {exc}")
            return None

        if not isinstance(message, dict):
            return None

        event = message.get("event")

        if event == "media":
            payload = message.get("media", {}).get("payload")
            if not payload:
                return None
            try:
                audio = base64.b64decode(payload)
            except (ValueError, TypeError) as exc:
                logger.warning(f"SmartFlo media payload was not valid base64: {exc}")
                return None

            deserialized = await ulaw_to_pcm(
                audio,
                self._wire_sample_rate,
                self._sample_rate,
                self._input_resampler,
            )
            if not deserialized:
                return None

            return InputAudioRawFrame(
                audio=deserialized, num_channels=1, sample_rate=self._sample_rate
            )

        if event == "dtmf":
            digit = message.get("dtmf", {}).get("digit")
            try:
                return InputDTMFFrame(KeypadEntry(digit))
            except ValueError:
                logger.debug(f"SmartFlo sent an unmapped DTMF digit: {digit!r}")
                return None

        if event == "start":
            # Re-latch the stream id from the socket itself. The id we were
            # constructed with came from the connect request; if SmartFlo's
            # start event disagrees, every outgoing frame keyed on the old
            # value would be silently discarded by them.
            started = message.get("streamSid") or message.get("start", {}).get(
                "streamSid"
            )
            if started and started != self._stream_id:
                logger.warning(
                    f"SmartFlo start event carries streamSid={started}, replacing "
                    f"{self._stream_id} from the connect request."
                )
                self._stream_id = started
            return None

        return None

    def _identifier_label(self) -> str:
        """How this call is named in logs — never a phone number."""
        if self._ref_id:
            return f"ref_id={self._ref_id}"
        return f"call_id={self._call_id}"


__all__ = ["SmartfloFrameSerializer", "SMARTFLO_WIRE_SAMPLE_RATE"]
