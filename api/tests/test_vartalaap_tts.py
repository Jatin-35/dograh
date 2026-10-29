"""Vartalaap text-to-speech: settings, factory, key check and the WebSocket protocol."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    ErrorFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import SleepFrame, run_test
from websockets.asyncio.server import serve

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    REGISTRY,
    ServiceProviders,
    ServiceType,
    VartalaapTTSConfiguration,
)
from api.services.pipecat.provider_rejection import provider_http_status
from api.services.pipecat.service_factory import create_tts_service
from api.services.pipecat.vartalaap_tts import VARTALAAP_SAMPLE_RATE, VartalaapTTSService

# ---------------------------------------------------------------------------
# Settings, factory, key check
# ---------------------------------------------------------------------------


def test_defaults():
    config = VartalaapTTSConfiguration(api_key="k")
    assert config.provider == ServiceProviders.VARTALAAP
    assert (config.voice, config.speed, config.profile, config.digits, config.temperature) == (
        "saanvi",
        1.0,
        "default",
        "hi",
        0.3,
    )
    assert config.model == "vartalaap"


def test_registered_as_a_voice_provider():
    assert REGISTRY[ServiceType.TTS][ServiceProviders.VARTALAAP] is VartalaapTTSConfiguration


@pytest.mark.parametrize(
    "field,value", [("speed", 2.5), ("speed", 0.4), ("profile", "radio"), ("digits", "fr"), ("temperature", 3)]
)
def test_out_of_range_values_are_refused(field, value):
    with pytest.raises(ValueError):
        VartalaapTTSConfiguration(api_key="k", **{field: value})


def test_the_form_offers_the_choices():
    props = VartalaapTTSConfiguration.model_json_schema()["properties"]
    assert props["profile"]["enum"] == ["default", "telephony"]
    assert props["digits"]["enum"] == ["hi", "mixed", "en"]
    assert "saanvi" in props["voice"]["examples"]


def _user_config(**tts):
    base = dict(
        provider=ServiceProviders.VARTALAAP.value,
        model="vartalaap",
        api_key="vk",
        voice="sahil",
        speed=0.95,
        profile="telephony",
        digits="mixed",
        temperature=0.4,
    )
    base.update(tts)
    return SimpleNamespace(tts=SimpleNamespace(**base))


@pytest.mark.parametrize("call_rate", [8000, 16000])
def test_factory_builds_the_service_at_vartalaaps_rate(call_rate):
    audio = SimpleNamespace(transport_out_sample_rate=call_rate, transport_in_sample_rate=call_rate)
    service = create_tts_service(_user_config(), audio)
    assert isinstance(service, VartalaapTTSService)
    # Vartalaap only produces 24 kHz; the output transport resamples to the call.
    assert service._init_sample_rate == VARTALAAP_SAMPLE_RATE
    assert service._settings.voice == "sahil"
    assert service._options == {"speed": 0.95, "profile": "telephony", "digits": "mixed", "temperature": 0.4}


def test_factory_fills_in_missing_values():
    audio = SimpleNamespace(transport_out_sample_rate=8000, transport_in_sample_rate=8000)
    service = create_tts_service(
        _user_config(voice=None, speed=None, profile=None, digits=None, temperature=None), audio
    )
    assert service._settings.voice == "saanvi"
    assert service._options == {"speed": 1.0, "profile": "default", "digits": "hi"}


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        VartalaapTTSService(api_key="  ")


def test_key_check_is_registered():
    assert ServiceProviders.VARTALAAP.value in UserConfigurationValidator()._validator_map


def test_key_check_accepts_a_good_key():
    with patch("api.services.configuration.check_validity.httpx.get") as get:
        get.return_value.status_code = 200
        assert UserConfigurationValidator()._check_vartalaap_api_key("vartalaap", "good") is True
    assert get.call_args.args[0] == "https://api.vartalaap.si/v1/voices"
    assert get.call_args.kwargs["headers"] == {"x-api-key": "good"}


@pytest.mark.parametrize("status", [401, 403])
def test_key_check_rejects_a_bad_key(status):
    with patch("api.services.configuration.check_validity.httpx.get") as get:
        get.return_value.status_code = status
        with pytest.raises(ValueError, match="Invalid Vartalaap API key"):
            UserConfigurationValidator()._check_vartalaap_api_key("vartalaap", "bad")


# ---------------------------------------------------------------------------
# Protocol, driven by hand
# ---------------------------------------------------------------------------


def _service():
    service = VartalaapTTSService(api_key="vk", voice="saanvi", speed=1.0, profile="telephony", digits="hi")
    service._websocket = SimpleNamespace(send=AsyncMock(), state=None)
    service.push_error = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    return service


def _queue(service, context_id):
    queue = asyncio.Queue()
    service._audio_contexts[context_id] = queue
    return queue


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_each_sentence_is_one_message():
    service = _service()
    message = service._build_message("Namaste ji", "ctx:1")
    assert message == {
        "text": "Namaste ji",
        "voice": "saanvi",
        "format": "pcm16",
        "request_id": "ctx:1",
        "speed": 1.0,
        "profile": "telephony",
        "digits": "hi",
    }


@pytest.mark.asyncio
async def test_audio_goes_to_the_sentence_being_spoken():
    service = _service()
    queue = _queue(service, "reply-1")
    service._requests["reply-1:1"] = ("reply-1", {}, 0)

    await service._handle_event({"type": "start", "request_id": "reply-1:1"})
    await service._handle_audio(b"\x01\x00" * 1920)
    await service._handle_event({"type": "end", "request_id": "reply-1:1"})

    items = _drain(queue)
    assert isinstance(items[0], TTSAudioRawFrame)
    assert items[0].sample_rate == VARTALAAP_SAMPLE_RATE and items[0].context_id == "reply-1"
    assert isinstance(items[1], TTSStoppedFrame)
    assert items[2] is None  # context closed
    assert service._requests == {}


@pytest.mark.asyncio
async def test_a_reply_ends_only_after_its_last_sentence():
    service = _service()
    queue = _queue(service, "reply-1")
    service._requests["reply-1:1"] = ("reply-1", {}, 0)
    service._requests["reply-1:2"] = ("reply-1", {}, 0)

    await service._handle_event({"type": "end", "request_id": "reply-1:1"})
    assert _drain(queue) == []

    await service._handle_event({"type": "end", "request_id": "reply-1:2"})
    items = _drain(queue)
    assert isinstance(items[0], TTSStoppedFrame) and items[1] is None


@pytest.mark.asyncio
async def test_events_may_name_their_kind_with_event():
    service = _service()
    queue = _queue(service, "reply-1")
    service._requests["reply-1:1"] = ("reply-1", {}, 0)
    await service._handle_event({"event": "end", "request_id": "reply-1:1"})
    assert isinstance(_drain(queue)[0], TTSStoppedFrame)


@pytest.mark.parametrize(
    "event,kind",
    [
        # Untagged, recognised by the fields the docs give each event.
        ({"request_id": "r", "voice": "saanvi", "format": "pcm16", "chunks": 2, "normalized": "x", "speed": 1, "postfx": True}, "start"),
        ({"request_id": "r", "chunk": 0, "ms": 140}, "first_audio_ms"),
        ({"request_id": "r", "chunk": 0, "text": "x", "tokens": 9}, None),  # chunk: nothing to do
        ({"request_id": "r", "chunk": 0, "frames": 3, "max_frames": 9, "audio_s": 0.2, "eos": True}, None),  # chunk_end
        ({"request_id": "r", "audio_s": 1.2, "frames": 15, "chunks": 2, "ttfa_ms": 130, "wall_ms": 900, "rtf": 0.7}, "end"),
        ({"request_id": "r", "error": "busy", "status": 503, "retry_after": 1}, "error"),
        # Tagged, under any of the usual names.
        ({"type": "end"}, "end"),
        ({"event": "start"}, "start"),
        ({"kind": "error"}, "error"),
    ],
)
def test_events_are_recognised_with_or_without_a_tag(event, kind):
    assert VartalaapTTSService._event_kind(event) == kind


@pytest.mark.asyncio
async def test_an_untagged_error_still_reaches_the_call():
    service = _service()
    queue = _queue(service, "reply-1")
    service._requests["reply-1:1"] = ("reply-1", {}, 0)
    await service._handle_event({"request_id": "reply-1:1", "error": "balance is zero", "status": 402})
    assert provider_http_status(None, service.push_error.await_args.kwargs["error_msg"]) == 402
    assert isinstance(_drain(queue)[0], TTSStoppedFrame)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403])
async def test_a_rejected_key_or_empty_balance_is_reported_with_its_status(status):
    service = _service()
    queue = _queue(service, "reply-1")
    service._requests["reply-1:1"] = ("reply-1", {}, 0)

    await service._handle_event(
        {"type": "error", "request_id": "reply-1:1", "status": status, "error": "nope"}
    )

    message = service.push_error.await_args.kwargs["error_msg"]
    # The call's provider-error handling (campaign pause) must recognise it.
    assert provider_http_status(None, message) == status
    assert isinstance(_drain(queue)[0], TTSStoppedFrame)


@pytest.mark.asyncio
async def test_busy_is_retried_once_then_reported():
    service = _service()
    sent = {"text": "x", "request_id": "reply-1:1"}
    service._requests["reply-1:1"] = ("reply-1", sent, 0)
    service.create_task = lambda coro, *a, **k: asyncio.ensure_future(coro)

    busy = {"type": "error", "request_id": "reply-1:1", "status": 503, "retry_after": 0.01}
    await service._handle_event(busy)
    await asyncio.sleep(0.05)
    service._websocket.send.assert_awaited_once_with(json.dumps(sent))
    service.push_error.assert_not_awaited()

    await service._handle_event(busy)
    service.push_error.assert_awaited_once()
    assert "status 503" in service.push_error.await_args.kwargs["error_msg"]


@pytest.mark.asyncio
async def test_a_busy_retry_is_dropped_after_an_interruption():
    service = _service()
    service._requests["reply-1:1"] = ("reply-1", {"text": "x"}, 0)
    task_holder = []
    service.create_task = lambda coro, *a, **k: task_holder.append(asyncio.ensure_future(coro))

    await service._handle_event({"type": "error", "request_id": "reply-1:1", "status": 503, "retry_after": 0.01})
    service._requests.clear()  # reconnect after an interruption
    await task_holder[0]
    service._websocket.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot_speaking,queued,reconnects",
    [(True, False, True), (False, True, True), (False, False, False)],
)
async def test_an_interruption_reconnects_when_anything_is_still_coming(bot_speaking, queued, reconnects):
    service = _service()
    service._bot_speaking = bot_speaking
    if queued:
        service._requests["reply-1:1"] = ("reply-1", {}, 0)
    service._disconnect = AsyncMock()
    service._connect = AsyncMock()

    with patch(
        "pipecat.services.tts_service.TTSService._handle_interruption", new=AsyncMock()
    ) as base:
        await service._handle_interruption(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    base.assert_awaited_once()
    assert service._disconnect.await_count == int(reconnects)
    assert service._connect.await_count == int(reconnects)


@pytest.mark.asyncio
async def test_closing_the_socket_forgets_queued_sentences():
    service = _service()
    service._websocket = SimpleNamespace(close=AsyncMock())
    service._requests["reply-1:1"] = ("reply-1", {}, 0)
    service._speaking_request = "reply-1:1"
    service.stop_all_metrics = AsyncMock()
    await service._disconnect_websocket()
    assert service._requests == {} and service._speaking_request is None


# ---------------------------------------------------------------------------
# End to end: a real pipeline against a fake Vartalaap server
# ---------------------------------------------------------------------------

CHUNK = b"\x10\x00" * 1920  # 80 ms of 24 kHz s16le


class FakeVartalaap:
    """Speaks Vartalaap's streaming protocol: one JSON message per sentence."""

    def __init__(self, *, busy_first: bool = False, reject_key: bool = False):
        self.received: list[dict] = []
        self.api_keys: list[str] = []
        self._busy_first = busy_first
        self._reject_key = reject_key

    async def handler(self, websocket):
        self.api_keys.append(websocket.request.headers.get("x-api-key"))
        async for raw in websocket:
            message = json.loads(raw)
            self.received.append(message)
            request_id = message["request_id"]
            if self._reject_key:
                await websocket.send(
                    json.dumps({"type": "error", "request_id": request_id, "status": 402, "error": "balance is zero"})
                )
                continue
            if self._busy_first:
                self._busy_first = False
                await websocket.send(
                    json.dumps({"type": "error", "request_id": request_id, "status": 503, "retry_after": 0.05})
                )
                continue
            await websocket.send(json.dumps({"type": "start", "request_id": request_id, "voice": message["voice"]}))
            await websocket.send(json.dumps({"type": "first_audio_ms", "request_id": request_id, "ms": 120}))
            for _ in range(3):
                await websocket.send(CHUNK)
            await websocket.send(json.dumps({"type": "end", "request_id": request_id, "audio_s": 0.24}))


