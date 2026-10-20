"""Shared helper for building audio output mixers used by telephony transports."""

import os

from loguru import logger

from api.constants import APP_ROOT_DIR
from api.services.pipecat.ambient_sound import (
    PLAY_WHOLE_CALL,
    GatedSoundfileMixer,
    normalize_ambient_config,
)
from api.services.pipecat.audio_file_cache import get_cached_ambient_noise_path
from pipecat.audio.mixers.silence_mixer import SilenceAudioMixer
from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

BUILT_IN_SOUNDS = {
    "office": "office-ambience-{rate}-mono.wav",
    "typing": "typing-{rate}-mono.wav",
}

librnnoise_path = os.path.normpath(
    str(APP_ROOT_DIR / "native" / "rnnoise" / "librnnoise.so")
)


async def build_audio_out_mixer(
    audio_out_sample_rate: int,
    ambient_noise_config: dict | None,
):
    """Build the audio output mixer based on the ambient noise configuration.

    Returns a ``SoundfileMixer`` when ambient noise is enabled, or a
    ``SilenceAudioMixer`` otherwise.  Supports custom user-uploaded audio
    files via the ``storage_key`` / ``storage_backend`` fields in the config,
    and the built-in ``office`` and ``typing`` sounds via ``sound``.

    A ``play_mode`` other than ``whole_call`` returns a
    ``GatedSoundfileMixer`` that starts silent; ``attach_ambient_sound_gate``
    then turns it on at the chosen moments of the call.
    """
    if not ambient_noise_config or not ambient_noise_config.get("enabled", False):
        return SilenceAudioMixer()

    ambient_noise_config = normalize_ambient_config(ambient_noise_config)
    volume = ambient_noise_config["volume"]
    gated = ambient_noise_config["play_mode"] != PLAY_WHOLE_CALL
    mixer_cls = GatedSoundfileMixer if gated else SoundfileMixer

    storage_key = ambient_noise_config.get("storage_key")
    storage_backend = ambient_noise_config.get("storage_backend")

    if storage_key and storage_backend:
        cached_path = await get_cached_ambient_noise_path(
            storage_key, storage_backend, audio_out_sample_rate
        )
        if cached_path:
            return mixer_cls(
                sound_files={"custom": cached_path},
                default_sound="custom",
                volume=volume,
            )
        logger.warning("Custom ambient noise file unavailable, falling back to default")

    sound = ambient_noise_config["sound"]
    return mixer_cls(
        sound_files={
            sound: APP_ROOT_DIR
            / "assets"
            / BUILT_IN_SOUNDS[sound].format(rate=audio_out_sample_rate)
        },
        default_sound=sound,
        volume=volume,
    )
