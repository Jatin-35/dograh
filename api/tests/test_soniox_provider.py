"""Tests for the Soniox real-time STT provider.

Covers the settings (and the schema the settings form renders), the session
context built from an agent's dictionary and description, the service a call
builds, the exact configuration message Soniox receives, transcripts coming
back, rejected accounts, and the key check on save.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pipecat.frames.frames import ErrorFrame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.services.soniox.stt import SonioxSTTService
from pipecat.transcriptions.language import Language
from pydantic import TypeAdapter, ValidationError
from websockets.protocol import State

from api.services.campaign.circuit_breaker import (
    circuit_breaker,
    register_campaign_provider_error_handler,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    SONIOX_STT_LANGUAGES,
    ServiceProviders,
    SonioxSTTConfiguration,
    STTConfig,
)
from api.services.pipecat.provider_rejection import provider_http_status
from api.services.pipecat.service_factory import (
    create_stt_service,
    stt_uses_external_turns,
)
from api.services.pipecat.soniox_stt import (
    DograhSonioxSTTService,
    SonioxRejectedError,
    build_soniox_context,
)

# ── Settings ──────────────────────────────────────────────────────────


def test_soniox_is_an_stt_provider_with_hinglish_defaults():
    cfg = TypeAdapter(STTConfig).validate_python({"provider": "soniox", "api_key": "k"})
    assert isinstance(cfg, SonioxSTTConfiguration)
    assert cfg.provider == ServiceProviders.SONIOX
    assert cfg.model == "stt-rt-v5"
    assert cfg.languages == ["hi", "en"]
    assert cfg.strict_languages is False
    assert cfg.domain_context is None
    assert cfg.use_soniox_turn_detection is False
    assert (cfg.max_endpoint_delay_ms, cfg.endpoint_sensitivity, cfg.endpoint_speed_level) == (
        None,
        None,
        None,
    )


def test_the_schema_tells_the_form_how_to_draw_each_field():
    props = SonioxSTTConfiguration.model_json_schema()["properties"]
    # Languages: a multi-select with Indian languages first and a visible default.
    assert props["languages"]["type"] == "array"
    assert props["languages"]["multi_select"] is True
    assert props["languages"]["default"] == ["hi", "en"]
    assert props["languages"]["examples"][:4] == ["en", "hi", "mr", "gu"]
    # Strict languages and the description sit in the main section.
    assert props["strict_languages"]["type"] == "boolean"
    assert "advanced" not in props["strict_languages"]
    assert props["domain_context"]["multiline"] is True
    # Turn detection is advanced; its three knobs only show when it's on.
    assert props["use_soniox_turn_detection"]["advanced"] is True
    for knob in ("max_endpoint_delay_ms", "endpoint_sensitivity", "endpoint_speed_level"):
        assert props[knob]["advanced"] is True
        assert props[knob]["visible_when"] == {"use_soniox_turn_detection": True}
        assert props[knob]["label"]


def test_all_listed_languages_are_ones_pipecat_can_send():
    assert len(SONIOX_STT_LANGUAGES) == 60
    for code in SONIOX_STT_LANGUAGES:
        Language(code)  # raises if pipecat can't map it


@pytest.mark.parametrize(
    "given,stored",
    [
        (["hi", "en"], ["hi", "en"]),
        (["HI", " en ", "hi"], ["hi", "en"]),  # case, spaces, duplicates
        ("hi, en", ["hi", "en"]),  # a comma string from an API or SDK writer
        ([], []),
        (None, []),
        (["mr", "hi", "en"], ["mr", "hi", "en"]),
    ],
)
def test_languages_are_cleaned_up(given, stored):
    assert SonioxSTTConfiguration(api_key="k", languages=given).languages == stored


@pytest.mark.parametrize(
    "bad",
    [
        {"languages": ["hindi"]},
        {"languages": ["xx"]},
        {"strict_languages": True, "languages": []},
        {"max_endpoint_delay_ms": 100},
        {"max_endpoint_delay_ms": 5000},
        {"endpoint_sensitivity": 2},
        {"endpoint_speed_level": 4},
        {"domain_context": "x" * 8001},
    ],
)
def test_bad_values_are_refused(bad):
    with pytest.raises(ValidationError):
        SonioxSTTConfiguration(api_key="k", **bad)


def test_a_blank_description_is_stored_as_none():
    assert SonioxSTTConfiguration(api_key="k", domain_context="   ").domain_context is None
    assert SonioxSTTConfiguration(api_key="k", domain_context=" PNG support ").domain_context == "PNG support"


def test_strict_with_languages_is_allowed():
    cfg = SonioxSTTConfiguration(api_key="k", strict_languages=True, languages=["hi", "en"])
    assert cfg.strict_languages is True


# ── Session context ───────────────────────────────────────────────────


def test_no_dictionary_and_no_description_sends_no_context():
    assert build_soniox_context(None, None) is None
    assert build_soniox_context([], "  ") is None
    assert build_soniox_context([" ", ""], None) is None


def test_dictionary_words_become_terms_cleaned_and_deduplicated():
    ctx = build_soniox_context([" Think Gas", "BP number", "Think Gas", ""], None)
    assert ctx.terms == ["Think Gas", "BP number"]
    assert ctx.general is None and ctx.text is None


def test_a_short_description_goes_in_general():
    ctx = build_soniox_context(["Vectus Cool"], "Vectus water tank enquiries")
    assert [(g.key, g.value) for g in ctx.general] == [("domain", "Vectus water tank enquiries")]
    assert ctx.text is None
    assert ctx.terms == ["Vectus Cool"]


def test_a_long_description_goes_in_text():
    long = "Think Gas PNG support. " * 40  # ~920 characters
    ctx = build_soniox_context(None, long)
    assert ctx.general is None
    assert ctx.text == long.strip()


def test_the_context_stays_under_sonioxs_size_limit():
    terms = [f"term-{i:04d}" for i in range(2000)]  # ~20,000 characters of terms
    ctx = build_soniox_context(terms, "y" * 8000)
    size = sum(len(t) + 1 for t in ctx.terms or []) + len(ctx.text or "") + sum(
        len(g.value) for g in ctx.general or []
    )
    assert size <= 9000
    assert ctx.terms[0] == "term-0000"  # terms come first; they matter most


# ── The service a call builds ─────────────────────────────────────────


def _service(stt=None, *, keyterms=None, rate=8000, ref="run-42", **overrides):
    cfg = stt or SonioxSTTConfiguration(api_key="sk-soniox", **overrides)
    return create_stt_service(
        SimpleNamespace(stt=cfg),
        SimpleNamespace(transport_in_sample_rate=rate),
        keyterms=keyterms,
        call_reference=ref,
    )


def test_default_service_for_a_hinglish_agent():
    svc = _service(keyterms=["Think Gas", "BP number"], domain_context="Think Gas PNG support")
    assert isinstance(svc, DograhSonioxSTTService)
    s = svc._settings
    assert s.model == "stt-rt-v5"
    assert s.language_hints == [Language.HI, Language.EN]
    assert s.language_hints_strict is False
    assert s.context.terms == ["Think Gas", "BP number"]
    assert s.context.general[0].value == "Think Gas PNG support"
    assert s.client_reference_id == "run-42"
    # Our own turn detection: local VAD ends the turn and finalizes Soniox.
    assert svc._vad_force_turn_endpoint is True
    assert svc._should_interrupt is False
    assert s.max_endpoint_delay_ms is None and s.endpoint_sensitivity is None


def test_strict_languages_reach_the_service():
    s = _service(strict_languages=True, languages=["hi", "en", "mr"])._settings
    assert s.language_hints == [Language.HI, Language.EN, Language.MR]
    assert s.language_hints_strict is True


def test_no_languages_means_auto_detect_and_never_strict():
    s = _service(languages=[])._settings
    assert s.language_hints is None
    assert s.language_hints_strict is False


def test_soniox_turn_detection_hands_turns_to_soniox_with_its_tuning():
    svc = _service(
        use_soniox_turn_detection=True,
        max_endpoint_delay_ms=1200,
        endpoint_sensitivity=0.3,
        endpoint_speed_level=2,
    )
    assert svc._vad_force_turn_endpoint is False
    s = svc._settings
    assert (s.max_endpoint_delay_ms, s.endpoint_sensitivity, s.endpoint_latency_adjustment_level) == (
        1200,
        0.3,
        2,
    )
    cfg = SonioxSTTConfiguration(api_key="k", use_soniox_turn_detection=True)
    assert stt_uses_external_turns(SimpleNamespace(stt=cfg)) is True


def test_turn_tuning_is_ignored_while_soniox_turn_detection_is_off():
    s = _service(max_endpoint_delay_ms=1200, endpoint_sensitivity=0.3, endpoint_speed_level=2)._settings
    assert s.max_endpoint_delay_ms is None
    assert s.endpoint_sensitivity is None
    assert s.endpoint_latency_adjustment_level is None
    assert stt_uses_external_turns(SimpleNamespace(stt=SonioxSTTConfiguration(api_key="k"))) is False


# ── What Soniox receives ──────────────────────────────────────────────


class FakeSoniox:
    """An in-process stand-in for Soniox's websocket."""

    def __init__(self, replies=()):
        self.sent: list = []
        self.state = State.OPEN
        self._replies = asyncio.Queue()
        for reply in replies:
            self._replies.put_nowait(reply)

    def reply(self, message):
        self._replies.put_nowait(message)

    def finish(self):
        self._replies.put_nowait(None)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.state = State.CLOSED

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._replies.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)

    @property
    def config(self) -> dict:
        return json.loads(self.sent[0])


