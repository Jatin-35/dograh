"""Shared helpers for tuning pipecat ``TransportParams`` per run mode.

These live outside ``transport_setup.py`` (which is non-telephony only) so
that both the WebRTC factory there and the telephony provider factories
under ``api.services.telephony.providers/<name>/transport.py`` can call
into the same place.
"""

from loguru import logger

# Realtime (speech-to-speech) LLMs don't emit ``TTSStoppedFrame``, so the
# bot-stopped-speaking signal relies on the output-queue-drained fallback.
# The default 3s tail leaves a long gap before the assistant aggregator
# closes its turn; 0.5s keeps the conversation snappy without cutting into
# the bot's own audio (audio chunks arrive far more frequently than this).
REALTIME_BOT_VAD_STOP_SECS = 0.5


def realtime_param_overrides(is_realtime: bool) -> dict:
    """Return kwargs to splat into ``TransportParams`` for the given run mode.

    Currently this only tunes ``bot_vad_stop_secs``; new realtime-specific
    knobs should be added here so each transport stays a thin shim.
    """
    if not is_realtime:
        return {}
    return {"bot_vad_stop_secs": REALTIME_BOT_VAD_STOP_SECS}


def build_audio_in_filter(noise_reduction: str | None):
    """Return a fresh caller-side noise filter for ``noise_reduction``.

    ``"off"`` (or anything unknown) returns None. Filters keep per-stream
    state, so every call gets its own instance.
    """
    if noise_reduction == "rnnoise":
        from api.services.pipecat.native_rnnoise_filter import (
            NativeRNNoiseFilter,
            load_native_rnnoise,
        )

        # The bundled librnnoise is ~3x cheaper per call; pyrnnoise is only the
        # fallback for machines where that Linux build cannot load.
        lib = load_native_rnnoise()
        if lib is not None:
            return NativeRNNoiseFilter(lib)

        from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

        return RNNoiseFilter()
    return None


def apply_noise_reduction(transport, run_configs: dict | None, run_id) -> None:
    """Attach the workflow's noise filter to a freshly created transport.

    Done once here, right after any transport factory returns, instead of in
    every provider's factory. The input transport reads
    ``params.audio_in_filter`` when the pipeline starts, and every pipecat
    transport hands the same params object to its input side, so setting it
    before the pipeline runs is enough.
    """
    mode = (run_configs or {}).get("noise_reduction") or "off"
    audio_filter = build_audio_in_filter(mode)
    if audio_filter is None:
        return
    params = getattr(transport, "_params", None)
    if params is None or not hasattr(params, "audio_in_filter"):
        logger.warning(
            f"[run {run_id}] noise_reduction={mode} requested but "
            f"{type(transport).__name__} exposes no input params; skipping"
        )
        return
    params.audio_in_filter = audio_filter
    logger.info(f"[run {run_id}] noise reduction enabled: {mode}")
