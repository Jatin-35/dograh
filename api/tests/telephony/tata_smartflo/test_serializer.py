"""SmartFlo's own frame serializer.

This provider used to re-export ``TwilioFrameSerializer`` on the grounds that
SmartFlo's media protocol is Twilio-shaped. The wire format is — the call
control is not, and the credentials are not. Nothing instantiated the
serializer in a test, so the mismatch stayed invisible: the transport passed
``auto_hang_up=True`` with a ``call_sid`` but no ``account_sid``/``auth_token``,
and Twilio's constructor raises on exactly that. Every SmartFlo call would have
died before the first frame moved.

So the first test here is the one that was missing.
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.utils.enums import EndTaskReason

from api.services.telephony.providers.tata_smartflo.serializers import (
    SMARTFLO_WIRE_SAMPLE_RATE,
    SmartfloFrameSerializer,
)

# 20 ms of silence at 8 kHz, 16-bit mono — one telephony frame.
_SILENCE = b"\x00\x00" * 160


def _strategies():
    hangup = SimpleNamespace(execute_hangup=AsyncMock(return_value=True))
    transfer = SimpleNamespace(execute_transfer=AsyncMock(return_value=True))
    return hangup, transfer


def _serializer(*, stream_id="stream-1", call_id="call-1", ref_id=None, **kwargs):
    hangup, transfer = _strategies()
    serializer = SmartfloFrameSerializer(
        stream_id=stream_id,
        call_id=call_id,
        ref_id=ref_id,
        hangup_strategy=kwargs.pop("hangup_strategy", hangup),
        transfer_strategy=kwargs.pop("transfer_strategy", transfer),
        params=SmartfloFrameSerializer.InputParams(
            smartflo_sample_rate=SMARTFLO_WIRE_SAMPLE_RATE,
            sample_rate=SMARTFLO_WIRE_SAMPLE_RATE,
            **kwargs,
        ),
    )
    return serializer, hangup, transfer


async def _ready(serializer):
    """Run the setup a StartFrame would drive, without building one."""
    await serializer.setup(SimpleNamespace(audio_in_sample_rate=SMARTFLO_WIRE_SAMPLE_RATE))
    return serializer


class TestConstruction:
    def test_the_transport_arguments_actually_build_a_serializer(self):
        """The regression. These are exactly what ``transport.py`` passes.

        Under ``TwilioFrameSerializer`` this raised ValueError — auto_hang_up
        demanded Twilio credentials SmartFlo does not possess — so no call
        could ever start.
        """
        serializer, _, _ = _serializer(ref_id="ref-1", auto_hang_up=True)
        assert serializer is not None

    def test_auto_hang_up_without_a_hangup_strategy_is_refused(self):
        """SmartFlo has no REST fallback inside the serializer.

        Twilio's would quietly fall through to its own API call. Here, a
        missing strategy means the agent could decide to end a call and the
        caller would be left on a silent line, so it fails loudly at build
        time instead.
        """
        with pytest.raises(ValueError, match="no hangup_strategy"):
            SmartfloFrameSerializer(
                stream_id="stream-1",
                call_id="call-1",
                hangup_strategy=None,
                params=SmartfloFrameSerializer.InputParams(auto_hang_up=True),
            )

    def test_auto_hang_up_off_needs_no_strategy(self):
        serializer = SmartfloFrameSerializer(
            stream_id="stream-1",
            call_id="call-1",
            params=SmartfloFrameSerializer.InputParams(auto_hang_up=False),
        )
        assert serializer is not None

    def test_a_call_with_no_identifier_at_all_is_refused(self):
        """Without ref_id or call_id the call can never be hung up or transferred."""
        with pytest.raises(ValueError, match="call_id or ref_id"):
            SmartfloFrameSerializer(
                stream_id="stream-1",
                call_id=None,
                ref_id=None,
                params=SmartfloFrameSerializer.InputParams(auto_hang_up=False),
            )

    def test_a_missing_stream_id_is_refused(self):
        with pytest.raises(ValueError, match="stream_id"):
            SmartfloFrameSerializer(
                stream_id="",
                call_id="call-1",
                params=SmartfloFrameSerializer.InputParams(auto_hang_up=False),
            )


class TestOutgoing:
    @pytest.mark.asyncio
    async def test_audio_becomes_a_media_event_keyed_by_stream_sid(self):
        """SmartFlo's envelope is camelCase on the wire; that is their protocol."""
        serializer, _, _ = _serializer()
        await _ready(serializer)

        result = await serializer.serialize(
            OutputAudioRawFrame(
                audio=_SILENCE, sample_rate=SMARTFLO_WIRE_SAMPLE_RATE, num_channels=1
            )
        )

        message = json.loads(result)
        assert message["event"] == "media"
        assert message["streamSid"] == "stream-1"
        # mu-law is one byte per sample against PCM's two.
        assert len(base64.b64decode(message["media"]["payload"])) == len(_SILENCE) // 2

    @pytest.mark.asyncio
    async def test_an_interruption_clears_buffered_playback(self):
        serializer, _, _ = _serializer()
        await _ready(serializer)

        message = json.loads(await serializer.serialize(InterruptionFrame()))
        assert message == {"event": "clear", "streamSid": "stream-1"}

    @pytest.mark.asyncio
    async def test_silence_that_resamples_to_nothing_sends_no_frame(self):
        serializer, _, _ = _serializer()
        await _ready(serializer)

        result = await serializer.serialize(
            OutputAudioRawFrame(
                audio=b"", sample_rate=SMARTFLO_WIRE_SAMPLE_RATE, num_channels=1
            )
        )
        assert result is None


