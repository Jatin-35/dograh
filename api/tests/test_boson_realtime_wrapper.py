"""Boson Higgs Realtime: Dograh wrapper, wire format, factory and key check.

Higgs Realtime speaks OpenAI Realtime (GA) with a few differences: it waits
for the first session.update, sends events the pinned parser doesn't know,
keeps the session open after most errors, and reports refusals as close
codes. These tests pin what Dograh sends and how it reads Boson's replies,
using Boson's documented event shapes. No network.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.services.openai.realtime import events
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.registry import (
    REALTIME_PROVIDERS,
    BosonRealtimeLLMConfiguration,
    ServiceProviders,
)
from api.services.pipecat.provider_rejection import provider_http_status
from api.services.pipecat.realtime.boson_realtime import (
    BOSON_REALTIME_URL,
    DograhBosonRealtimeLLMService,
    _BosonEventFilter,
)
from api.services.pipecat.service_factory import create_realtime_llm_service


class _FakeSocket:
    def __init__(self, incoming=None, close_with=None):
        self.sent: list[dict] = []
        self._incoming = list(incoming or [])
        self._close_with = close_with

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        pass

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for message in self._incoming:
            yield message
        if self._close_with is not None:
            raise self._close_with


def _make_config(**overrides) -> BosonRealtimeLLMConfiguration:
    overrides.setdefault("api_key", "boson-key")
    return BosonRealtimeLLMConfiguration(**overrides)


def _make_service(**config) -> DograhBosonRealtimeLLMService:
    effective = EffectiveAIModelConfiguration(
        is_realtime=True, realtime=_make_config(**config)
    )
    service = create_realtime_llm_service(effective, audio_config=SimpleNamespace())
    service.push_error = AsyncMock()
    return service


def _with_socket(service, **kwargs) -> _FakeSocket:
    sock = _FakeSocket(**kwargs)
    service._websocket = sock
    return sock


def _closed(code: int) -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, ""), None)


# ── Configuration and factory ─────────────────────────────────────────


def test_boson_is_a_realtime_provider_with_hindi_defaults():
    assert ServiceProviders.BOSON_REALTIME in REALTIME_PROVIDERS
    cfg = _make_config()
    assert cfg.provider == "boson_realtime"
    assert cfg.model == "higgs-realtime"
    assert cfg.voice == "default"
    assert cfg.language == "hi"
    assert cfg.turn_detection == "server_vad"
    assert cfg.noise_reduction == "off"


def test_factory_builds_the_boson_service_with_boson_defaults():
    service = _make_service()

    assert isinstance(service, DograhBosonRealtimeLLMService)
    assert service.base_url == f"{BOSON_REALTIME_URL}?model=higgs-realtime"
    assert service.api_key == "boson-key"
    props = service._settings.session_properties
    assert props.output_modalities == ["audio"]
    assert props.audio.output.voice == "default"
    assert props.audio.input.transcription.model == "higgs-stt-3.1"
    assert props.audio.input.transcription.language == "hi"
    turn = props.audio.input.turn_detection
    # Boson's defaults, not OpenAI's 0.5 threshold.
    assert (
        turn.type,
        turn.threshold,
        turn.silence_duration_ms,
        turn.prefix_padding_ms,
    ) == (
        "server_vad",
        0.85,
        500,
        300,
    )
    assert props.audio.input.noise_reduction is None


def test_factory_passes_the_agents_choices():
    service = _make_service(
        voice="voice_abc123",
        language="en",
        temperature=0.6,
        vad_threshold=0.9,
        silence_duration_ms=700,
        prefix_padding_ms=200,
        noise_reduction="near_field",
    )

    props = service._settings.session_properties
    assert props.audio.output.voice == "voice_abc123"
    assert props.audio.input.transcription.language == "en"
    turn = props.audio.input.turn_detection
    assert (turn.threshold, turn.silence_duration_ms, turn.prefix_padding_ms) == (
        0.9,
        700,
        200,
    )
    assert props.audio.input.noise_reduction.type == "near_field"
    assert service._boson_temperature == 0.6


def test_semantic_vad_is_selectable():
    service = _make_service(turn_detection="semantic_vad")
    turn = service._settings.session_properties.audio.input.turn_detection
    assert turn.type == "semantic_vad"


def test_boson_follows_the_providers_turn_frames():
    from api.services.pipecat.run_pipeline import _create_realtime_user_turn_config

    strategies, vad = _create_realtime_user_turn_config(
        ServiceProviders.BOSON_REALTIME.value
    )
    assert vad is None
    assert strategies is not None


# ── What goes over the wire ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_update_matches_what_boson_accepts():
    service = _make_service(temperature=0.4)
    sock = _with_socket(service)

    await service._send_session_update()

    msg = sock.sent[0]
    assert msg["type"] == "session.update"
    session = msg["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "higgs-realtime"
    assert session["temperature"] == 0.4
    assert session["output_modalities"] == ["audio"]
    for rejected in ("include", "prompt", "tracing", "reasoning"):
        assert rejected not in session
    audio_in = session["audio"]["input"]
    assert audio_in["transcription"] == {"model": "higgs-stt-3.1", "language": "hi"}
    assert audio_in["turn_detection"]["type"] == "server_vad"
    assert audio_in["turn_detection"]["threshold"] == 0.85
    assert session["audio"]["output"]["voice"] == "default"


@pytest.mark.asyncio
async def test_no_temperature_is_sent_unless_set():
    service = _make_service()
    sock = _with_socket(service)

    await service._send_session_update()

    assert "temperature" not in sock.sent[0]["session"]


@pytest.mark.asyncio
async def test_connect_sends_the_first_session_update_at_once():
    service = _make_service()
    sock = _FakeSocket()

    async def fake_connect(uri, additional_headers):
        assert uri == f"{BOSON_REALTIME_URL}?model=higgs-realtime"
        assert additional_headers == {"Authorization": "Bearer boson-key"}
        return sock

    service.create_task = lambda coro, *a, **k: coro.close()
    with patch(
        "api.services.pipecat.realtime.boson_realtime.openai_realtime_llm.websocket_connect",
        fake_connect,
    ):
        await service._connect()

    assert isinstance(service._websocket, _BosonEventFilter)
    assert [m["type"] for m in sock.sent] == ["session.update"]


@pytest.mark.asyncio
async def test_a_failed_connection_is_reported():
    service = _make_service()

    async def fake_connect(uri, additional_headers):
        raise OSError("unreachable")

    with patch(
        "api.services.pipecat.realtime.boson_realtime.openai_realtime_llm.websocket_connect",
        fake_connect,
    ):
        await service._connect()

    assert service._websocket is None
    service.push_error.assert_awaited_once()
    assert "Boson" in service.push_error.await_args.kwargs["error_msg"]


@pytest.mark.asyncio
async def test_tool_results_are_not_encoded_twice():
    service = _make_service()
    sock = _with_socket(service)

    await service._send_tool_result("call-1", '{"found": true}')
    await service._send_tool_result("call-2", {"rate": "₹10"})
    await service._send_tool_result("call-3", None)

    outputs = [m["item"]["output"] for m in sock.sent]
    assert outputs == ['{"found": true}', '{"rate": "₹10"}', ""]
    assert all(m["item"]["type"] == "function_call_output" for m in sock.sent)


@pytest.mark.asyncio
async def test_appended_system_messages_go_in_as_user_items():
    service = _make_service()
    sock = _with_socket(service)
    service._api_session_ready = True

    await service._handle_messages_append(
        LLMMessagesAppendFrame(
            messages=[{"role": "system", "content": "The caller is quiet; check in."}],
            run_llm=False,
        )
    )

    (msg,) = sock.sent
    assert msg["type"] == "conversation.item.create"
    assert msg["item"]["role"] == "user"
    assert msg["item"]["content"] == [
        {"type": "input_text", "text": "The caller is quiet; check in."}
    ]


# ── What Boson sends back ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_created_marks_the_session_ready():
    service = _make_service()
    service._create_response = AsyncMock()
    service._run_llm_when_api_session_ready = True

    raw = json.dumps(
        {
            "type": "session.created",
            "session": {
                "id": "sess_1",
                "object": "realtime.session",
                "type": "realtime",
                "model": "higgs-realtime",
                "truncation": "auto",
                "temperature": 0.3,
                "audio": {
                    "input": {
                        "format": {"type": "audio/opus", "frame_size_ms": 20},
                        "turn_detection": {
                            "type": "server_vad",
                            "min_speech_duration": 0.125,
                        },
                    }
                },
            },
        }
    )
    forwarded = await service._filter_server_message(raw)
    evt = events.parse_server_event(forwarded)
    await service._handle_evt_session_created(evt)

    assert service._api_session_ready is True
    service._create_response.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "conversation.context.summarized",
            "summary": "s",
            "summarized_item_ids": ["a"],
            "summary_item_id": "b",
            "token_count_before": 9000,
            "token_count_after": 1200,
        },
        {
            "type": "response.output_audio_transcript.length",
            "delta": "hi",
            "length_ms": 400,
        },
        {"type": "session.idle_timeout", "seconds_idle": 300},
    ],
)
async def test_boson_only_events_never_reach_the_parser(event):
    service = _make_service()

    assert await service._filter_server_message(json.dumps(event)) is None
    service.push_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaching_the_maximum_session_length_is_reported():
    service = _make_service()

    raw = json.dumps({"type": "session.max_duration_reached", "max_duration_sec": 3600})
    assert await service._filter_server_message(raw) is None

    service.push_error.assert_awaited_once()
    assert "3600" in service.push_error.await_args.kwargs["error_msg"]


@pytest.mark.asyncio
async def test_ordinary_errors_keep_the_session_reading():
    service = _make_service()

    raw = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_tool_call_id",
                "message": "unknown call",
                "param": None,
            },
        }
    )
    assert await service._filter_server_message(raw) is None
    service.push_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_out_of_credit_is_reported_so_campaigns_pause():
    service = _make_service()

    raw = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "insufficient_quota",
                "code": "monthly_cap_reached",
                "message": "Monthly spending cap reached",
                "param": None,
            },
        }
    )
    assert await service._filter_server_message(raw) is None

    message = service.push_error.await_args.kwargs["error_msg"]
    assert provider_http_status(None, message) == 402


@pytest.mark.asyncio
async def test_response_done_parses_with_and_without_usage():
    service = _make_service()
    base = {
        "type": "response.done",
        "event_id": "e1",
        "response": {
            "id": "resp_1",
            "object": "realtime.response",
            "status": "cancelled",
            "status_details": None,
            "output": [],
            "metadata": None,
            "conversation_id": "conv_1",
        },
    }

    no_usage = await service._filter_server_message(json.dumps(base))
    assert events.parse_server_event(no_usage).response.usage.total_tokens == 0

    base["response"]["status"] = "completed"
    base["response"]["usage"] = {
        "total_tokens": 1500,
        "input_tokens": 1400,
        "output_tokens": 100,
        "input_token_details": {
            "cached_tokens": 1024,
            "text_tokens": 1200,
            "audio_tokens": 200,
            "image_tokens": 0,
            "cached_tokens_details": {"text_tokens": 1024, "audio_tokens": 0},
        },
        "output_token_details": {"text_tokens": 20, "audio_tokens": 80},
    }
    with_usage = events.parse_server_event(
        await service._filter_server_message(json.dumps(base))
    )
    assert with_usage.response.usage.input_token_details.cached_tokens == 1024


_AUDIO_DELTA = {
    "type": "response.output_audio.delta",
    "response_id": "r",
    "item_id": "i",
    "output_index": 0,
    "content_index": 0,
    "delta": "AAAA",
}


@pytest.mark.asyncio
async def test_events_with_an_event_id_pass_through_untouched():
    service = _make_service()
    for event in (
        {**_AUDIO_DELTA, "event_id": "e9"},
        {
            "type": "input_audio_buffer.speech_started",
            "event_id": "e8",
            "item_id": "i",
            "audio_start_ms": 120,
        },
    ):
        raw = json.dumps(event)
        assert await service._filter_server_message(raw) is raw


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [True, False])
async def test_events_without_an_event_id_still_parse(compact):
    # The pinned parser requires event_id; Boson's docs don't promise one.
    service = _make_service()
    separators = (",", ":") if compact else None

    delta = await service._filter_server_message(
        json.dumps(_AUDIO_DELTA, separators=separators)
    )
    assert events.parse_server_event(delta).delta == "AAAA"

    started = await service._filter_server_message(
        json.dumps(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "i",
                "audio_start_ms": 120,
            },
            separators=separators,
        )
    )
    assert events.parse_server_event(started).audio_start_ms == 120


@pytest.mark.asyncio
async def test_function_call_items_with_boson_fields_parse():
    service = _make_service()
    raw = json.dumps(
        {
            "type": "conversation.item.added",
            "previous_item_id": "prev",
            "item": {
                "id": "item_1",
                "object": "realtime.item",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_1",
                "name": "lookup_area_manager",
                "arguments": "",
                "executor": "client",
            },
        }
    )
    evt = events.parse_server_event(await service._filter_server_message(raw))
    assert evt.item.call_id == "call_1"


# ── Session-ending close codes ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [(3000, 401), (4429, 402)])
async def test_refusal_close_codes_are_reported_in_words_campaigns_recognise(
    code, status
):
    service = _make_service()
    sock = _FakeSocket(incoming=[], close_with=_closed(code))

    received = [m async for m in _BosonEventFilter(sock, service)]

    assert received == []
    message = service.push_error.await_args.kwargs["error_msg"]
    assert provider_http_status(None, message) == status


@pytest.mark.asyncio
async def test_other_closes_are_only_logged():
    service = _make_service()
    sock = _FakeSocket(incoming=[], close_with=_closed(1011))

    _ = [m async for m in _BosonEventFilter(sock, service)]

    service.push_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_closes_during_our_own_disconnect_are_ignored():
    service = _make_service()
    service._disconnecting = True
    sock = _FakeSocket(incoming=[], close_with=_closed(3000))

    _ = [m async for m in _BosonEventFilter(sock, service)]

    service.push_error.assert_not_awaited()


# ── API key check ─────────────────────────────────────────────────────


def _validate_key():
    from api.services.configuration.check_validity import UserConfigurationValidator

    return UserConfigurationValidator()._validate_service(
        _make_config(api_key="bk"), "realtime"
    )


class _SyncSocket:
    def __init__(self, recv_error=None):
        self._recv_error = recv_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        raise self._recv_error


def _rejected(status):
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    return InvalidStatus(Response(status, "", None))


def test_a_working_key_saves():
    seen = {}

    def fake_connect(url, additional_headers, open_timeout):
        seen.update(url=url, headers=additional_headers)
        return _SyncSocket(recv_error=TimeoutError())

    with patch("websockets.sync.client.connect", fake_connect):
        assert _validate_key() == []
    assert seen["url"] == "wss://api.boson.ai/v1/realtime?model=higgs-realtime"
    assert seen["headers"] == {"Authorization": "Bearer bk"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_key_rejected_at_the_handshake_blocks_the_save(status):
    with patch("websockets.sync.client.connect", side_effect=_rejected(status)):
        (err,) = _validate_key()
    assert "Invalid Boson API key" in err["message"]


def test_a_key_rejected_by_close_code_blocks_the_save():
    with patch(
        "websockets.sync.client.connect",
        lambda *a, **k: _SyncSocket(recv_error=_closed(3000)),
    ):
        (err,) = _validate_key()
    assert "Invalid Boson API key" in err["message"]


def test_an_account_without_credit_is_explained():
    with patch(
        "websockets.sync.client.connect",
        lambda *a, **k: _SyncSocket(recv_error=_closed(4429)),
    ):
        (err,) = _validate_key()
    assert "no credit" in err["message"]


def test_unreachable_boson_is_reported():
    with patch("websockets.sync.client.connect", side_effect=TimeoutError()):
        (err,) = _validate_key()
    assert "Could not connect to Boson" in err["message"]


# ── A whole scripted session through pipecat's own receive loop ──────


def _boson_session_script():
    """A call as Boson's docs describe it, events without event_id."""
    return [
        {
            "type": "session.created",
            "session": {"type": "realtime", "truncation": "auto"},
        },
        {
            "type": "response.created",
            "response": {
                "id": "resp_1",
                "object": "realtime.response",
                "status": "in_progress",
                "status_details": None,
                "output": [],
                "usage": None,
                "metadata": None,
                "conversation_id": "conv_1",
            },
        },
        {
            "type": "conversation.item.added",
            "previous_item_id": None,
            "item": {
                "id": "item_a",
                "object": "realtime.item",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "resp_1",
            "item_id": "item_a",
            "output_index": 0,
            "content_index": 0,
            "delta": "Namaskar",
        },
        {
            "type": "response.output_audio_transcript.length",
            "delta": "Namaskar",
            "length_ms": 600,
        },
        {
            **_AUDIO_DELTA,
            "response_id": "resp_1",
            "item_id": "item_a",
            "delta": "AAAAAAAA",
        },
        {
            "type": "response.output_audio.done",
            "response_id": "resp_1",
            "item_id": "item_a",
            "output_index": 0,
            "content_index": 0,
        },
        {
            "type": "response.done",
            "response": {
                "id": "resp_1",
                "object": "realtime.response",
                "status": "completed",
                "status_details": None,
                "output": [],
                "metadata": None,
                "conversation_id": "conv_1",
                "usage": {
                    "total_tokens": 10,
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "input_token_details": {"cached_tokens": 0},
                    "output_token_details": {"audio_tokens": 2},
                },
            },
        },
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "item_u",
            "audio_start_ms": 900,
        },
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "item_u",
            "audio_end_ms": 2400,
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item_u",
            "content_index": 0,
            "transcript": "mera area manager kaun hai",
            "usage": {"type": "duration", "seconds": 2},
        },
        {
            "type": "error",
            "error": {
                "type": "server_error",
                "code": "upstream_error",
                "message": "speech synthesis did not complete",
                "param": None,
            },
        },
        {
            "type": "conversation.item.added",
            "previous_item_id": "item_u",
            "item": {
                "id": "item_f",
                "object": "realtime.item",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_9",
                "name": "lookup_area_manager",
                "arguments": "",
                "executor": "client",
            },
        },
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp_2",
            "item_id": "item_f",
            "output_index": 0,
            "call_id": "call_9",
            "name": "lookup_area_manager",
            "arguments": '{"pincode": "110001"}',
        },
        {
            "type": "conversation.context.summarized",
            "summary": "s",
            "summarized_item_ids": [],
            "summary_item_id": "x",
            "token_count_before": 1,
            "token_count_after": 1,
        },
        {"type": "session.idle_timeout", "seconds_idle": 300},
    ]