async def _connected(svc, fake):
    # What start() does: the call's input rate, unless the service pinned one.
    svc._sample_rate = svc._init_sample_rate or 16000
    connect = AsyncMock(return_value=fake)
    with patch("pipecat.services.soniox.stt.websocket_connect", connect):
        await svc._connect_websocket()
    fake.connect_kwargs = connect.await_args.kwargs
    return fake.config


@pytest.mark.asyncio
async def test_the_configuration_message_soniox_receives():
    svc = _service(
        keyterms=["Think Gas", "BP number"],
        domain_context="Think Gas PNG customer support",
        rate=8000,
    )
    fake = FakeSoniox()
    config = await _connected(svc, fake)
    # The key travels in the Authorization header, never in the start request:
    # Soniox refuses start-request keys from 15 January 2027.
    assert fake.connect_kwargs["additional_headers"] == {"Authorization": "Bearer sk-soniox"}
    assert "api_key" not in config
    assert config["model"] == "stt-rt-v5"
    assert config["audio_format"] == "pcm_s16le"
    assert config["num_channels"] == 1
    assert config["sample_rate"] == 8000  # phone audio, as it arrives
    assert sorted(config["language_hints"]) == ["en", "hi"]  # Soniox hints are a set
    assert config["language_hints_strict"] is False
    assert config["context"]["terms"] == ["Think Gas", "BP number"]
    assert config["context"]["general"] == [{"key": "domain", "value": "Think Gas PNG customer support"}]
    assert config["client_reference_id"] == "run-42"
    assert config["enable_endpoint_detection"] is False
    assert config["enable_speaker_diarization"] is False
    assert config["enable_language_identification"] is False


