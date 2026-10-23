"""Inworld Realtime: Dograh wrapper, wire format and factory.

Inworld runs STT, a routed LLM and Inworld TTS in one session. These tests
check what Dograh sends over the socket (the session.update JSON, tool
results, the session key) and the same engine behaviour the Grok wrapper has.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMMessagesAppendFrame,
    TTSSpeakFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.inworld.realtime import events

from api.schemas.ai_model_configuration import EffectiveAIModelConfiguration
from api.services.configuration.registry import (
    REALTIME_PROVIDERS,
    InworldRealtimeLLMConfiguration,
    ServiceProviders,
)
from api.services.pipecat.realtime.inworld_realtime import (
    DograhInworldRealtimeLLMService,
)
from api.services.pipecat.service_factory import create_realtime_llm_service


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def _make_service(**kwargs) -> DograhInworldRealtimeLLMService:
    kwargs.setdefault("api_key", "test-key")
    service = DograhInworldRealtimeLLMService(**kwargs)
    service._create_response = AsyncMock()
    service._process_completed_function_calls = AsyncMock()
    return service


def _with_socket(service) -> _FakeSocket:
    sock = _FakeSocket()
    service._websocket = sock
    return sock


# ── What goes over the wire ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_update_sends_provider_data_under_inworlds_name():
    service = _make_service(language="hi")
    sock = _with_socket(service)

    await service._send_session_update()

    session = sock.sent[0]["session"]
    assert sock.sent[0]["type"] == "session.update"
    assert "provider_data" not in session
    assert session["providerData"]["metadata"] == {"sdk": "pipecat-realtime"}
    assert session["providerData"]["auto_tool_response"] is False
    assert session["providerData"]["tts"] == {"segmenter_strategy": "fast_start", "language": "hi"}
    assert session["audio"]["input"]["transcription"] == {
        "model": "inworld/inworld-stt-1",
        "language": "hi",
    }


@pytest.mark.asyncio
async def test_session_update_carries_the_agents_models_and_voice():
    service = _make_service(
        llm_model="google/gemini-2.5-flash",
        voice="Riya",
        tts_model="inworld-tts-2",
        stt_model="assemblyai/universal-streaming-multilingual",
    )
    service._ensure_audio_config(8000, 8000)
    sock = _with_socket(service)

    await service._send_session_update()

    session = sock.sent[0]["session"]
    assert session["model"] == "google/gemini-2.5-flash"
    assert session["audio"]["output"]["voice"] == "Riya"
    assert session["audio"]["output"]["model"] == "inworld-tts-2"
    assert session["audio"]["output"]["format"]["rate"] == 8000
    assert session["audio"]["input"]["format"]["rate"] == 8000
    assert session["audio"]["input"]["transcription"]["model"] == (
        "assemblyai/universal-streaming-multilingual"
    )
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    # No language set: none is sent, Inworld auto-detects.
    assert "language" not in session["audio"]["input"]["transcription"]
    assert session["providerData"]["tts"] == {"segmenter_strategy": "fast_start"}


@pytest.mark.asyncio
async def test_session_update_does_not_change_stored_settings():
    service = _make_service(language="en")
    _with_socket(service)

    await service._send_session_update()
    await service._send_session_update()

    props = service._settings.session_properties
    assert props.provider_data is None
    assert not hasattr(props.audio.input.transcription, "language")


@pytest.mark.asyncio
async def test_other_events_are_sent_unchanged():
    service = _make_service(language="hi")
    sock = _with_socket(service)

    await service.send_client_event(events.InputAudioBufferAppendEvent(audio="AAAA"))

    assert sock.sent[0]["type"] == "input_audio_buffer.append"
    assert sock.sent[0]["audio"] == "AAAA"
    assert "providerData" not in sock.sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,want",
    [
        ('{"status":"success","data":{"area_manager":"Ravi"}}',
         '{"status":"success","data":{"area_manager":"Ravi"}}'),
        ({"status": "success", "data": "हाँ"}, '{"status": "success", "data": "हाँ"}'),
        ("plain text", "plain text"),
        (None, ""),
    ],
)
async def test_tool_result_is_encoded_once(result, want):
    service = _make_service()
    sock = _with_socket(service)

    await service._send_tool_result("call-1", result)

    item = sock.sent[0]["item"]
    assert sock.sent[0]["type"] == "conversation.item.create"
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call-1"
    assert item["output"] == want


@pytest.mark.asyncio
async def test_each_connection_gets_its_own_session_key():
    uris = []

    async def fake_connect(uri, additional_headers):
        uris.append((uri, additional_headers))
        return _FakeSocket()

    with patch(
        "pipecat.services.inworld.realtime.llm.websocket_connect", fake_connect
    ):
        for _ in range(5):
            service = _make_service(api_key="secret-key")
            service.create_task = lambda coro: coro.close()
            await service._connect()

    keys = {uri.split("key=")[1].split("&")[0] for uri, _ in uris}
    assert len(keys) == 5
    uri, headers = uris[0]
    assert uri.startswith("wss://api.inworld.ai/api/v1/realtime/session?")
    assert "protocol=realtime" in uri
    assert headers == {"Authorization": "Basic secret-key"}


# ── Engine behaviour (as in the Grok wrapper) ─────────────────────────


@pytest.mark.asyncio
async def test_tts_greeting_sends_static_greeting_prompt():
    service = _make_service()
    service._context = LLMContext([{"role": "user", "content": "Existing context"}])
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    service.push_frame = AsyncMock()
    service.start_processing_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()

    await service.process_frame(
        TTSSpeakFrame("Namaskar, main Vectus se Aastha.", append_to_context=True),
        FrameDirection.DOWNSTREAM,
    )

    sent = [c.args[0] for c in service.send_client_event.await_args_list]
    assert isinstance(sent[0], events.ConversationItemCreateEvent)
    assert sent[0].item.content[0].text == "Existing context"
    assert isinstance(sent[1], events.SessionUpdateEvent)
    prompt = sent[2].item.content[0].text
    assert "Greet the caller now" in prompt
    assert prompt.endswith('"Namaskar, main Vectus se Aastha."')
    assert isinstance(sent[-1], events.ResponseCreateEvent)
    service._create_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_greeting_waits_for_session_updated():
    service = _make_service()
    service._context = LLMContext([{"role": "user", "content": "Existing context"}])

    await service.process_frame(
        TTSSpeakFrame("Hello.", append_to_context=True), FrameDirection.DOWNSTREAM
    )
    assert service._pending_initial_greeting_text == "Hello."

    service.send_client_event = AsyncMock()
    service.push_frame = AsyncMock()
    service.start_processing_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    await service._handle_evt_session_updated(SimpleNamespace())

    sent = [c.args[0] for c in service.send_client_event.await_args_list]
    assert sent[2].item.content[0].text.endswith('"Hello."')
    assert isinstance(sent[-1], events.ResponseCreateEvent)
    assert service._pending_initial_greeting_text is None
    assert service._run_llm_when_api_session_ready is False


@pytest.mark.asyncio
async def test_messages_append_frame_is_sent_and_runs_the_llm():
    service = _make_service()
    service._api_session_ready = True
    service.send_client_event = AsyncMock()
    service._send_manual_response_create = AsyncMock()

    await service._handle_messages_append(
        LLMMessagesAppendFrame(
            [{"role": "user", "content": "Kya aap abhi bhi line par hain?"}],
            run_llm=True,
        )
    )

    event = service.send_client_event.await_args.args[0]
    assert event.item.content == [
        events.ItemContent(type="input_text", text="Kya aap abhi bhi line par hain?")
    ]
    service._send_manual_response_create.assert_awaited_once()


@pytest.mark.asyncio
async def test_muted_caller_audio_is_not_sent():
    service = _make_service()
    service._llm_needs_conversation_setup = False
    sock = _with_socket(service)
    service.push_frame = AsyncMock()
    audio = InputAudioRawFrame(audio=b"\x01\x00" * 4000, sample_rate=8000, num_channels=1)

    await service.process_frame(UserMuteStartedFrame(), FrameDirection.DOWNSTREAM)
    await service._send_user_audio(audio)
    assert sock.sent == []

    await service.process_frame(UserMuteStoppedFrame(), FrameDirection.DOWNSTREAM)
    await service._send_user_audio(audio)
    assert sock.sent and sock.sent[0]["type"] == "input_audio_buffer.append"


@pytest.mark.asyncio
async def test_ordinary_tool_runs_while_bot_is_speaking():
    service = _make_service()
    service._context = LLMContext()
    service.run_function_calls = AsyncMock()
    service._bot_is_speaking = True
    service._pending_function_calls["call-1"] = SimpleNamespace(name="vectus_product_lookup")

    await service._handle_evt_function_call_arguments_done(
        SimpleNamespace(call_id="call-1", name=None, arguments='{"product":"seat cover"}')
    )

    calls = service.run_function_calls.await_args.args[0]
    assert calls[0].function_name == "vectus_product_lookup"
    assert calls[0].arguments == {"product": "seat cover"}
    assert service._deferred_node_transition_function_calls == []


@pytest.mark.asyncio
async def test_node_transition_waits_until_bot_stops_speaking():
    service = _make_service()
    service._context = LLMContext()
    service.run_function_calls = AsyncMock()
    service._bot_is_speaking = True
    service.register_function("end_call", AsyncMock(), is_node_transition=True)
    service._pending_function_calls["call-1"] = SimpleNamespace(name="end_call")

    await service._handle_evt_function_call_arguments_done(
        SimpleNamespace(call_id="call-1", name="end_call", arguments="")
    )
    service.run_function_calls.assert_not_awaited()

    await service._run_pending_node_transition_function_calls()
    service.run_function_calls.assert_awaited_once()
    assert service._deferred_node_transition_function_calls == []


@pytest.mark.asyncio
async def test_completed_transcription_is_broadcast_as_finalized():
    service = _make_service()
    service._call_event_handler = AsyncMock()
    service.broadcast_frame = AsyncMock()

    await service._handle_evt_input_audio_transcription_completed(
        SimpleNamespace(item_id="item-1", transcript=" Jaipur, Rajasthan ")
    )

    kwargs = service.broadcast_frame.await_args.kwargs
    assert service.broadcast_frame.await_args.args[0].__name__ == "TranscriptionFrame"
    assert kwargs["text"] == "Jaipur, Rajasthan"
    assert kwargs["finalized"] is True


@pytest.mark.asyncio
async def test_empty_transcription_is_dropped():
    service = _make_service()
    service._call_event_handler = AsyncMock()
    service.broadcast_frame = AsyncMock()

    await service._handle_evt_input_audio_transcription_completed(
        SimpleNamespace(item_id="item-1", transcript="  ")
    )

    service.broadcast_frame.assert_not_awaited()


# ── Configuration and factory ─────────────────────────────────────────


def test_inworld_is_a_realtime_provider_with_hindi_defaults():
    assert ServiceProviders.INWORLD_REALTIME in REALTIME_PROVIDERS
    cfg = InworldRealtimeLLMConfiguration(api_key="k")
    assert cfg.provider == "inworld_realtime"
    assert cfg.model == "openai/gpt-4.1-mini"
    assert cfg.stt_model == "inworld/inworld-stt-1"
    assert cfg.tts_model == "inworld-tts-2"
    assert cfg.voice == "Riya"
    assert cfg.language == "hi"


def test_factory_creates_the_dograh_inworld_service():
    effective_config = EffectiveAIModelConfiguration(
        is_realtime=True,
        realtime=InworldRealtimeLLMConfiguration(
            api_key="inworld-key",
            model="google/gemini-2.5-flash",
            voice="Aanya",
            stt_model="assemblyai/u3-rt-pro",
            language="en",
        ),
    )

    service = create_realtime_llm_service(effective_config, audio_config=SimpleNamespace())

    assert isinstance(service, DograhInworldRealtimeLLMService)
    props = service._settings.session_properties
    assert props.model == "google/gemini-2.5-flash"
    assert props.audio.output.voice == "Aanya"
    assert props.audio.output.model == "inworld-tts-2"
    assert props.audio.input.transcription.model == "assemblyai/u3-rt-pro"
    assert service._dograh_language == "en"
    assert service.api_key == "inworld-key"


# ── API key check ─────────────────────────────────────────────────────


def _validate_key():
    from api.services.configuration.check_validity import UserConfigurationValidator

    return UserConfigurationValidator()._validate_service(
        InworldRealtimeLLMConfiguration(api_key="realtime-key"), "realtime"
    )


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _rejected(status):
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    return InvalidStatus(Response(status, "", None))


def test_a_realtime_key_without_voice_access_saves():
    seen = {}

    def fake_connect(url, additional_headers, open_timeout):
        seen.update(url=url, headers=additional_headers)
        return _Socket()

    with patch("websockets.sync.client.connect", fake_connect):
        assert _validate_key() == []
    assert seen["url"].startswith("wss://api.inworld.ai/api/v1/realtime/session?key=")
    assert seen["headers"] == {"Authorization": "Basic realtime-key"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_blocks_the_save(status):
    with patch("websockets.sync.client.connect", side_effect=_rejected(status)):
        (err,) = _validate_key()
    assert "Invalid Inworld API key" in err["message"]


def test_other_statuses_do_not_block_the_save():
    with patch("websockets.sync.client.connect", side_effect=_rejected(503)):
        assert _validate_key() == []


def test_unreachable_inworld_is_reported():
    with patch("websockets.sync.client.connect", side_effect=TimeoutError()):
        (err,) = _validate_key()
    assert "Could not connect to Inworld Realtime" in err["message"]


# ── Expected words and eagerness ──────────────────────────────────────


@pytest.mark.asyncio
async def test_expected_words_and_eagerness_reach_inworld():
    service = _make_service(
        language="hi", eagerness="medium", expected_words=[" Vectus", "Puff ", "", "Samba"]
    )
    sock = _with_socket(service)

    await service._send_session_update()

    audio_in = sock.sent[0]["session"]["audio"]["input"]
    assert audio_in["transcription"]["prompts"] == ["Vectus", "Puff", "Samba"]
    assert audio_in["transcription"]["language"] == "hi"
    assert audio_in["turn_detection"]["eagerness"] == "medium"


@pytest.mark.asyncio
async def test_no_expected_words_sends_no_prompts_and_keeps_low_eagerness():
    service = _make_service()
    sock = _with_socket(service)

    await service._send_session_update()

    audio_in = sock.sent[0]["session"]["audio"]["input"]
    assert "prompts" not in audio_in["transcription"]
    assert audio_in["turn_detection"]["eagerness"] == "low"


def test_factory_passes_expected_words_and_eagerness():
    effective_config = EffectiveAIModelConfiguration(
        is_realtime=True,
        realtime=InworldRealtimeLLMConfiguration(
            api_key="k",
            expected_words="Vectus, Puff,Cool\nSamba,",
            eagerness="high",
        ),
    )

    service = create_realtime_llm_service(effective_config, audio_config=SimpleNamespace())

    assert service._expected_words == ["Vectus", "Puff", "Cool", "Samba"]
    turn = service._settings.session_properties.audio.input.turn_detection
    assert turn.eagerness == "high"


def test_new_settings_default_to_todays_behaviour():
    cfg = InworldRealtimeLLMConfiguration(api_key="k")
    assert cfg.expected_words is None
    assert cfg.eagerness == "low"
    with pytest.raises(ValueError):
        InworldRealtimeLLMConfiguration(api_key="k", eagerness="bogus")


# ── A reply asked for while one is running is held, not lost ──────────


def _server_event(data: dict):
    return events.parse_server_event(json.dumps(data))


def _response_done(status="completed"):
    return _server_event({
        "type": "response.done",
        "event_id": "evt-1",
        "response": {"id": "resp-1", "object": "realtime.response", "status": status, "output": []},
    })


def _sent_types(sock):
    return [m["type"] for m in sock.sent]


@pytest.mark.asyncio
async def test_reply_is_held_while_one_runs_and_sent_when_it_is_done():
    service = _make_service()
    sock = _with_socket(service)
    service.push_frame = AsyncMock()
    service.stop_processing_metrics = AsyncMock()

    await service._on_server_event({"type": "response.created"})
    # The tool finished while the bot was still speaking its first reply.
    await service._send_tool_result("call-1", {"status": "success"})
    await service.send_client_event(events.ResponseCreateEvent())
    assert _sent_types(sock) == ["conversation.item.create"]

    await service._handle_evt_response_done(_response_done())

    assert _sent_types(sock) == ["conversation.item.create", "response.create"]
    assert service._held_response_create is None


@pytest.mark.asyncio
async def test_reply_refused_by_inworld_is_sent_again_after_the_running_one():
    service = _make_service()
    sock = _with_socket(service)
    service.push_frame = AsyncMock()
    service.stop_processing_metrics = AsyncMock()

    # Sent before we heard of the server's own reply, then refused.
    await service.send_client_event(events.ResponseCreateEvent())
    await service._on_server_event({
        "type": "error",
        "error": {"type": "invalid_request_error",
                  "code": "conversation_already_has_active_response",
                  "message": "Conversation already has an active response"},
    })
    await service._handle_evt_response_done(_response_done())

    assert _sent_types(sock) == ["response.create", "response.create"]


@pytest.mark.asyncio
async def test_caller_speaking_drops_the_held_reply():
    service = _make_service()
    sock = _with_socket(service)
    service.push_frame = AsyncMock()
    service.stop_processing_metrics = AsyncMock()
    service.broadcast_frame = AsyncMock()
    service.broadcast_interruption = AsyncMock()

    await service._on_server_event({"type": "response.created"})
    await service.send_client_event(events.ResponseCreateEvent())
    await service._handle_evt_speech_started(SimpleNamespace(item_id="i"))
    await service._handle_evt_response_done(_response_done("cancelled"))

    assert sock.sent == []


@pytest.mark.asyncio
async def test_replies_flow_normally_when_none_is_running():
    service = _make_service()
    sock = _with_socket(service)

    await service.send_client_event(events.ResponseCreateEvent())
    await service.send_client_event(events.ResponseCreateEvent())

    assert _sent_types(sock) == ["response.create", "response.create"]


@pytest.mark.asyncio
async def test_disconnect_forgets_a_running_reply():
    service = _make_service()
    _with_socket(service)
    service._websocket.close = AsyncMock()
    await service._on_server_event({"type": "response.created"})

    await service._disconnect()

    assert service._response_active is False
    assert service._held_response_create is None


@pytest.mark.asyncio
async def test_event_tap_sees_events_but_skips_audio_and_passes_everything_on():
    seen = []

    async def on_event(evt):
        seen.append(evt["type"])

    class _Source:
        def __init__(self, messages):
            self._messages = messages

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for m in self._messages:
                yield m

    from api.services.pipecat.realtime.inworld_realtime import _ServerEventTap

    messages = [
        json.dumps({"type": "response.created"}),
        json.dumps({"type": "response.output_audio.delta", "delta": "AAAA"}),
        "not json",
        json.dumps({"type": "input_audio_buffer.speech_stopped"}),
    ]
    tap = _ServerEventTap(_Source(messages), on_event)

    passed = [m async for m in tap]

    assert passed == messages
    assert seen == ["response.created", "input_audio_buffer.speech_stopped"]


@pytest.mark.asyncio
async def test_every_session_asks_for_low_latency_and_caching():
    service = _make_service()
    sock = _with_socket(service)

    await service._send_session_update()

    pd = sock.sent[0]["session"]["providerData"]
    assert pd["caching"] == {"enabled": True}
    assert pd["text_generation_config"] == {"reasoning": {"effort": "NONE"}}
    assert pd["tts"]["segmenter_strategy"] == "fast_start"


def test_soniox_and_flash_are_offered():
    props = InworldRealtimeLLMConfiguration.model_json_schema()["properties"]
    assert "soniox/stt-rt-v5" in props["stt_model"]["examples"]
    assert "inworld-tts-2-flash" in props["tts_model"]["examples"]