@pytest.mark.asyncio
async def test_a_scripted_boson_call_runs_through_the_base_receive_loop():
    from pipecat.frames.frames import (
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
    )

    service = _make_service()
    service._create_response = AsyncMock()
    service.run_function_calls = AsyncMock()
    pushed, broadcast = [], []

    async def push_frame(frame, direction=None):
        pushed.append(frame)

    async def broadcast_frame(frame_cls, **kwargs):
        broadcast.append(frame_cls(**kwargs))

    service.push_frame = push_frame
    service.broadcast_frame = broadcast_frame
    service.broadcast_interruption = AsyncMock()
    service.start_llm_usage_metrics = AsyncMock()
    service.stop_processing_metrics = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()

    script = [json.dumps(e, separators=(",", ":")) for e in _boson_session_script()]
    service._websocket = _BosonEventFilter(_FakeSocket(incoming=script), service)

    await service._receive_task_handler()

    # The session became ready from session.created.
    assert service._api_session_ready is True
    # The greeting's audio reached the pipeline, bracketed once.
    audio = [f for f in pushed if isinstance(f, TTSAudioRawFrame)]
    assert len(audio) == 1 and audio[0].sample_rate == 24000
    assert sum(isinstance(f, TTSStartedFrame) for f in pushed) == 1
    assert sum(isinstance(f, TTSStoppedFrame) for f in pushed) == 1
    # The caller's words became a final transcript.
    (transcript,) = [f for f in broadcast if isinstance(f, TranscriptionFrame)]
    assert transcript.text == "mera area manager kaun hai"
    assert transcript.finalized is True
    # The non-fatal error didn't stop the loop: the tool call after it ran.
    (call,) = service.run_function_calls.await_args.args[0]
    assert call.function_name == "lookup_area_manager"
    assert call.arguments == {"pincode": "110001"}
    assert call.tool_call_id == "call_9"
    # Usage was reported from Boson's response.done.
    tokens = service.start_llm_usage_metrics.await_args.args[0]
    assert (tokens.prompt_tokens, tokens.completion_tokens) == (8, 2)
    # Nothing in the script should have been reported as a pipeline error.
    service.push_error.assert_not_awaited()