@pytest.mark.asyncio
async def test_soniox_turn_detection_turns_on_its_endpoint_detection():
    svc = _service(use_soniox_turn_detection=True, max_endpoint_delay_ms=1500, endpoint_speed_level=1)
    config = await _connected(svc, FakeSoniox())
    assert config["enable_endpoint_detection"] is True
    assert config["max_endpoint_delay_ms"] == 1500
    assert config["endpoint_latency_adjustment_level"] == 1
    assert config["endpoint_sensitivity"] is None


@pytest.mark.asyncio
async def test_no_dictionary_and_no_languages_send_nulls_not_empty_values():
    config = await _connected(_service(languages=[]), FakeSoniox())
    assert config["language_hints"] is None
    assert config["context"] is None


# ── Transcripts coming back ───────────────────────────────────────────


def _tokens(*pairs, language="hi"):
    return {"tokens": [{"text": text, "is_final": final, "language": language} for text, final in pairs]}


@pytest.mark.asyncio
async def test_a_hinglish_turn_comes_back_as_one_finalized_transcript():
    svc = _service()
    fake = FakeSoniox(
        [
            _tokens(("Mera", False)),
            _tokens(("Mera", True), (" BP number", True), (" 7000", False)),
            _tokens((" 7000 2382 hai", True), ("<end>", True)),
        ]
    )
    fake.finish()
    svc._websocket = fake
    pushed = []

    async def capture(frame, *args, **kwargs):
        pushed.append(frame)

    with (
        patch.object(svc, "push_frame", side_effect=capture),
        patch.object(svc, "start_processing_metrics", AsyncMock()),
        patch.object(svc, "stop_processing_metrics", AsyncMock()),
    ):
        await svc._receive_messages()

    finals = [f for f in pushed if isinstance(f, TranscriptionFrame)]
    assert [f.text for f in finals] == ["Mera BP number 7000 2382 hai"]
    assert finals[0].finalized is True
    assert finals[0].language == Language.HI
    interims = [f.text for f in pushed if isinstance(f, InterimTranscriptionFrame)]
    assert interims[0] == "Mera" and interims[1] == "Mera BP number 7000"


