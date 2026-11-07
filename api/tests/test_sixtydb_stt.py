"""60db speech-to-text: settings, factory, key check and the WebSocket protocol."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from websockets.asyncio.server import serve

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    REGISTRY,
    ServiceProviders,
    ServiceType,
    SixtyDBSTTConfiguration,
)
from api.services.pipecat.service_factory import create_stt_service
from api.services.pipecat.sixtydb_stt import SixtyDBSTTService, clean_keywords

# ---------------------------------------------------------------------------
# Settings, factory, key check
# ---------------------------------------------------------------------------


def test_defaults():
    config = SixtyDBSTTConfiguration(api_key="k")
    assert config.provider == ServiceProviders.SIXTYDB
    assert config.model == "60db-stt-v01"
    assert config.languages == ["hi", "en"]
    assert config.audio_enhancement is True
    assert config.keep_short_answers is True


def test_registered_as_a_transcriber():
    assert REGISTRY[ServiceType.STT][ServiceProviders.SIXTYDB] is SixtyDBSTTConfiguration


def test_languages_are_normalised():
    config = SixtyDBSTTConfiguration(api_key="k", languages=" HI, en ,hi")
    assert config.languages == ["hi", "en"]


@pytest.mark.parametrize(
    "languages",
    [["hi", "ur"], ["hi", "en", "mr", "gu", "bn", "ta"]],
    ids=["unsupported", "more-than-five"],
)
def test_bad_languages_are_refused(languages):
    with pytest.raises(ValueError):
        SixtyDBSTTConfiguration(api_key="k", languages=languages)


def test_the_form_offers_the_languages():
    props = SixtyDBSTTConfiguration.model_json_schema()["properties"]
    assert props["languages"]["multi_select"] is True
    assert props["languages"]["default"] == ["hi", "en"]
    assert props["keep_short_answers"]["advanced"] is True


def _user_config(**stt):
    base = dict(
        provider=ServiceProviders.SIXTYDB.value,
        model="60db-stt-v01",
        api_key="sk_live_test",
        languages=["hi", "en"],
        audio_enhancement=True,
        keep_short_answers=True,
    )
    base.update(stt)
    return SimpleNamespace(stt=SimpleNamespace(**base))


def test_factory_builds_the_service_with_the_agents_words():
    audio = SimpleNamespace(transport_in_sample_rate=8000, transport_out_sample_rate=8000)
    service = create_stt_service(
        _user_config(audio_enhancement=False), audio, keyterms=["Vectus", "TenX", "Vectus"]
    )
    assert isinstance(service, SixtyDBSTTService)
    assert service._init_sample_rate == 8000
    assert service._languages == ["hi", "en"]
    assert service._keywords == ["Vectus", "TenX"]
    assert service._audio_enhancement is False
    assert service._keep_short_answers is True


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        SixtyDBSTTService(api_key="  ")


def test_keywords_are_trimmed_unique_and_capped():
    assert clean_keywords([" Puff ", "", "Puff", "Safe"]) == ["Puff", "Safe"]
    assert len(clean_keywords([f"w{i}" for i in range(150)])) == 100


def test_key_check_is_registered():
    assert ServiceProviders.SIXTYDB.value in UserConfigurationValidator()._validator_map


def test_key_check_accepts_a_good_key():
    with patch("api.services.configuration.check_validity.httpx.get") as get:
        get.return_value.status_code = 200
        assert UserConfigurationValidator()._check_sixtydb_api_key("60db-stt-v01", "good") is True
    assert get.call_args.args[0] == "https://api.60db.ai/stt/models"
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer good"}


@pytest.mark.parametrize("status", [401, 403])
def test_key_check_rejects_a_bad_key(status):
    with patch("api.services.configuration.check_validity.httpx.get") as get:
        get.return_value.status_code = status
        with pytest.raises(ValueError, match="Invalid 60db API key"):
            UserConfigurationValidator()._check_sixtydb_api_key("60db-stt-v01", "bad")


# ---------------------------------------------------------------------------
# The start message
# ---------------------------------------------------------------------------


def test_start_message_asks_for_phone_audio_and_our_turns():
    service = SixtyDBSTTService(api_key="k", keywords=["Vectus"])
    message = service._start_message()
    assert message["type"] == "start"
    assert message["languages"] == ["hi", "en"]
    assert message["keywords"] == ["Vectus"]
    config = message["config"]
    assert (config["encoding"], config["sample_rate"]) == ("mulaw", 8000)
    # Our VAD ends turns; 60db's own endpointing must not split sentences.
    assert config["turn_detector"] is False and config["early_final"] is False
    assert config["utterance_end_ms"] >= 1000  # 60db refuses less
    assert config["audio_enhancement"] == "on"
    assert config["interim_min_voiced_ms"] == 200


def test_start_message_without_extras():
    service = SixtyDBSTTService(api_key="k", languages=[], audio_enhancement=False, keep_short_answers=False)
    message = service._start_message()
    assert message["languages"] is None  # auto-detect
    assert "keywords" not in message
    assert message["config"]["audio_enhancement"] == "off"
    assert "interim_min_voiced_ms" not in message["config"]


def test_the_key_is_redacted():
    service = SixtyDBSTTService(api_key="sk_live/secret+key")
    text = "failed for wss://x?apiKey=sk_live%2Fsecret%2Bkey and sk_live/secret+key"
    assert "secret" not in service._redact(text)


# ---------------------------------------------------------------------------
# Results, driven by hand
# ---------------------------------------------------------------------------


def _service(**kwargs):
    service = SixtyDBSTTService(api_key="k", **kwargs)
    service.push_frame = AsyncMock()
    service.push_error = AsyncMock()
    return service


def _pushed(service, kind):
    return [c.args[0] for c in service.push_frame.call_args_list if isinstance(c.args[0], kind)]


def _final(text, utterance=1, **extra):
    return {
        "type": "transcription",
        "text": text,
        "is_final": True,
        "speech_final": True,
        "utterance_id": utterance,
        "final_id": f"s:{utterance}",
        "language": "hi",
        **extra,
    }


def _interim(text, utterance=1):
    return {"type": "transcription", "text": text, "is_final": False, "utterance_id": utterance}


@pytest.mark.asyncio
async def test_interim_and_final_become_frames():
    service = _service()
    await service._handle_event(_interim("mujhe tank"))
    await service._handle_event(_final("mujhe tank chahiye"))
    (interim,) = _pushed(service, InterimTranscriptionFrame)
    (final,) = _pushed(service, TranscriptionFrame)
    assert interim.text == "mujhe tank"
    assert final.text == "mujhe tank chahiye"
    assert final.language is not None and final.language.value == "hi"


@pytest.mark.asyncio
async def test_a_duplicate_final_is_dropped():
    service = _service()
    await service._handle_event(_final("haan ji"))
    await service._handle_event(_final("haan ji"))
    assert len(_pushed(service, TranscriptionFrame)) == 1


@pytest.mark.asyncio
async def test_a_withheld_short_answer_is_kept_from_its_interim():
    service = _service()
    await service._handle_event(_interim("haan"))
    await service._handle_event(_final("", processing_mode="low_confidence_transcript"))
    (final,) = _pushed(service, TranscriptionFrame)
    assert final.text == "haan"


@pytest.mark.asyncio
async def test_a_withheld_answer_with_nothing_heard_is_dropped():
    service = _service()
    await service._handle_event(_final("", processing_mode="low_confidence_transcript"))
    assert _pushed(service, TranscriptionFrame) == []


@pytest.mark.asyncio
async def test_withheld_answers_stay_dropped_when_the_setting_is_off():
    service = _service(keep_short_answers=False)
    await service._handle_event(_interim("haan"))
    await service._handle_event(_final("", processing_mode="low_confidence_transcript"))
    assert _pushed(service, TranscriptionFrame) == []


@pytest.mark.asyncio
async def test_an_interim_does_not_leak_into_a_later_turn():
    service = _service()
    await service._handle_event(_interim("haan", utterance=1))
    await service._handle_event({"type": "no_speech", "terminal": True, "utterance_id": 1})
    await service._handle_event(_final("", utterance=1, processing_mode="low_confidence_transcript"))
    assert _pushed(service, TranscriptionFrame) == []


@pytest.mark.asyncio
async def test_a_failed_turn_is_logged_not_reported():
    service = _service()
    await service._handle_event(
        {"type": "error", "terminal": True, "failure_type": "transcription_timeout", "error": "timed out"}
    )
    service.push_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_message_is_reported():
    service = _service()
    await service._handle_event({"type": "error", "error": "Unsupported language code(s): xx"})
    service.push_error.assert_awaited_once()
    assert "Unsupported language" in service.push_error.await_args.kwargs["error_msg"]


@pytest.mark.asyncio
async def test_running_out_of_credit_stops_reconnecting():
    service = _service()
    await service._handle_event({"type": "error", "error": "Insufficient credits"})
    assert service._rejected_status == 402
    assert await service._maybe_try_reconnect("closed", AsyncMock()) is False


# ---------------------------------------------------------------------------
# End to end against a fake 60db server
# ---------------------------------------------------------------------------


class Fake60db:
    """Speaks 60db's STT handshake, then answers a finalize with the turn's final."""

    def __init__(self, *, reject_key: bool = False):
        self.reject_key = reject_key
        self.path = None
        self.start = None
        self.audio: list[bytes] = []
        self.finalizes = 0
        self.stopped = False

    async def handler(self, websocket):
        self.path = websocket.request.path
        await websocket.send(json.dumps({"type": "connecting", "message": "Authenticating..."}))
        if self.reject_key:
            await websocket.send(json.dumps({"type": "error", "error": "Invalid API key"}))
            await websocket.close(1008, "Invalid API key")
            return
        await websocket.send(json.dumps({"type": "connected", "session_id": "proxy"}))
        await websocket.send(json.dumps({"type": "connection_established", "service": "stt"}))
        async for raw in websocket:
            if isinstance(raw, bytes):
                self.audio.append(raw)
                continue
            message = json.loads(raw)
            if message["type"] == "start":
                self.start = message
                await websocket.send(
                    json.dumps({"type": "session_started", "model": "60db-stt-v01", "languages": ["hi", "en"]})
                )
            elif message["type"] == "finalize":
                self.finalizes += 1
                await websocket.send(json.dumps(_interim("haan ji")))
                await websocket.send(
                    json.dumps(_final("haan ji, mujhe tank chahiye", endpoint_reason="client_finalize"))
                )
            elif message["type"] == "stop":
                self.stopped = True
                await websocket.send(json.dumps({"type": "session_stopped", "billing_summary": {}}))


def _audio_frames(count=10):
    # 20 ms of 8 kHz 16-bit silence each, as a phone call delivers it.
    return [InputAudioRawFrame(audio=b"\x00\x00" * 160, sample_rate=8000, num_channels=1) for _ in range(count)]


@pytest.mark.asyncio
async def test_end_to_end_a_turn_is_transcribed():
    fake = Fake60db()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        service = SixtyDBSTTService(
            api_key="sk_live_test", keywords=["Vectus"], url=f"ws://127.0.0.1:{port}", sample_rate=8000
        )
        down, _ = await run_test(
            service,
            frames_to_send=[
                SleepFrame(sleep=0.3),
                VADUserStartedSpeakingFrame(),
                *_audio_frames(10),
                VADUserStoppedSpeakingFrame(),
                SleepFrame(sleep=0.5),
            ],
        )

    assert fake.path.endswith("apiKey=sk_live_test")
    assert fake.start["keywords"] == ["Vectus"]
    # 200 ms of audio: three full 60 ms chunks, the rest sent with the finalize.
    assert [len(chunk) for chunk in fake.audio] == [480, 480, 480, 160]
    assert fake.finalizes == 1
    assert fake.stopped
    finals = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert [f.text for f in finals] == ["haan ji, mujhe tank chahiye"]
    assert finals[0].finalized is True


@pytest.mark.asyncio
async def test_a_rejected_key_is_reported_once_without_the_key():
    fake = Fake60db(reject_key=True)
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        service = SixtyDBSTTService(api_key="sk_live_secret", url=f"ws://127.0.0.1:{port}", sample_rate=8000)
        service.push_error = AsyncMock()
        await service._connect_websocket()

    assert service._websocket is None
    assert service._rejected_status == 401
    message = service.push_error.await_args.kwargs["error_msg"]
    assert "Invalid API key" in message
    assert "sk_live_secret" not in message
    assert await service._maybe_try_reconnect("closed", AsyncMock()) is False