async def _speak(fake, text="Namaste ji, main Aastha bol rahi hoon.", sleep=0.6):
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        service = VartalaapTTSService(
            api_key="test-key", voice="saanvi", profile="telephony", url=f"ws://127.0.0.1:{port}"
        )
        # The service pauses its input while speaking and resumes on
        # BotStoppedSpeakingFrame, which the output transport sends in a call.
        return await run_test(
            service,
            frames_to_send=[TTSSpeakFrame(text), SleepFrame(sleep=sleep), BotStoppedSpeakingFrame()],
        )


@pytest.mark.asyncio
async def test_end_to_end_a_sentence_is_spoken():
    fake = FakeVartalaap()
    down, _ = await _speak(fake)

    assert fake.api_keys == ["test-key"]
    assert fake.received[0]["text"].strip() == "Namaste ji, main Aastha bol rahi hoon."
    assert fake.received[0]["format"] == "pcm16" and fake.received[0]["profile"] == "telephony"

    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert b"".join(f.audio for f in audio) == CHUNK * 3
    assert all(f.sample_rate == VARTALAAP_SAMPLE_RATE for f in audio)
    kinds = [type(f) for f in down]
    assert TTSStartedFrame in kinds and TTSStoppedFrame in kinds
    assert kinds.index(TTSStartedFrame) < kinds.index(TTSAudioRawFrame) < kinds.index(TTSStoppedFrame)


@pytest.mark.asyncio
async def test_end_to_end_busy_server_is_retried():
    fake = FakeVartalaap(busy_first=True)
    down, up = await _speak(fake, sleep=0.8)

    assert len(fake.received) == 2 and fake.received[0] == fake.received[1]
    assert b"".join(f.audio for f in down if isinstance(f, TTSAudioRawFrame)) == CHUNK * 3
    assert not [f for f in up if isinstance(f, ErrorFrame)]


@pytest.mark.asyncio
async def test_end_to_end_empty_balance_surfaces_as_an_error():
    fake = FakeVartalaap(reject_key=True)
    down, up = await _speak(fake)

    errors = [f for f in up if isinstance(f, ErrorFrame)]
    assert errors and provider_http_status(None, errors[0].error) == 402
    assert not [f for f in down if isinstance(f, TTSAudioRawFrame)]