# ── One reply at a time ──────────────────────────────────────────────


def _response_create() -> events.ResponseCreateEvent:
    return events.ResponseCreateEvent(
        response=events.ResponseProperties(output_modalities=["audio"])
    )


def _response_done(status="completed") -> str:
    return json.dumps(
        {
            "type": "response.done",
            "response": {
                "id": "r",
                "object": "realtime.response",
                "status": status,
                "status_details": None,
                "output": [],
            },
        }
    )


async def _deliver(service, raw: str):
    forwarded = await service._filter_server_message(raw)
    if forwarded is not None and json.loads(forwarded)["type"] == "response.done":
        await service._handle_evt_response_done(events.parse_server_event(forwarded))


def _quiet(service):
    service.push_frame = AsyncMock()
    service.start_llm_usage_metrics = AsyncMock()
    service.stop_processing_metrics = AsyncMock()


@pytest.mark.asyncio
async def test_a_reply_requested_while_one_plays_waits_for_it():
    service = _make_service()
    _quiet(service)
    sock = _with_socket(service)

    await service.send_client_event(_response_create())  # e.g. the greeting
    await _deliver(service, json.dumps({"type": "response.created", "response": {}}))
    await service.send_client_event(
        _response_create()
    )  # e.g. a tool result's follow-up
    assert [m["type"] for m in sock.sent] == ["response.create"]

    await _deliver(service, _response_done())
    assert [m["type"] for m in sock.sent] == ["response.create", "response.create"]