@pytest.mark.asyncio
async def test_the_local_end_of_speech_asks_soniox_to_finalize():
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    svc = _service()
    fake = FakeSoniox()
    svc._websocket = fake
    with patch("pipecat.services.stt_service.STTService.process_frame", AsyncMock()):
        await svc.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert fake.sent == ['{"type": "finalize"}']


# ── Rejected accounts ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,status,stops",
    [
        ("Error: 401 (_receive_messages) - Invalid API key.", 401, True),
        ("Error: 402 (_receive_messages) - Organization balance exhausted.", 402, True),
        ("Error: 403 (_receive_messages) - Forbidden.", 403, True),
        ("Error: 503 (_receive_messages) - Service unavailable.", 503, False),
        ("Error: 408 (_receive_messages) - Request timeout.", 408, False),
    ],
)
@pytest.mark.asyncio
async def test_in_stream_errors_carry_their_status(message, status, stops):
    svc = _service()
    with patch.object(SonioxSTTService, "push_error", AsyncMock()) as base_push:
        await svc.push_error(message)
    exception = base_push.await_args.args[1]
    assert isinstance(exception, SonioxRejectedError)
    assert exception.status_code == status
    assert provider_http_status(exception, message) == status
    assert (svc._rejected_status is not None) is stops


@pytest.mark.asyncio
async def test_a_refused_account_stops_reconnecting_but_an_outage_still_retries():
    refused = _service()
    with patch.object(SonioxSTTService, "push_error", AsyncMock()):
        await refused.push_error("Error: 402 (_receive_messages) - Organization balance exhausted.")
    with patch(
        "pipecat.services.websocket_service.WebsocketService._maybe_try_reconnect",
        AsyncMock(return_value=True),
    ) as base:
        assert await refused._maybe_try_reconnect("closed", AsyncMock()) is False
        base.assert_not_awaited()

        outage = _service()
        assert await outage._maybe_try_reconnect("closed", AsyncMock()) is True
        base.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_errors_pass_through_untouched():
    svc = _service()
    boom = RuntimeError("socket died")
    with patch.object(SonioxSTTService, "push_error", AsyncMock()) as base_push:
        await svc.push_error("Unable to connect to Soniox: socket died", boom)
        await svc.push_error("Error closing websocket")
    assert base_push.await_args_list[0].args[1] is boom
    assert base_push.await_args_list[1].args[1] is None
    assert svc._rejected_status is None


@pytest.mark.asyncio
async def test_an_out_of_credit_soniox_pauses_the_campaign():
    """The whole path: Soniox's in-stream 402 reaches the campaign hook."""
    svc = _service()
    register_campaign_provider_error_handler(
        svc, provider="soniox", source="stt", workflow_run_id=11, organization_id=5
    )
    fake = FakeSoniox(
        [{"tokens": [], "error_code": 402, "error_message": "Organization balance exhausted."}]
    )
    fake.finish()
    svc._websocket = fake
    frames = []

    async def push_error_frame(*args, **kwargs):
        frame = args[0] if args else kwargs["error"]
        frames.append(frame)
        await svc._call_event_handler("on_error", frame)

    with (
        patch.object(svc, "push_error_frame", side_effect=push_error_frame),
        patch.object(svc, "push_frame", AsyncMock()),
        patch.object(circuit_breaker, "pause_on_provider_error", AsyncMock()) as pause,
    ):
        await svc._receive_messages()
        await asyncio.sleep(0)  # event handlers may run as tasks

    assert frames and isinstance(frames[0], ErrorFrame)
    pause.assert_awaited_once()
    kwargs = pause.await_args.kwargs
    assert (kwargs["service"], kwargs["provider"], kwargs["workflow_run_id"], kwargs["organization_id"]) == (
        "stt",
        "soniox",
        11,
        5,
    )
    assert provider_http_status(kwargs["exception"], kwargs["message"]) == 402


# ── Key check on save ─────────────────────────────────────────────────


