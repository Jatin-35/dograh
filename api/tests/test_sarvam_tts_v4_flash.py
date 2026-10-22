"""Sarvam bulbul:v4-flash, and agents still saved on the deprecated bulbul:v2.

bulbul:v4-flash is registered on top of the pinned pipecat (which refuses
unknown models). An agent saved on bulbul:v2, which Sarvam's API now rejects,
runs on bulbul:v3 instead of going silent. These tests build the real
SarvamTTSService and check the config message Sarvam would receive.
"""

import json
from types import SimpleNamespace

import pytest
from pipecat.services.sarvam.tts import TTS_MODEL_CONFIGS, SarvamTTSService
from pipecat.transcriptions.language import Language

from api.services.configuration.options.sarvam import (
    SARVAM_TTS_MODELS,
    SARVAM_V4_FLASH_VOICES,
)
from api.services.configuration.registry import (
    ServiceProviders,
    SarvamTTSConfiguration,
)
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.sarvam_tts import resolve_model_and_voice
from api.services.pipecat.service_factory import create_tts_service


def _build(model, voice, speed=1.0, language="hi-IN"):
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.SARVAM.value,
            api_key="test-key",
            model=model,
            voice=voice,
            language=language,
            speed=speed,
        )
    )
    audio = AudioConfig(transport_in_sample_rate=8000, transport_out_sample_rate=8000)
    return create_tts_service(user_config, audio)


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


async def _config_sent(service: SarvamTTSService) -> dict:
    sock = _FakeSocket()
    service._websocket = sock
    await service._send_config()
    return sock.sent[0]["data"]


# ── The settings form ─────────────────────────────────────────────────


def test_new_sarvam_configs_default_to_v4_flash_with_a_hinglish_voice():
    cfg = SarvamTTSConfiguration(api_key="k")
    assert cfg.model == "bulbul:v4-flash"
    assert cfg.voice == "simran_enhi_customer"


def test_v2_is_no_longer_offered_but_a_saved_v2_still_shows_its_voices():
    props = SarvamTTSConfiguration.model_json_schema()["properties"]
    assert props["model"]["examples"] == list(SARVAM_TTS_MODELS)
    assert "bulbul:v2" not in props["model"]["examples"]
    options = props["voice"]["model_options"]
    assert options["bulbul:v4-flash"][0] == "simran_enhi_customer"
    assert "bulbul:v2" in options


def test_v4_voice_ids_have_the_persona_shape():
    assert len(set(SARVAM_V4_FLASH_VOICES)) == len(SARVAM_V4_FLASH_VOICES)
    for v in SARVAM_V4_FLASH_VOICES:
        name, lang, *_ = v.split("_")
        assert v == v.lower() and lang in {"en", "hi", "enhi"}, v


# ── Model and voice resolution ────────────────────────────────────────


@pytest.mark.parametrize("model,voice,want", [
    ("bulbul:v2", "anushka", ("bulbul:v3", "simran")),
    ("bulbul:v2", "vidya", ("bulbul:v3", "simran")),
    ("bulbul:v2", "abhilash", ("bulbul:v3", "shubh")),
    ("bulbul:v2", "", ("bulbul:v3", "simran")),
    ("bulbul:v4-flash", "", ("bulbul:v4-flash", "simran_enhi_customer")),
    ("bulbul:v4-flash", None, ("bulbul:v4-flash", "simran_enhi_customer")),
    ("bulbul:v4-flash", "simran", ("bulbul:v4-flash", "simran_enhi_customer")),
    ("bulbul:v4-flash", "anushka", ("bulbul:v4-flash", "simran_enhi_customer")),
    ("bulbul:v4-flash", " Ritu_Hi_Customer ", ("bulbul:v4-flash", "ritu_hi_customer")),
    ("bulbul:v4-flash", "my-cloned-voice-123", ("bulbul:v4-flash", "my-cloned-voice-123")),
    ("bulbul:v3", "kavya", ("bulbul:v3", "kavya")),
    ("bulbul:v3", "", ("bulbul:v3", "shubh")),
    (None, None, ("bulbul:v4-flash", "simran_enhi_customer")),
])
def test_resolve_model_and_voice(model, voice, want):
    assert resolve_model_and_voice(model, voice) == want


# ── The real service and what Sarvam receives ─────────────────────────


def test_pipecat_knows_v4_flash():
    cfg = TTS_MODEL_CONFIGS["bulbul:v4-flash"]
    assert not (cfg.supports_pitch or cfg.supports_loudness or cfg.supports_temperature)
    assert cfg.pace_range == (0.5, 2.0)


@pytest.mark.asyncio
async def test_v4_flash_service_sends_only_what_v4_accepts():
    service = _build("bulbul:v4-flash", "ritu_hi_customer", speed=1.1)
    assert isinstance(service, SarvamTTSService)
    sent = await _config_sent(service)
    assert sent["model"] == "bulbul:v4-flash"
    assert sent["speaker"] == "ritu_hi_customer"
    assert sent["target_language_code"] == "hi-IN"
    assert sent["pace"] == 1.1
    assert sent["enable_preprocessing"] is True
    assert not {"pitch", "loudness", "temperature"} & set(sent)
    assert "model=bulbul:v4-flash" in service._websocket_url


@pytest.mark.asyncio
async def test_an_agent_saved_on_v2_runs_on_v3_instead_of_going_silent():
    service = _build("bulbul:v2", "anushka")
    sent = await _config_sent(service)
    assert sent["model"] == "bulbul:v3"
    assert sent["speaker"] == "simran"
    assert "model=bulbul:v3" in service._websocket_url


@pytest.mark.asyncio
async def test_v3_is_unchanged():
    service = _build("bulbul:v3", "kavya")
    sent = await _config_sent(service)
    assert (sent["model"], sent["speaker"]) == ("bulbul:v3", "kavya")
    assert not {"pitch", "loudness"} & set(sent)  # v3 never took these


def test_pace_is_clamped_to_v4_range():
    service = _build("bulbul:v4-flash", "ritu_hi_customer", speed=2.0)
    assert service._settings.pace == 2.0


@pytest.mark.parametrize("lang,want", [("en-IN", Language.EN), ("hi-IN", Language.HI)])
def test_language_reaches_v4(lang, want):
    service = _build("bulbul:v4-flash", "simran_enhi_customer", language=lang)
    assert service._settings.language == service.language_to_service_language(want)