@pytest.mark.asyncio
async def test_a_reply_boson_started_itself_also_holds_ours():
    service = _make_service()
    _quiet(service)
    sock = _with_socket(service)

    # Server VAD started a reply when the caller stopped talking.
    await _deliver(service, json.dumps({"type": "response.created", "response": {}}))
    await service.send_client_event(_response_create())
    assert sock.sent == []

    # The caller barged in: Boson cancels it, and ours goes out.
    await _deliver(service, _response_done("cancelled"))
    assert [m["type"] for m in sock.sent] == ["response.create"]


@pytest.mark.asyncio
async def test_a_refused_request_does_not_strand_the_next_one():
    service = _make_service()
    _quiet(service)
    sock = _with_socket(service)

    await service.send_client_event(_response_create())
    await service.send_client_event(_response_create())  # held behind the first
    await _deliver(
        service,
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "rate_limit_exceeded",
                    "message": "slow down",
                    "param": None,
                },
            }
        ),
    )

    assert [m["type"] for m in sock.sent] == ["response.create", "response.create"]


@pytest.mark.asyncio
async def test_an_unrelated_error_keeps_the_reply_held():
    service = _make_service()
    _quiet(service)
    sock = _with_socket(service)

    await service.send_client_event(_response_create())
    await service.send_client_event(_response_create())
    await _deliver(
        service,
        json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_tool_call_id",
                    "message": "unknown call",
                    "param": None,
                },
            }
        ),
    )

    assert [m["type"] for m in sock.sent] == ["response.create"]