def _check(status=None, error=None):
    def fake_get(url, headers, timeout):
        assert url == "https://api.soniox.com/v1/models"
        assert headers == {"Authorization": "Bearer sk-soniox"}
        if error:
            raise error
        return MagicMock(status_code=status)

    with patch("api.services.configuration.check_validity.httpx.get", side_effect=fake_get):
        return UserConfigurationValidator()._check_api_key("soniox", "sk-soniox")


def test_a_good_key_passes_the_save_check():
    assert _check(200) is True


@pytest.mark.parametrize("status", [401, 403])
def test_a_bad_key_is_refused_on_save(status):
    with pytest.raises(ValueError, match="Invalid Soniox API key"):
        _check(status)


def test_an_unreachable_soniox_says_so():
    with pytest.raises(ValueError, match="Could not connect to the Soniox API"):
        _check(error=httpx.ConnectError("down"))


def test_a_soniox_server_error_does_not_block_saving():
    assert _check(500) is True


# ── A failed first connection is retried, not left dead ───────────────


@pytest.mark.asyncio
async def test_a_failed_first_connect_still_starts_the_receive_loop():
    svc = _service()
    svc._sample_rate = 8000
    with (
        patch("pipecat.services.soniox.stt.websocket_connect", AsyncMock(side_effect=OSError("network blip"))),
        patch.object(svc, "push_error", AsyncMock()),
        patch.object(svc, "create_task", MagicMock(return_value="receive-task")) as create_task,
        patch.object(svc, "_create_keepalive_task", MagicMock()),
    ):
        await svc._connect()
    assert svc._websocket is None
    # Pipecat's own version skips this when the connect failed, leaving the
    # call with no transcription; the loop's reconnect needs it running.
    assert svc._receive_task == "receive-task"
    create_task.assert_called_once()
    create_task.call_args.args[0].close()  # the un-awaited coroutine


@pytest.mark.asyncio
async def test_the_retry_after_a_failed_connect_reaches_soniox():
    svc = _service()
    svc._sample_rate = 8000
    fake = FakeSoniox()
    fake.ping = AsyncMock()
    connect = AsyncMock(side_effect=[OSError("network blip"), fake])
    with (
        patch("pipecat.services.soniox.stt.websocket_connect", connect),
        patch.object(svc, "push_error", AsyncMock()),
        # Event handlers and the keepalive task need a running pipeline.
        patch.object(svc, "_call_event_handler", AsyncMock()),
        patch.object(svc, "_create_keepalive_task", MagicMock()),
        patch.object(svc, "_cancel_keepalive_task", AsyncMock()),
        patch("pipecat.services.websocket_service.asyncio.sleep", AsyncMock()),
    ):
        await svc._connect_websocket()  # the call's first connect fails
        assert svc._websocket is None
        assert await svc._try_reconnect(max_retries=3) is True
    assert svc._websocket is fake
    assert fake.config["client_reference_id"] == "run-42"
    assert connect.await_count == 2


@pytest.mark.asyncio
async def test_v5_only_turn_settings_are_not_sent_to_an_older_model():
    svc = _service(
        model="stt-rt-v4",
        use_soniox_turn_detection=True,
        max_endpoint_delay_ms=1500,
        endpoint_sensitivity=0.5,
        endpoint_speed_level=2,
    )
    config = await _connected(svc, FakeSoniox())
    assert config["model"] == "stt-rt-v4"
    assert config["max_endpoint_delay_ms"] == 1500  # every model takes this one
    # Soniox refuses these on pre-v5 models ("Model does not support ...").
    assert config["endpoint_sensitivity"] is None
    assert config["endpoint_latency_adjustment_level"] is None


@pytest.mark.asyncio
async def test_the_start_request_holds_only_fields_soniox_documents():
    documented = {
        "model", "audio_format", "num_channels", "sample_rate", "language_hints",
        "language_hints_strict", "context", "enable_speaker_diarization",
        "enable_language_identification", "enable_endpoint_detection",
        "max_endpoint_delay_ms", "endpoint_sensitivity",
        "endpoint_latency_adjustment_level", "client_reference_id", "translation",
    }
    config = await _connected(_service(keyterms=["x"], domain_context="y"), FakeSoniox())
    assert set(config) <= documented, set(config) - documented
    # Context sections are the four Soniox defines, each in its documented shape.
    assert set(k for k, v in config["context"].items() if v is not None) <= {
        "general", "text", "terms", "translation_terms"
    }
    assert all(set(item) == {"key", "value"} for item in config["context"]["general"])
    assert len(config["client_reference_id"]) <= 256