class TestIncoming:
    @pytest.mark.asyncio
    async def test_media_becomes_an_input_audio_frame(self):
        serializer, _, _ = _serializer()
        await _ready(serializer)

        # Round-trip our own encoder so the payload is genuinely mu-law.
        encoded = json.loads(
            await serializer.serialize(
                OutputAudioRawFrame(
                    audio=_SILENCE, sample_rate=SMARTFLO_WIRE_SAMPLE_RATE, num_channels=1
                )
            )
        )["media"]["payload"]

        frame = await serializer.deserialize(
            json.dumps({"event": "media", "media": {"payload": encoded}})
        )

        assert isinstance(frame, InputAudioRawFrame)
        assert frame.sample_rate == SMARTFLO_WIRE_SAMPLE_RATE
        assert frame.num_channels == 1

    @pytest.mark.asyncio
    async def test_a_dtmf_digit_becomes_a_dtmf_frame(self):
        serializer, _, _ = _serializer()
        await _ready(serializer)

        frame = await serializer.deserialize(
            json.dumps({"event": "dtmf", "dtmf": {"digit": "5"}})
        )
        assert isinstance(frame, InputDTMFFrame)

    @pytest.mark.asyncio
    async def test_the_start_event_relatches_the_stream_sid(self):
        """The connect request's id is a guess until the socket confirms it.

        If SmartFlo's start event names a different streamSid, every media
        frame keyed on the old one is discarded on their side — a call that
        connects and then stays silent.
        """
        serializer, _, _ = _serializer()
        await _ready(serializer)

        await serializer.deserialize(
            json.dumps({"event": "start", "streamSid": "stream-authoritative"})
        )

        message = json.loads(await serializer.serialize(InterruptionFrame()))
        assert message["streamSid"] == "stream-authoritative"

    @pytest.mark.asyncio
    async def test_the_stream_sid_is_also_read_from_a_nested_start_block(self):
        serializer, _, _ = _serializer()
        await _ready(serializer)

        await serializer.deserialize(
            json.dumps({"event": "start", "start": {"streamSid": "stream-nested"}})
        )

        message = json.loads(await serializer.serialize(InterruptionFrame()))
        assert message["streamSid"] == "stream-nested"

    @pytest.mark.parametrize(
        "payload",
        [
            b"{not json",
            b'["a", "list"]',
            b'{"event": "connected"}',
            b'{"event": "stop"}',
            b'{"event": "mark", "mark": {"name": "x"}}',
            b'{"no_event_key": true}',
            b'{"event": "media", "media": {}}',
            b'{"event": "media", "media": {"payload": "!!!not-base64!!!"}}',
            b'{"event": "dtmf", "dtmf": {"digit": "Z"}}',
        ],
    )
    @pytest.mark.asyncio
    async def test_unmodelled_or_malformed_messages_are_dropped_not_raised(self, payload):
        """A live call must not die because SmartFlo sent something unexpected.

        The contract with Tata is unverified in places, so every one of these
        is a shape we might genuinely receive.
        """
        serializer, _, _ = _serializer()
        await _ready(serializer)

        assert await serializer.deserialize(payload) is None