# ── Behaviour learned from Boson's own Pipecat plugin ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["state.changed", "should_end_call", "something.new"])
async def test_undocumented_events_are_dropped_not_parsed(kind):
    service = _make_service()
    assert (
        await service._filter_server_message(json.dumps({"type": kind, "x": 1})) is None
    )
    service.push_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_repeated_transcript_is_passed_on_once():
    service = _make_service()
    raw = json.dumps(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1",
            "content_index": 0,
            "transcript": "haan ji",
        }
    )

    assert await service._filter_server_message(raw) is not None
    assert await service._filter_server_message(raw) is None


@pytest.mark.asyncio
async def test_turn_detection_goes_in_the_first_session_update_only():
    service = _make_service()
    sock = _with_socket(service)

    await service._send_session_update()
    await service._send_session_update()  # e.g. a node change

    first, later = (m["session"]["audio"]["input"] for m in sock.sent)
    assert first["turn_detection"]["type"] == "server_vad"
    assert "turn_detection" not in later
    assert later["transcription"]["model"] == "higgs-stt-3.1"
    # The stored settings keep it, for a reconnect.
    assert service._settings.session_properties.audio.input.turn_detection is not None


@pytest.mark.asyncio
async def test_a_reconnect_sends_turn_detection_again():
    service = _make_service()
    sock = _with_socket(service)
    await service._send_session_update()

    await service._disconnect()
    sock = _with_socket(service)
    await service._send_session_update()

    assert (
        sock.sent[0]["session"]["audio"]["input"]["turn_detection"]["type"]
        == "server_vad"
    )


@pytest.mark.asyncio
async def test_semantic_vad_always_says_auto_eagerness():
    service = _make_service(turn_detection="semantic_vad")
    sock = _with_socket(service)

    await service._send_session_update()

    assert sock.sent[0]["session"]["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "auto",
    }


@pytest.mark.asyncio
async def test_a_held_reply_is_dropped_when_the_caller_cuts_in():
    service = _make_service()
    _quiet(service)
    service.broadcast_frame = AsyncMock()
    service.broadcast_interruption = AsyncMock()
    sock = _with_socket(service)

    await service.send_client_event(_response_create())
    await service.send_client_event(
        _response_create()
    )  # held: a tool result's follow-up
    await service._handle_evt_speech_started(
        SimpleNamespace(item_id="u2", audio_start_ms=100)
    )
    await _deliver(service, _response_done("cancelled"))

    # Boson answers when the caller stops; no second reply from us.
    assert [m["type"] for m in sock.sent] == ["response.create"]
    service.broadcast_interruption.assert_awaited_once()
