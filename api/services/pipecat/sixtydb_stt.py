"""60db real-time speech-to-text (https://docs.60db.ai/websocket-api/stt).

The connection is a handshake before it is a stream. 60db's proxy checks the
key in the URL and answers ``connection_established``; we send our ``start``
settings; audio is accepted only after ``session_started``. Audio goes as raw
binary frames of 8 kHz mu-law in 60 ms chunks, the telephony path 60db
documents and tests most.

Turns are ours. 60db's own end-of-speech wait is at least a second (two with
its turn detector), so when the local VAD hears the caller stop we send
``finalize``, which closes the turn at once and returns its transcript. 60db
never revises a final, so each one is pushed as it arrives.

60db withholds a final that is short and unsure (confidence under 0.55, at most
four words and 14 characters) and sends it empty. On a phone line that can be a
quiet "haan" or "ji", and a bot that misses it waits for an answer it was
given. With ``keep_short_answers`` on, the last interim heard for that stretch
of audio stands in for the withheld final.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Iterable, Optional
from urllib.parse import quote

from loguru import logger

from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from api.services.pipecat.provider_rejection import (
    PROVIDER_PAUSE_STATUSES,
    ProviderRejectionAwareWebsocket,
    provider_http_status,
)

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    raise Exception(f"Missing module: {e}") from e

SIXTYDB_STT_URL = "wss://api.60db.ai/ws/stt"
SIXTYDB_STT_MODEL = "60db-stt-v01"
SIXTYDB_AUDIO_RATE = 8000  # mu-law is accepted only at 8 kHz
SIXTYDB_MAX_LANGUAGES = 5
SIXTYDB_MAX_KEYWORDS = 100

_CHUNK_BYTES = 480  # 60 ms of 8 kHz mu-law, 60db's recommended chunk
_HANDSHAKE_TIMEOUT_S = 10.0
_WITHHELD = "low_confidence_transcript"


class SixtyDBError(Exception):
    """A 60db refusal or failure, with its HTTP status when 60db gave one."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def clean_keywords(terms: Optional[Iterable[str]]) -> list[str]:
    """The agent's dictionary words, trimmed and de-duplicated, at most 100."""
    cleaned: list[str] = []
    for term in terms or []:
        term = (term or "").strip()
        if term and term not in cleaned:
            cleaned.append(term)
    if len(cleaned) > SIXTYDB_MAX_KEYWORDS:
        logger.warning(
            f"60db STT: using the first {SIXTYDB_MAX_KEYWORDS} of {len(cleaned)} dictionary words."
        )
        cleaned = cleaned[:SIXTYDB_MAX_KEYWORDS]
    return cleaned


def _language(code: Any) -> Optional[Language]:
    try:
        return Language(str(code).lower()) if code else None
    except ValueError:
        return None


