"""Sarvam STT on the pipecat we ship, kept in step with Sarvam's current release.

The pinned pipecat (dograh-hq/pipecat at the v1.5 merge) predates three changes
upstream pipecat has since made to ``SarvamSTTService``. Rather than upgrade
pipecat for the whole app, this subclass carries them, ported from pipecat main
(10 Oct 2026), in the same way ``sarvam_llm.py`` carries the LLM's:

- **saaras:v4**, Sarvam's latest model (pipecat 6b9453a6 / 8bc4079a). It takes
  the same connection parameters as saaras:v3, so it is registered with v3's
  model config.
- **Retired models move up.** Sarvam sunset ``saarika:v2.5`` and
  ``saaras:v2.5`` (pipecat 11d41291). An agent still saved with one runs on
  ``saaras:v3`` (see ``resolve_sarvam_stt_model``), so it keeps working without
  being re-saved.
- **Keyterms** (pipecat 0b7f3c11): up to 50 recognition hints, sent with the
  connection on saaras:v4. Dograh feeds the agent's Dictionary words here.
- **Transcripts are final** (pipecat 4c99ebf2). This service emits exactly one
  transcript per utterance and never an interim one, but the pinned version
  leaves ``finalized`` at False. Turn-stop strategies then wait out a timeout
  meant for partial transcripts: pipecat measured ~970 ms of a ~2.7 s turn on a
  live phone call.
- **No guessed language** (pipecat e4b75550): a transcript whose language Sarvam
  does not name, or names with a code we can't map, carries no language rather
  than Hindi.

Not carried: ``SarvamRealtimeSTTService``, which depends on pipecat's newer
proposed-turn frames. It arrives with the full pipecat upgrade, at which point
this module can be deleted in favour of pipecat's own service.

The Sarvam SDK is unchanged: upstream pins the same ``sarvamai==0.1.28``.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import MODEL_CONFIGS, SarvamSTTService
from pipecat.transcriptions.language import Language

try:
    from sarvamai.core.api_error import ApiError
    from sarvamai.core.events import EventType
except ModuleNotFoundError as e:  # pragma: no cover - same guard as pipecat's
    raise ImportError(f"Missing module: {e}") from e

SARVAM_STT_DEFAULT_MODEL = "saaras:v4"
SARVAM_STT_KEYTERMS_MODEL = "saaras:v4"

# Models Sarvam retired, and what an agent still saved with one now uses.
# saaras:v3 in "transcribe" mode does what saarika:v2.5 did: transcribe in the
# spoken language.
RETIRED_SARVAM_STT_MODELS = {
    "saarika:v2.5": "saaras:v3",
    "saaras:v2.5": "saaras:v3",
}

# Sarvam's documented keyterm limits.
MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 64

# saaras:v4 takes the same connection parameters as saaras:v3. The pinned
# service validates the model against this module-level table, so v4 is added
# to it here.
if SARVAM_STT_DEFAULT_MODEL not in MODEL_CONFIGS:
    MODEL_CONFIGS[SARVAM_STT_DEFAULT_MODEL] = MODEL_CONFIGS["saaras:v3"]

# Sarvam language codes a transcript can carry, mapped to pipecat's Language.
# Codes pipecat has no member for (ne-IN, sat-IN, ...) map to no language.
_SARVAM_LANGUAGES = {
    "as-IN": Language.AS_IN,
    "bn-IN": Language.BN_IN,
    "en-IN": Language.EN_IN,
    "en-US": Language.EN_US,
    "gu-IN": Language.GU_IN,
    "hi-IN": Language.HI_IN,
    "kn-IN": Language.KN_IN,
    "kok-IN": Language.KOK_IN,
    "mai-IN": Language.MAI_IN,
    "ml-IN": Language.ML_IN,
    "mr-IN": Language.MR_IN,
    "od-IN": Language.OR_IN,
    "pa-IN": Language.PA_IN,
    "sd-IN": Language.SD_IN,
    "ta-IN": Language.TA_IN,
    "te-IN": Language.TE_IN,
    "ur-IN": Language.UR_IN,
}


def resolve_sarvam_stt_model(model: str | None) -> str:
    """The model to run: a retired one moves to its replacement; unset is the default."""
    if not model:
        return SARVAM_STT_DEFAULT_MODEL
    replacement = RETIRED_SARVAM_STT_MODELS.get(model)
    if replacement:
        logger.info(
            f"Sarvam STT model {model!r} has been retired by Sarvam; using {replacement!r}."
        )
        return replacement
    return model


def clean_keyterms(terms: list[str] | None) -> list[str] | None:
    """Keyterms in the shape Sarvam accepts: trimmed, unique, at most 50 x 64 chars."""
    if not terms:
        return None
    cleaned: list[str] = []
    for term in terms:
        term = (term or "").strip()[:MAX_KEYTERM_CHARS].strip()
        if term and term not in cleaned:
            cleaned.append(term)
    if len(cleaned) > MAX_KEYTERMS:
        logger.warning(
            f"Sarvam STT takes at most {MAX_KEYTERMS} keyterms; "
            f"using the first {MAX_KEYTERMS} of {len(cleaned)}."
        )
        cleaned = cleaned[:MAX_KEYTERMS]
    return cleaned or None


class DograhSarvamSTTService(SarvamSTTService):
    """``SarvamSTTService`` with saaras:v4, keyterms and finalized transcripts."""

    def __init__(self, *, keyterms: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        model = self._settings.model
        terms = clean_keyterms(keyterms)
        if terms and model != SARVAM_STT_KEYTERMS_MODEL:
            logger.debug(
                f"Sarvam STT: keyterms are only sent with {SARVAM_STT_KEYTERMS_MODEL}; "
                f"{model} runs without them."
            )
            terms = None
        self._keyterms = terms
        self._unmapped_language_codes_warned: set[str] = set()

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        # One transcript per utterance and no interim frames: every transcript
        # this service produces is final.
        if isinstance(frame, TranscriptionFrame):
            frame.finalized = True
        await super().push_frame(frame, direction)

    def _map_language_code_to_enum(self, language_code: str) -> Language | None:
        language = _SARVAM_LANGUAGES.get(language_code)
        if language is None and language_code not in self._unmapped_language_codes_warned:
            self._unmapped_language_codes_warned.add(language_code)
            logger.debug(f"Sarvam STT: no pipecat language for {language_code!r}; leaving it unset.")
        return language

    def _connect_kwargs(self) -> dict[str, Any]:
        """Connection parameters, as the pinned ``_connect`` builds them."""
        settings = self._settings
        kwargs: dict[str, Any] = {
            "model": settings.model,
            "sample_rate": str(self.sample_rate),
        }
        # Honour flush() on Pipecat's user-stopped-speaking when Sarvam's own
        # VAD isn't driving turns.
        if not settings.vad_signals:
            kwargs["flush_signal"] = "true"
        if settings.vad_signals is not None:
            kwargs["vad_signals"] = "true" if settings.vad_signals else "false"
        if settings.high_vad_sensitivity is not None:
            kwargs["high_vad_sensitivity"] = "true" if settings.high_vad_sensitivity else "false"
        if self._config.supports_vad_params:
            for name in (
                "positive_speech_threshold",
                "negative_speech_threshold",
                "min_speech_frames",
                "first_turn_min_speech_frames",
                "negative_frames_count",
                "negative_frames_window",
                "start_speech_volume_threshold",
                "interrupt_min_speech_frames",
                "pre_speech_pad_frames",
                "num_initial_ignored_frames",
            ):
                value = getattr(settings, name, None)
                if value is not None:
                    kwargs[name] = str(value)
        language = self._get_language_string()
        if language is not None:
            kwargs["language_code"] = language
        if self._config.supports_mode and self._mode is not None:
            kwargs["mode"] = self._mode
        return kwargs

    async def _connect(self):
        """Connect with the keyterms on the query string, as pipecat main does.

        The SDK's ``connect`` has no keyterms argument, so they travel in
        ``request_options.additional_query_parameters``. Without keyterms this
        is the pinned connect path (the retired models' prompt and translate
        endpoint no longer apply).
        """
        logger.debug("Connecting to Sarvam")
        try:
            request_options: dict[str, Any] = {"additional_headers": self._sdk_headers}
            if self._keyterms:
                request_options["additional_query_parameters"] = {
                    "keyterms": json.dumps(self._keyterms)
                }
            self._websocket_context = self._sarvam_client.speech_to_text_streaming.connect(
                **self._connect_kwargs(), request_options=request_options
            )
            self._socket_client = await self._websocket_context.__aenter__()

            def _message_handler(message):
                self.create_task(self._handle_message(message))

            self._socket_client.on(EventType.MESSAGE, _message_handler)
            self._receive_task = self.create_task(self._receive_task_handler())
            self._create_keepalive_task()
            logger.info(
                f"Connected to Sarvam successfully ({self._settings.model}"
                f"{f', {len(self._keyterms)} keyterms' if self._keyterms else ''})"
            )
        except ApiError as e:
            self._socket_client = None
            self._websocket_context = None
            await self.push_error(error_msg=f"Sarvam API error: {e}", exception=e)
        except Exception as e:
            self._socket_client = None
            self._websocket_context = None
            await self.push_error(error_msg=f"Failed to connect to Sarvam: {e}", exception=e)


__all__ = [
    "DograhSarvamSTTService",
    "RETIRED_SARVAM_STT_MODELS",
    "SARVAM_STT_DEFAULT_MODEL",
    "clean_keyterms",
    "resolve_sarvam_stt_model",
]
