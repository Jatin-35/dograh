"""Sarvam TTS: bulbul:v4-flash support, and a home for agents saved on bulbul:v2.

Our pinned pipecat knows bulbul:v2 and bulbul:v3 only; it refuses any other
model. bulbul:v4-flash (low latency, under 250 ms to first audio, ₹20 per 10K
characters) is registered here with the capabilities Sarvam documents, the same
entry upstream pipecat added, so no pipecat change is needed.

Sarvam has deprecated bulbul:v2: its API answers "Model 'bulbul:v2' has been
deprecated", so an agent still saved with it would speak nothing. Such an agent
now runs on bulbul:v3, with the nearest v3 voice.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.sarvam.tts import TTS_MODEL_CONFIGS, TTSModelConfig

from api.services.configuration.options.sarvam import (
    SARVAM_V2_VOICES,
    SARVAM_V3_VOICES,
    SARVAM_V4_FLASH_DEFAULT_VOICE,
)

V4_FLASH = "bulbul:v4-flash"
V3 = "bulbul:v3"
V2 = "bulbul:v2"

# Sarvam's docs: pace 0.5-2.0; pitch and loudness are bulbul:v2 only;
# temperature is fixed, so none of those three is sent.
V4_FLASH_CONFIG = TTSModelConfig(
    supports_pitch=False,
    supports_loudness=False,
    supports_temperature=False,
    default_sample_rate=24000,
    default_speaker="shubh_enhi_ads",
    pace_range=(0.5, 2.0),
    preprocessing_always_enabled=True,
    # Its own catalogue (<name>_<language>_<style>); Sarvam's API validates it.
    speakers=(),
)


def register_v4_flash() -> None:
    """Teach the pinned pipecat about bulbul:v4-flash (idempotent)."""
    TTS_MODEL_CONFIGS.setdefault(V4_FLASH, V4_FLASH_CONFIG)


register_v4_flash()

# bulbul:v2 voices by gender, for moving a v2 agent to a v3 voice.
_V2_FEMALE = {"anushka", "manisha", "vidya", "arya"}
_V3_FEMALE_DEFAULT = "simran"
_V3_MALE_DEFAULT = "shubh"


def resolve_model_and_voice(model: str | None, voice: str | None) -> tuple[str, str]:
    """The model and voice to synthesize with: any saved config, a working pair.

    - bulbul:v2 (deprecated) becomes bulbul:v3; its voice becomes the nearest
      v3 voice, or stays when v3 has the same name.
    - bulbul:v4-flash takes a v4 persona ID; a v2/v3 short name ("simran",
      "anushka") is not accepted by Sarvam, so it becomes the default Hinglish
      voice. Other IDs (a custom or cloned voice) pass through.
    - Anything else passes through unchanged.
    """
    model = (model or V4_FLASH).strip()
    voice = (voice or "").strip().lower()

    if model == V2:
        new_voice = voice if voice in SARVAM_V3_VOICES else (
            _V3_FEMALE_DEFAULT if (voice in _V2_FEMALE or not voice) else _V3_MALE_DEFAULT
        )
        logger.warning(
            f"Sarvam bulbul:v2 is deprecated and rejected by Sarvam; using bulbul:v3 "
            f"with voice '{new_voice}' (was '{voice or 'default'}')"
        )
        return V3, new_voice

    if model == V4_FLASH:
        if not voice:
            return model, SARVAM_V4_FLASH_DEFAULT_VOICE
        if voice in SARVAM_V3_VOICES or voice in SARVAM_V2_VOICES:
            logger.warning(
                f"Sarvam bulbul:v4-flash needs a persona voice ID like "
                f"'{SARVAM_V4_FLASH_DEFAULT_VOICE}', not '{voice}'; using the default"
            )
            return model, SARVAM_V4_FLASH_DEFAULT_VOICE
        return model, voice

    if not voice:
        config = TTS_MODEL_CONFIGS.get(model)
        return model, config.default_speaker if config else _V3_FEMALE_DEFAULT
    return model, voice