def _error_text(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("error")
    return str(error or event.get("message") or event.get("error_code") or "unknown error")


def _error_status(event: dict[str, Any]) -> Optional[int]:
    for key in ("status", "status_code", "code"):
        value = event.get(key)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


class SixtyDBSTTService(ProviderRejectionAwareWebsocket, WebsocketSTTService):
    """60db streaming transcription, with turns ended by the local VAD."""

    def __init__(
        self,
        *,
        api_key: str,
        languages: Optional[Iterable[str]] = ("hi", "en"),
        keywords: Optional[Iterable[str]] = None,
        audio_enhancement: bool = True,
        keep_short_answers: bool = True,
        url: str = SIXTYDB_STT_URL,
        **kwargs,
    ):
        """Initialize the service.

        Args:
            api_key: 60db API key, sent as the ``apiKey`` query parameter.
            languages: Language codes callers speak, at most five; empty
                detects any language.
            keywords: Words likely to be said (the agent's dictionary); 60db
                corrects near-miss spellings of them.
            audio_enhancement: 60db's noise and level clean-up, meant for phone audio.
            keep_short_answers: Use the interim heard for a final 60db withheld
                as too short and unsure.
            url: Streaming endpoint.
            **kwargs: Passed to the parent service.
        """
        if not api_key or not str(api_key).strip():
            raise ValueError("60db API key is required and cannot be empty")
        kwargs.setdefault("ttfs_p99_latency", 1.0)
        super().__init__(
            settings=STTSettings(model=SIXTYDB_STT_MODEL, language=None),
            **kwargs,
        )
        self._api_key = str(api_key).strip()
        self._url = url
        self._languages = list(languages or [])[:SIXTYDB_MAX_LANGUAGES]
        self._keywords = clean_keywords(keywords)
        self._audio_enhancement = audio_enhancement
        self._keep_short_answers = keep_short_answers

        self._resampler = create_stream_resampler()
        self._pending_audio = bytearray()
        self._session_ready = False
        self._receive_task: Optional[asyncio.Task] = None
        self._rejected_status: Optional[int] = None
        # utterance_id -> latest interim text, until that stretch's final arrives.
        self._interims: dict[Any, str] = {}
        self._seen_finals: set[str] = set()

    def can_generate_metrics(self) -> bool:
        return True

    def _redact(self, text: str) -> str:
        """Never let the key, which travels in the URL, reach a log or an error."""
        text = str(text)
        for secret in {self._api_key, quote(self._api_key, safe="")}:
            if secret:
                text = text.replace(secret, "***")
        return text

    def _start_message(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "encoding": "mulaw",
            "sample_rate": SIXTYDB_AUDIO_RATE,
            # Our VAD ends the turn with finalize; 60db's own endpointing is
            # only the fallback, so keep it from splitting sentences.
            "utterance_end_ms": 1000,
            "early_final": False,
            "turn_detector": False,
            # Interims feed interruption checks and the short-answer fallback.
            "interim_results_frequency": 300,
            "audio_enhancement": "on" if self._audio_enhancement else "off",
        }
        if self._keep_short_answers:
            # 60db's default waits 600 ms of voice before a first interim; a
            # short "haan" needs one sooner to be kept.
            config["interim_min_voiced_ms"] = 200
        message: dict[str, Any] = {
            "type": "start",
            "languages": self._languages or None,
            "config": config,
        }
        if self._keywords:
            message["keywords"] = self._keywords
        return message

    # ── Connection ───────────────────────────────────────────────────

    async def start(self, frame):
        await super().start(frame)
        await self._connect()

    async def _connect(self):
        await self._connect_websocket()
        await super()._connect()
        # The receive loop starts even if this first connect failed (a blip at
        # call start): it finds no socket and reconnects with backoff.
        if not self._receive_task:
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        if self._websocket and self._websocket.state is State.OPEN:
            return
        self._session_ready = False
        self._pending_audio.clear()
        self._interims.clear()
        try:
            self._websocket = await websocket_connect(
                f"{self._url}?apiKey={quote(self._api_key, safe='')}", max_size=None
            )
            await asyncio.wait_for(self._open_session(self._websocket), _HANDSHAKE_TIMEOUT_S)
            self._session_ready = True
            await self._call_event_handler("on_connected")
        except Exception as e:
            websocket, self._websocket = self._websocket, None
            if websocket is not None:
                try:
                    await websocket.close()
                except Exception:
                    pass
            message = self._redact(f"Unable to connect to 60db STT: {e}")
            status = provider_http_status(e, message)
            if status in PROVIDER_PAUSE_STATUSES:
                # Another connection would be refused the same way.
                self._rejected_status = status
            await self.push_error(error_msg=message, exception=SixtyDBError(message, status))
            await self._call_event_handler("on_connection_error", message)

    async def _open_session(self, websocket):
        """Run 60db's handshake up to ``session_started``."""
        async for raw in websocket:
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "connection_established" or event.get("connection_established"):
                await websocket.send(json.dumps(self._start_message()))
            elif kind == "session_started":
                ignored = event.get("ignored_config_keys") or []
                logger.info(
                    f"{self} 60db session started ({event.get('model')}, "
                    f"languages={event.get('languages')}, {len(self._keywords)} keywords"
                    f"{f', ignored {ignored}' if ignored else ''})"
                )
                return
            elif kind == "error":
                raise SixtyDBError(_error_text(event), _error_status(event))
        raise SixtyDBError(
            f"60db closed the connection before the session started "
            f"(code {websocket.close_code}: {websocket.close_reason or 'no reason'})"
        )

    async def _disconnect_websocket(self):
        websocket, self._websocket = self._websocket, None
        self._session_ready = False
        self._pending_audio.clear()
        try:
            if websocket is not None and websocket.state is State.OPEN:
                try:
                    await websocket.send(json.dumps({"type": "stop"}))
                except Exception:
                    pass
                await websocket.close()
        except Exception as e:
            logger.warning(f"{self} error closing websocket: {self._redact(e)}")
        finally:
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("Websocket not connected")

    async def _maybe_try_reconnect(self, error_message, report_error, error=None):
        if self._rejected_status is not None:
            return False
        return await super()._maybe_try_reconnect(error_message, report_error, error)

    # ── Audio in ─────────────────────────────────────────────────────

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        websocket = self._websocket
        if websocket is not None and self._session_ready:
            try:
                ulaw = await pcm_to_ulaw(audio, self.sample_rate, SIXTYDB_AUDIO_RATE, self._resampler)
                self._pending_audio.extend(ulaw)
                while len(self._pending_audio) >= _CHUNK_BYTES:
                    chunk = bytes(self._pending_audio[:_CHUNK_BYTES])
                    del self._pending_audio[:_CHUNK_BYTES]
                    await websocket.send(chunk)
            except Exception as e:
                # The receive loop sees the dead socket and reconnects.
                logger.debug(f"{self} could not send audio: {self._redact(e)}")
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._finalize_turn()

    async def _finalize_turn(self):
        """The caller stopped: send what is buffered and close the turn now."""
        websocket = self._websocket
        if websocket is None or not self._session_ready:
            return
        try:
            if self._pending_audio:
                await websocket.send(bytes(self._pending_audio))
                self._pending_audio.clear()
            await websocket.send(json.dumps({"type": "finalize"}))
            self.request_finalize()
        except Exception as e:
            logger.debug(f"{self} could not finalize the turn: {self._redact(e)}")

    # ── Results ──────────────────────────────────────────────────────

    async def _receive_messages(self):
        async for raw in self._get_websocket():
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                logger.warning(f"{self} non-JSON message from 60db: {raw[:120]!r}")
                continue
            await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]):
        kind = event.get("type")
        if kind == "transcription":
            await self._handle_transcription(event)
        elif kind == "no_speech":
            if event.get("terminal"):
                self._interims.pop(event.get("utterance_id"), None)
        elif kind == "error":
            await self._handle_error(event)
        elif kind == "warning":
            logger.warning(f"{self} 60db warning {event.get('code')}: {event.get('message')}")
        elif kind == "session_stopped":
            logger.info(f"{self} 60db session stopped: {event.get('billing_summary')}")

    async def _handle_transcription(self, event: dict[str, Any]):
        utterance = event.get("utterance_id")
        text = (event.get("text") or "").strip()
        language = _language(event.get("language"))

        if not event.get("is_final"):
            if text:
                self._interims[utterance] = text
                await self.push_frame(
                    InterimTranscriptionFrame(
                        text=text,
                        user_id=self._user_id,
                        timestamp=time_now_iso8601(),
                        language=language,
                        result=event,
                    )
                )
            return

        final_id = event.get("final_id")
        if final_id:
            if final_id in self._seen_finals:
                return  # a retried delivery of one we already have
            self._seen_finals.add(final_id)

        interim = self._interims.pop(utterance, None)
        if not text and event.get("processing_mode") == _WITHHELD:
            if not (self._keep_short_answers and interim):
                return
            logger.info(f"{self} 60db withheld a short, unsure final; using what it heard: {interim!r}")
            text = interim
        if not text:
            return

        if event.get("endpoint_reason") == "client_finalize":
            # The transcript of the turn our VAD ended: nothing more is coming.
            self.confirm_finalize()
        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id=self._user_id,
                timestamp=time_now_iso8601(),
                language=language,
                result=event,
            )
        )

    async def _handle_error(self, event: dict[str, Any]):
        message = _error_text(event)
        if event.get("terminal"):
            # One turn's recognition failed; the session carries on.
            logger.warning(
                f"{self} 60db could not transcribe a turn ({event.get('failure_type')}): {message}"
            )
            return
        status = _error_status(event)
        error_msg = (
            f"60db STT error (status {status}): {message}" if status else f"60db STT error: {message}"
        )
        if provider_http_status(None, error_msg) in PROVIDER_PAUSE_STATUSES:
            self._rejected_status = provider_http_status(None, error_msg)
        await self.push_error(error_msg=error_msg, exception=SixtyDBError(error_msg, status))


__all__ = [
    "SIXTYDB_STT_MODEL",
    "SIXTYDB_STT_URL",
    "SixtyDBSTTService",
    "clean_keywords",
]
