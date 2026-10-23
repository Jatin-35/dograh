"""Inworld Realtime with Dograh's own transcriber and voice (stt_tts_source "dograh").

The call runs as a normal pipeline (Dograh STT, Dograh turn-taking, Dograh
TTS) around the Inworld Realtime session, which acts as the LLM in text mode.
Covers the saved configuration, the switch, and the wrapper's text mode. No
network: every socket here is fake.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.inworld.realtime import events

from api.schemas.ai_model_configuration import (
    EffectiveAIModelConfiguration,
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.registry import (
    InworldRealtimeLLMConfiguration,
)
from api.services.pipecat.realtime.inworld_realtime import (
    DograhInworldRealtimeLLMService,
)
from api.services.pipecat.run_pipeline import uses_own_stt_tts
from api.services.pipecat.service_factory import create_realtime_llm_service


def _v2(realtime_extra=None, stt=True, tts=True):
    realtime = {
        "realtime": {"provider": "inworld_realtime", "api_key": "k", **(realtime_extra or {})},
        "llm": {"provider": "openai", "api_key": "llm-k", "model": "gpt-4.1"},
    }
    if stt:
        realtime["stt"] = {"provider": "deepgram", "api_key": "dg-k", "model": "nova-3-general"}
    if tts:
        realtime["tts"] = {"provider": "elevenlabs", "api_key": "el-k", "model": "eleven_flash_v2_5", "voice": "Rachel"}
    return OrganizationAIModelConfigurationV2.model_validate(
        {"version": 2, "mode": "byok", "byok": {"mode": "realtime", "realtime": realtime}}
    )


# ── Saved configuration ───────────────────────────────────────────────


def test_a_realtime_config_carries_its_own_transcriber_and_voice():
    effective = compile_ai_model_configuration_v2(_v2({"stt_tts_source": "dograh"}))
    assert effective.is_realtime is True
    assert effective.stt.provider == "deepgram"
    assert effective.tts.provider == "elevenlabs"
    assert effective.realtime.stt_tts_source == "dograh"


def test_realtime_configs_without_them_still_work():
    effective = compile_ai_model_configuration_v2(_v2(stt=False, tts=False))
    assert effective.stt is None and effective.tts is None


def test_dograh_managed_stt_is_refused_inside_byok_realtime():
    with pytest.raises(ValueError):
        OrganizationAIModelConfigurationV2.model_validate({
            "version": 2, "mode": "byok", "byok": {"mode": "realtime", "realtime": {
                "realtime": {"provider": "inworld_realtime", "api_key": "k"},
                "llm": {"provider": "openai", "api_key": "x", "model": "gpt-4.1"},
                "stt": {"provider": "dograh", "api_key": "x", "model": "default"},
            }},
        })


# ── The switch ────────────────────────────────────────────────────────


def test_the_switch_defaults_to_inworlds_own_stt_and_voice():
    assert InworldRealtimeLLMConfiguration(api_key="k").stt_tts_source == "inworld"


@pytest.mark.parametrize("source,expected", [("dograh", True), ("inworld", False)])
def test_uses_own_stt_tts(source, expected):
    cfg = compile_ai_model_configuration_v2(_v2({"stt_tts_source": source}))
    assert uses_own_stt_tts(cfg) is expected


def test_non_realtime_and_other_providers_never_count():
    assert uses_own_stt_tts(EffectiveAIModelConfiguration()) is False
    other = SimpleNamespace(is_realtime=True, realtime=SimpleNamespace(provider="openai_realtime", stt_tts_source="dograh"))
    assert uses_own_stt_tts(other) is False


def test_inworld_audio_settings_hide_when_our_stt_and_voice_are_used():
    props = InworldRealtimeLLMConfiguration.model_json_schema()["properties"]
    for field in ("stt_model", "expected_words", "noise_reduction", "voice", "tts_model",
                  "audio_start", "turn_detection", "eagerness", "local_min_words"):
        assert props[field]["visible_when"]["stt_tts_source"] == "inworld", field
    for field in ("model", "temperature", "max_reply_tokens", "stt_tts_source"):
        assert "visible_when" not in props[field], field


# ── The wrapper in text mode ──────────────────────────────────────────


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    def types(self):
        return [m["type"] for m in self.sent]


def _text_service(**kwargs):
    service = DograhInworldRealtimeLLMService(api_key="k", stt_tts_source="dograh", **kwargs)
    sock = _FakeSocket()
    service._websocket = sock
    service.push_frame = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    service.start_processing_metrics = AsyncMock()
    service.stop_all_metrics = AsyncMock()
    return service, sock


def _ready(service):
    service._api_session_ready = True


def test_factory_builds_a_text_mode_session():
    cfg = compile_ai_model_configuration_v2(_v2({"stt_tts_source": "dograh", "turn_detection": "dograh_local"}))
    service = create_realtime_llm_service(cfg, audio_config=SimpleNamespace())
    assert service._text_io is True
    # Dograh's normal turn rules drive the call; the audio-gate mode is off.
    assert service._local_turns is False
    assert service._is_manual_turn_detection() is True
    assert service._settings.session_properties.output_modalities == ["text"]


@pytest.mark.asyncio
async def test_session_asks_for_text_replies_with_turn_detection_off():
    service, sock = _text_service(speech_threshold=0.6)
    await service._send_session_update()
    session = sock.sent[0]["session"]
    assert session["output_modalities"] == ["text"]
    assert session["audio"]["input"]["turn_detection"] is None
    assert "stt" not in session["providerData"]


@pytest.mark.asyncio
async def test_no_caller_audio_ever_reaches_inworld():
    service, sock = _text_service()
    service._llm_needs_conversation_setup = False
    for _ in range(20):
        await service._send_user_audio(InputAudioRawFrame(audio=b"\x01\x00" * 160, sample_rate=8000, num_channels=1))
    assert sock.sent == []


@pytest.mark.asyncio
async def test_first_turn_sets_up_the_conversation_and_asks_for_a_text_reply():
    service, sock = _text_service()
    _ready(service)
    context = LLMContext([
        {"role": "system", "content": "You are Aastha."},
        {"role": "assistant", "content": "Namaskar, main Aastha."},
        {"role": "user", "content": "Haan ji boliye"},
    ])
    await service._handle_context(context)
    types = sock.types()
    assert "session.update" in types
    assert types[-1] == "response.create"
    assert sock.sent[-1]["response"]["modalities"] == ["text"]
    texts = [m["item"]["content"][0].get("text") for m in sock.sent if m["type"] == "conversation.item.create"]
    # Pipecat's Inworld adapter packs the history so far into one message
    # (as for every realtime agent); the caller's words travel inside it.
    assert any("Haan ji boliye" in t for t in texts)
    assert service._text_sent_upto == 3


@pytest.mark.asyncio
async def test_later_turns_send_only_the_new_user_text():
    service, sock = _text_service()
    _ready(service)
    context = LLMContext([{"role": "system", "content": "S"}, {"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()

    context.add_message({"role": "assistant", "content": "Ji, bataiye."})
    context.add_message({"role": "user", "content": "Main Jaipur se hoon"})
    await service._handle_context(context)

    items = [m for m in sock.sent if m["type"] == "conversation.item.create"]
    assert len(items) == 1
    assert items[0]["item"]["role"] == "user"
    assert items[0]["item"]["content"][0]["text"] == "Main Jaipur se hoon"
    assert sock.types()[-1] == "response.create"


@pytest.mark.asyncio
async def test_empty_turn_recovery_prompt_reaches_inworld():
    service, sock = _text_service()
    _ready(service)
    context = LLMContext([{"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "system", "content": "Ask the caller to repeat."})
    await service._handle_context(context)
    items = [m["item"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert items == [{**items[0], "role": "system"}]
    assert items[0]["content"][0]["text"] == "Ask the caller to repeat."


@pytest.mark.asyncio
async def test_a_context_frame_with_nothing_new_asks_for_nothing():
    service, sock = _text_service()
    _ready(service)
    context = LLMContext([{"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "assistant", "content": "Ji"})
    await service._handle_context(context)
    assert "response.create" not in sock.types()


@pytest.mark.asyncio
async def test_before_the_session_is_ready_the_reply_waits_for_it():
    service, sock = _text_service()
    await service._handle_context(LLMContext([{"role": "user", "content": "hello"}]))
    assert sock.sent == []
    assert service._run_llm_when_api_session_ready is True


@pytest.mark.asyncio
async def test_text_deltas_become_text_for_the_voice():
    service, _ = _text_service()
    await service._on_server_event({"type": "response.output_text.delta", "delta": "Ji, "})
    await service._on_server_event({"type": "response.text.delta", "delta": "bilkul."})
    frames = [c.args[0] for c in service.push_frame.await_args_list]
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == ["Ji, ", "bilkul."]


@pytest.mark.asyncio
async def test_text_deltas_are_ignored_when_inworld_speaks_itself():
    service = DograhInworldRealtimeLLMService(api_key="k")
    service.push_frame = AsyncMock()
    service.stop_ttfb_metrics = AsyncMock()
    await service._on_server_event({"type": "response.output_text.delta", "delta": "Ji"})
    service.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_greeting_goes_straight_to_our_voice():
    service, sock = _text_service()
    frame = TTSSpeakFrame("Namaskar, main Vectus se Aastha.", append_to_context=True)
    await service.process_frame(frame, FrameDirection.DOWNSTREAM)
    service.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)
    assert sock.sent == []
    assert service._handled_initial_context is True


@pytest.mark.asyncio
async def test_a_transition_line_is_spoken_and_inworld_is_told():
    service, sock = _text_service()
    _ready(service)
    service._llm_needs_conversation_setup = False
    service._handled_initial_context = True
    frame = TTSSpeakFrame("Ek minute, main check karti hoon.", append_to_context=False)
    await service.process_frame(frame, FrameDirection.DOWNSTREAM)
    service.push_frame.assert_awaited_with(frame, FrameDirection.DOWNSTREAM)
    (note,) = [m["item"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert note["role"] == "system"
    assert "Ek minute, main check karti hoon." in note["content"][0]["text"]
    assert "response.create" not in sock.types()


@pytest.mark.asyncio
async def test_turn_end_commits_nothing_in_text_mode():
    service, sock = _text_service()
    await service._handle_user_stopped_speaking(SimpleNamespace())
    assert sock.sent == []


@pytest.mark.asyncio
async def test_an_interruption_cancels_the_running_reply():
    service, sock = _text_service()
    await service._on_server_event({"type": "response.created"})
    await service._handle_interruption()
    assert sock.types() == ["response.cancel"]


@pytest.mark.asyncio
async def test_a_second_reply_waits_for_the_first():
    service, sock = _text_service()
    await service._on_server_event({"type": "response.created"})
    await service.send_client_event(events.ResponseCreateEvent())
    assert sock.sent == []
    assert service._held_response_create is not None
