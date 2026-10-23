"""Inworld Realtime with a custom transcriber and/or a custom voice.

``stt_source`` and ``tts_source`` are each ``inworld`` or ``custom``, so all
four combinations work: Inworld hears or a custom transcriber's text comes
in; Inworld speaks or its text goes to a custom voice. The Inworld Realtime
session stays the LLM throughout. Covers the saved configuration, old saved
values, the pipeline layout and the wrapper in each combination. No network:
every socket here is fake.
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
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.inworld.realtime import events

from api.schemas.ai_model_configuration import (
    EffectiveAIModelConfiguration,
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.registry import InworldRealtimeLLMConfiguration
from api.services.pipecat.pipeline_builder import build_realtime_pipeline
from api.services.pipecat.realtime.inworld_realtime import (
    DograhInworldRealtimeLLMService,
)
from api.services.pipecat.run_pipeline import (
    realtime_uses_custom_stt,
    realtime_uses_custom_tts,
)
from api.services.pipecat.service_factory import create_realtime_llm_service

STT = {"provider": "deepgram", "api_key": "dg-k", "model": "nova-3-general"}
TTS = {"provider": "elevenlabs", "api_key": "el-k", "model": "eleven_flash_v2_5", "voice": "Rachel"}


def _v2(realtime_extra=None, stt=True, tts=True):
    realtime = {
        "realtime": {"provider": "inworld_realtime", "api_key": "inworld-real-secret", **(realtime_extra or {})},
        "llm": {"provider": "openai", "api_key": "llm-k", "model": "gpt-4.1"},
    }
    if stt:
        realtime["stt"] = STT
    if tts:
        realtime["tts"] = TTS
    return OrganizationAIModelConfigurationV2.model_validate(
        {"version": 2, "mode": "byok", "byok": {"mode": "realtime", "realtime": realtime}}
    )


# ── Saved configuration ───────────────────────────────────────────────


def test_a_realtime_config_carries_its_custom_transcriber_and_voice():
    effective = compile_ai_model_configuration_v2(_v2({"stt_source": "custom", "tts_source": "custom"}))
    assert effective.is_realtime is True
    assert effective.stt.provider == "deepgram"
    assert effective.tts.provider == "elevenlabs"


def test_realtime_configs_without_them_still_work():
    effective = compile_ai_model_configuration_v2(_v2(stt=False, tts=False))
    assert effective.stt is None and effective.tts is None


def test_botrix_managed_stt_is_refused_inside_byok_realtime():
    with pytest.raises(ValueError):
        OrganizationAIModelConfigurationV2.model_validate({
            "version": 2, "mode": "byok", "byok": {"mode": "realtime", "realtime": {
                "realtime": {"provider": "inworld_realtime", "api_key": "k"},
                "llm": {"provider": "openai", "api_key": "x", "model": "gpt-4.1"},
                "stt": {"provider": "dograh", "api_key": "x", "model": "default"},
            }},
        })


def test_both_sources_default_to_inworld():
    cfg = InworldRealtimeLLMConfiguration(api_key="k")
    assert (cfg.stt_source, cfg.tts_source) == ("inworld", "inworld")


def test_agents_saved_with_the_combined_switch_keep_their_custom_providers():
    cfg = InworldRealtimeLLMConfiguration(api_key="k", stt_tts_source="dograh")
    assert (cfg.stt_source, cfg.tts_source) == ("custom", "custom")
    cfg = InworldRealtimeLLMConfiguration(api_key="k", stt_tts_source="inworld")
    assert (cfg.stt_source, cfg.tts_source) == ("inworld", "inworld")
    assert "stt_tts_source" not in InworldRealtimeLLMConfiguration.model_json_schema()["properties"]


def test_old_saved_value_survives_the_full_saved_config_too():
    effective = compile_ai_model_configuration_v2(_v2({"stt_tts_source": "dograh"}))
    assert realtime_uses_custom_stt(effective) and realtime_uses_custom_tts(effective)


@pytest.mark.parametrize("stt,tts", [("custom", "custom"), ("custom", "inworld"), ("inworld", "custom"), ("inworld", "inworld")])
def test_each_side_is_read_on_its_own(stt, tts):
    cfg = compile_ai_model_configuration_v2(_v2({"stt_source": stt, "tts_source": tts}))
    assert realtime_uses_custom_stt(cfg) is (stt == "custom")
    assert realtime_uses_custom_tts(cfg) is (tts == "custom")


def test_non_realtime_and_other_providers_never_count():
    assert realtime_uses_custom_stt(EffectiveAIModelConfiguration()) is False
    other = SimpleNamespace(is_realtime=True, realtime=SimpleNamespace(provider="openai_realtime", stt_source="custom", tts_source="custom"))
    assert realtime_uses_custom_stt(other) is False and realtime_uses_custom_tts(other) is False


def test_each_inworld_setting_hides_only_with_its_own_side_custom():
    props = InworldRealtimeLLMConfiguration.model_json_schema()["properties"]
    for field in ("stt_model", "expected_words", "noise_reduction", "turn_detection", "eagerness", "local_min_words"):
        assert props[field]["visible_when"]["stt_source"] == "inworld", field
        assert "tts_source" not in props[field]["visible_when"], field
    for field in ("voice", "tts_model", "speed", "delivery_mode", "audio_start"):
        assert props[field]["visible_when"] == {"tts_source": "inworld"}, field
    for field in ("model", "temperature", "language", "stt_source", "tts_source"):
        assert "visible_when" not in props[field], field


def test_no_dograh_wording_in_the_inworld_form():
    text = json.dumps(InworldRealtimeLLMConfiguration.model_json_schema())
    assert "dograh" not in text.lower()
    props = InworldRealtimeLLMConfiguration.model_json_schema()["properties"]
    assert props["stt_source"]["enum"] == props["tts_source"]["enum"] == ["inworld", "custom"]


# ── Pipeline layout ───────────────────────────────────────────────────


class _Stub(FrameProcessor):
    def __init__(self, label):
        super().__init__(name=label)


def _layout(stt=None, tts=None):
    transport = SimpleNamespace(input=lambda: _Stub("input"), output=lambda: _Stub("output"))
    pipeline = build_realtime_pipeline(
        transport, _Stub("inworld"), _Stub("recorder"), _Stub("user_agg"), _Stub("assistant_agg"),
        _Stub("engine_cb"), _Stub("metrics"), stt=stt, tts=tts,
    )
    return [p.name for p in pipeline.processors if p.name in {
        "input", "stt", "user_agg", "inworld", "engine_cb", "tts", "output", "recorder", "assistant_agg", "metrics"}]


def test_pipeline_with_no_custom_parts_is_unchanged():
    assert _layout() == ["input", "user_agg", "inworld", "engine_cb", "output", "recorder", "assistant_agg", "metrics"]


def test_custom_transcriber_sits_before_the_turn_rules():
    assert _layout(stt=_Stub("stt"))[:3] == ["input", "stt", "user_agg"]


def test_custom_voice_sits_after_inworld_before_the_output():
    assert _layout(tts=_Stub("tts"))[2:6] == ["inworld", "engine_cb", "tts", "output"]


# ── The wrapper ───────────────────────────────────────────────────────


class _FakeSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    def types(self):
        return [m["type"] for m in self.sent]


def _service(stt_source="custom", tts_source="custom", **kwargs):
    service = DograhInworldRealtimeLLMService(
        api_key="k", stt_source=stt_source, tts_source=tts_source, **kwargs
    )
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


def _audio(n=20):
    return [InputAudioRawFrame(audio=b"\x01\x00" * 160, sample_rate=8000, num_channels=1) for _ in range(n)]


@pytest.mark.parametrize("stt,tts,text_in,text_out", [
    ("custom", "custom", True, True), ("custom", "inworld", True, False),
    ("inworld", "custom", False, True), ("inworld", "inworld", False, False),
])
def test_factory_sets_each_side(stt, tts, text_in, text_out):
    cfg = compile_ai_model_configuration_v2(_v2({"stt_source": stt, "tts_source": tts, "turn_detection": "local"}))
    service = create_realtime_llm_service(cfg, audio_config=SimpleNamespace())
    assert (service._text_in, service._text_out) == (text_in, text_out)
    assert service._settings.session_properties.output_modalities == (["text"] if text_out else ["audio", "text"])
    # With a custom transcriber the call's own turn rules decide turns.
    assert service._local_turns is (not text_in)
    assert service._is_manual_turn_detection() is True


@pytest.mark.asyncio
async def test_custom_both_session_asks_for_text_with_turn_detection_off():
    service, sock = _service(speech_threshold=0.6)
    await service._send_session_update()
    session = sock.sent[0]["session"]
    assert session["output_modalities"] == ["text"]
    assert session["audio"]["input"]["turn_detection"] is None
    assert "stt" not in session["providerData"]


@pytest.mark.asyncio
async def test_a_custom_transcriber_means_no_caller_audio_reaches_inworld():
    service, sock = _service(tts_source="inworld")
    service._llm_needs_conversation_setup = False
    for frame in _audio():
        await service._send_user_audio(frame)
    assert sock.sent == []


@pytest.mark.asyncio
async def test_first_turn_sets_up_the_conversation_and_asks_for_a_reply():
    service, sock = _service()
    _ready(service)
    context = LLMContext([
        {"role": "system", "content": "You are Aastha."},
        {"role": "assistant", "content": "Namaskar, main Aastha."},
        {"role": "user", "content": "Haan ji boliye"},
    ])
    await service._handle_context(context)
    assert "session.update" in sock.types()
    assert sock.types()[-1] == "response.create"
    assert sock.sent[-1]["response"]["modalities"] == ["text"]
    texts = [m["item"]["content"][0].get("text") for m in sock.sent if m["type"] == "conversation.item.create"]
    # Pipecat's Inworld adapter packs the history so far into one message
    # (as for every realtime agent); the caller's words travel inside it.
    assert any("Haan ji boliye" in t for t in texts)


@pytest.mark.asyncio
async def test_later_turns_send_only_the_new_user_text():
    service, sock = _service()
    _ready(service)
    context = LLMContext([{"role": "system", "content": "S"}, {"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "assistant", "content": "Ji, bataiye."})
    context.add_message({"role": "user", "content": "Main Jaipur se hoon"})
    await service._handle_context(context)
    items = [m["item"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert [(i["role"], i["content"][0]["text"]) for i in items] == [("user", "Main Jaipur se hoon")]
    assert sock.types()[-1] == "response.create"


@pytest.mark.asyncio
async def test_custom_transcriber_with_inworlds_voice_asks_for_spoken_replies():
    service, sock = _service(tts_source="inworld")
    _ready(service)
    context = LLMContext([{"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "user", "content": "doosra"})
    await service._handle_context(context)
    assert sock.sent[-1]["type"] == "response.create"
    assert sock.sent[-1]["response"]["modalities"] == ["audio", "text"]


@pytest.mark.asyncio
async def test_empty_turn_recovery_prompt_reaches_inworld():
    service, sock = _service()
    _ready(service)
    context = LLMContext([{"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "system", "content": "Ask the caller to repeat."})
    await service._handle_context(context)
    (item,) = [m["item"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert item["role"] == "system" and item["content"][0]["text"] == "Ask the caller to repeat."


@pytest.mark.asyncio
async def test_a_context_frame_with_nothing_new_asks_for_nothing():
    service, sock = _service()
    _ready(service)
    context = LLMContext([{"role": "user", "content": "pehla"}])
    await service._handle_context(context)
    sock.sent.clear()
    context.add_message({"role": "assistant", "content": "Ji"})
    await service._handle_context(context)
    assert "response.create" not in sock.types()


@pytest.mark.asyncio
async def test_before_the_session_is_ready_the_reply_waits_for_it():
    service, sock = _service()
    await service._handle_context(LLMContext([{"role": "user", "content": "hello"}]))
    assert sock.sent == []
    assert service._run_llm_when_api_session_ready is True


@pytest.mark.asyncio
async def test_text_deltas_go_to_a_custom_voice():
    service, _ = _service(stt_source="inworld")
    await service._on_server_event({"type": "response.output_text.delta", "delta": "Ji, "})
    await service._on_server_event({"type": "response.text.delta", "delta": "bilkul."})
    frames = [c.args[0] for c in service.push_frame.await_args_list]
    assert [f.text for f in frames if isinstance(f, LLMTextFrame)] == ["Ji, ", "bilkul."]


@pytest.mark.asyncio
async def test_text_deltas_are_ignored_when_inworld_speaks():
    service, _ = _service(stt_source="custom", tts_source="inworld")
    await service._on_server_event({"type": "response.output_text.delta", "delta": "Ji"})
    service.push_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_voice_speaks_the_greeting_word_for_word():
    service, sock = _service()
    frame = TTSSpeakFrame("Namaskar, main Vectus se Aastha.", append_to_context=True)
    await service.process_frame(frame, FrameDirection.DOWNSTREAM)
    service.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)
    assert sock.sent == []


@pytest.mark.asyncio
async def test_a_transition_line_is_spoken_and_inworld_is_told():
    service, sock = _service()
    _ready(service)
    service._llm_needs_conversation_setup = False
    service._handled_initial_context = True
    frame = TTSSpeakFrame("Ek minute, main check karti hoon.", append_to_context=False)
    await service.process_frame(frame, FrameDirection.DOWNSTREAM)
    service.push_frame.assert_awaited_with(frame, FrameDirection.DOWNSTREAM)
    (note,) = [m["item"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert note["role"] == "system" and "Ek minute, main check karti hoon." in note["content"][0]["text"]
    assert "response.create" not in sock.types()


@pytest.mark.asyncio
async def test_custom_transcriber_with_inworlds_voice_has_inworld_say_the_greeting():
    service, sock = _service(stt_source="custom", tts_source="inworld")
    _ready(service)
    service._context = LLMContext([{"role": "system", "content": "S"}])
    frame = TTSSpeakFrame("Namaskar, main Aastha.", append_to_context=True)
    await service.process_frame(frame, FrameDirection.DOWNSTREAM)
    # Not passed on (no custom voice): Inworld is asked to say it.
    assert frame not in [c.args[0] for c in service.push_frame.await_args_list]
    prompts = [m["item"]["content"][0]["text"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert any(p.endswith('"Namaskar, main Aastha."') for p in prompts)
    assert sock.sent[-1]["type"] == "response.create"


# Inworld hears the caller, a custom voice speaks: the greeting goes to the
# voice, so the conversation must be set up then, or caller audio is dropped.


@pytest.mark.asyncio
async def test_inworld_ears_custom_voice_sets_up_on_the_greeting_and_then_hears():
    service, sock = _service(stt_source="inworld", tts_source="custom")
    _ready(service)
    service._context = LLMContext([{"role": "system", "content": "You are Aastha."}])
    for frame in _audio(5):
        await service._send_user_audio(frame)
    assert "input_audio_buffer.append" not in sock.types()  # not set up yet

    greeting = TTSSpeakFrame("Namaskar, main Aastha.", append_to_context=True)
    await service.process_frame(greeting, FrameDirection.DOWNSTREAM)
    service.push_frame.assert_any_await(greeting, FrameDirection.DOWNSTREAM)
    assert service._llm_needs_conversation_setup is False
    notes = [m["item"]["content"][0]["text"] for m in sock.sent if m["type"] == "conversation.item.create"]
    assert any("Namaskar, main Aastha." in n for n in notes)
    assert "response.create" not in sock.types()

    for frame in _audio(10):
        await service._send_user_audio(frame)
    assert "input_audio_buffer.append" in sock.types()


@pytest.mark.asyncio
async def test_inworld_ears_custom_voice_waits_for_the_session_before_setting_up():
    service, sock = _service(stt_source="inworld", tts_source="custom")
    service._context = LLMContext([{"role": "system", "content": "You are Aastha."}])
    await service.process_frame(TTSSpeakFrame("Namaskar.", append_to_context=True), FrameDirection.DOWNSTREAM)
    assert sock.sent == [] and service._pending_setup_note == "Namaskar."
    await service._handle_evt_session_updated(SimpleNamespace())
    assert service._llm_needs_conversation_setup is False
    assert service._pending_setup_note is None
    assert any("Namaskar." in m["item"]["content"][0]["text"] for m in sock.sent if m["type"] == "conversation.item.create")


@pytest.mark.asyncio
async def test_inworld_ears_custom_voice_keeps_inworlds_turn_detection():
    service, sock = _service(stt_source="inworld", tts_source="custom")
    await service._send_session_update()
    session = sock.sent[0]["session"]
    assert session["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert session["output_modalities"] == ["text"]


@pytest.mark.asyncio
async def test_turn_end_commits_nothing_with_a_custom_transcriber():
    service, sock = _service()
    await service._handle_user_stopped_speaking(SimpleNamespace())
    assert sock.sent == []


@pytest.mark.asyncio
async def test_an_interruption_cancels_the_running_reply():
    service, sock = _service()
    await service._on_server_event({"type": "response.created"})
    await service._handle_interruption()
    assert sock.types() == ["response.cancel"]


@pytest.mark.asyncio
async def test_a_second_reply_waits_for_the_first():
    service, sock = _service()
    await service._on_server_event({"type": "response.created"})
    await service.send_client_event(events.ResponseCreateEvent())
    assert sock.sent == [] and service._held_response_create is not None


# ── Saving with masked keys ───────────────────────────────────────────


def test_saving_again_with_masked_keys_keeps_the_real_custom_keys():
    """The settings page sends keys back masked; a changed voice must still save."""
    from api.services.configuration.ai_model_configuration import (
        check_for_masked_keys_in_ai_model_configuration_v2,
        mask_ai_model_configuration_v2,
        merge_ai_model_configuration_v2_secrets,
    )

    existing = _v2({"stt_source": "custom", "tts_source": "custom"})
    shown = mask_ai_model_configuration_v2(existing)  # what the page receives
    realtime = shown["byok"]["realtime"]
    assert realtime["stt"]["api_key"] != STT["api_key"]
    assert realtime["tts"]["api_key"] != TTS["api_key"]

    realtime["tts"]["voice"] = "Adam"  # the change the caller made
    incoming = OrganizationAIModelConfigurationV2.model_validate(shown)

    merged = merge_ai_model_configuration_v2_secrets(incoming, existing)

    check_for_masked_keys_in_ai_model_configuration_v2(merged)  # no "appears to be masked"
    saved = merged.byok.realtime
    assert saved.stt.api_key == STT["api_key"]
    assert saved.tts.api_key == TTS["api_key"]
    assert saved.tts.voice == "Adam"
    assert saved.realtime.api_key == "inworld-real-secret"