class TestCallControl:
    @pytest.mark.asyncio
    async def test_ending_the_call_invokes_the_hangup_strategy(self):
        serializer, hangup, _ = _serializer(ref_id="ref-1")
        await _ready(serializer)

        await serializer.serialize(EndFrame())

        hangup.execute_hangup.assert_awaited_once()
        context = hangup.execute_hangup.await_args.args[0]
        assert context["ref_id"] == "ref-1"
        assert context["call_id"] == "call-1"

    @pytest.mark.asyncio
    async def test_the_strategy_context_uses_smartflo_names_only(self):
        """Guards the rename. ``strategies.py`` reads ref_id/call_id now."""
        serializer, hangup, _ = _serializer()
        await _ready(serializer)

        await serializer.serialize(EndFrame())

        context = hangup.execute_hangup.await_args.args[0]
        assert "call_sid" not in context
        assert "account_sid" not in context
        assert "auth_token" not in context

    @pytest.mark.asyncio
    async def test_hangup_is_attempted_once_even_across_several_end_frames(self):
        """A second hangup would spend rate-limit budget a queued call needs."""
        serializer, hangup, _ = _serializer()
        await _ready(serializer)

        await serializer.serialize(EndFrame())
        await serializer.serialize(CancelFrame())

        hangup.execute_hangup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_transfer_goes_to_the_transfer_strategy_and_never_hangs_up(self):
        """Hanging up here would cut the leg we just handed to someone else."""
        serializer, hangup, transfer = _serializer(ref_id="ref-1")
        await _ready(serializer)

        await serializer.serialize(
            EndFrame(reason=EndTaskReason.TRANSFER_CALL.value)
        )

        transfer.execute_transfer.assert_awaited_once()
        hangup.execute_hangup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_transfer_frame_still_does_not_hang_up(self):
        """The first transfer frame returns early; a later one falls through.

        Once ``_transfer_attempted`` is set, a repeat EndFrame reaches the
        hangup branch, and only the reason check stops it there. Without that
        check we would hang up a call that was already handed to an agent —
        the caller and the agent both hear the line die.
        """
        serializer, hangup, transfer = _serializer(ref_id="ref-1")
        await _ready(serializer)

        reason = EndTaskReason.TRANSFER_CALL.value
        await serializer.serialize(EndFrame(reason=reason))
        await serializer.serialize(EndFrame(reason=reason))

        transfer.execute_transfer.assert_awaited_once()
        hangup.execute_hangup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_hangup_strategy_does_not_raise(self):
        """The pipeline is shutting down; an exception here helps nobody."""
        hangup = SimpleNamespace(execute_hangup=AsyncMock(return_value=False))
        serializer, _, _ = _serializer(hangup_strategy=hangup)
        await _ready(serializer)

        assert await serializer.serialize(EndFrame()) is None

    @pytest.mark.asyncio
    async def test_no_hangup_is_attempted_when_auto_hang_up_is_off(self):
        hangup = SimpleNamespace(execute_hangup=AsyncMock(return_value=True))
        serializer, _, _ = _serializer(hangup_strategy=hangup, auto_hang_up=False)
        await _ready(serializer)

        await serializer.serialize(EndFrame())

        hangup.execute_hangup.assert_not_awaited()
