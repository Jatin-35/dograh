"""Vartalaap streaming text-to-speech (https://docs.vartalaap.si/api/websocket/).

Each sentence goes over one WebSocket as its own JSON message tagged with a
``request_id``; the server answers with JSON events carrying that id and binary
frames of 24 kHz s16le audio for the sentence in progress. Sentences queue on
the connection and are served in order.

The protocol has no cancel message: closing the socket is the only way to stop
synthesis. So an interruption reconnects whenever sentences are still queued,
not only once the bot is audibly speaking; otherwise the next reply would wait
behind them on the same connection. The reconnect happens while the caller is
still talking, so the new socket is normally ready before the next reply.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, AsyncGenerator, Optional

from loguru import logger

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import InterruptibleTTSService
from pipecat.utils.tracing.service_decorators import traced_tts

from api.services.pipecat.provider_rejection import ProviderRejectionAwareWebsocket

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.protocol import State
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    raise Exception(f"Missing module: {e}") from e

VARTALAAP_STREAM_URL = "wss://api.vartalaap.si/v1/tts/stream"
VARTALAAP_SAMPLE_RATE = 24000  # the only rate Vartalaap produces

# A 503 means every worker is busy; nothing is billed, so one retry after the
# server's retry_after is worth it. Longer waits would stall the call.
_BUSY_STATUS = 503
_MAX_BUSY_RETRIES = 1
_MAX_BUSY_WAIT_S = 2.0


class VartalaapTTSService(ProviderRejectionAwareWebsocket, InterruptibleTTSService):
    """Vartalaap WebSocket text-to-speech with Hinglish and English voices."""

    def __init__(
        self,
        *,
        api_key: str,
        voice: str = "saanvi",
        speed: float = 1.0,
        profile: str = "default",
        digits: str = "hi",
        temperature: Optional[float] = None,
        url: str = VARTALAAP_STREAM_URL,
        **kwargs,
    ):
        """Initialize the service.

        Args:
            api_key: Vartalaap API key, sent as the ``x-api-key`` header.
            voice: Voice id from ``GET /v1/voices`` (e.g. ``saanvi``, ``sahil``).
            speed: Speaking speed, 0.5 to 2.0.
            profile: Output profile, ``default`` or ``telephony``.
            digits: How Hinglish voices read numbers: ``hi``, ``mixed`` or ``en``.
            temperature: Sampling temperature, 0 to 2; Vartalaap's default when None.
            url: Streaming endpoint.
            **kwargs: Passed to the parent service.
        """
        if not api_key or not api_key.strip():
            raise ValueError("Vartalaap API key is required and cannot be empty")
        kwargs.pop("sample_rate", None)
        super().__init__(
            sample_rate=VARTALAAP_SAMPLE_RATE,
            push_text_frames=True,
            pause_frame_processing=True,
            push_stop_frames=True,
            push_start_frame=True,
            settings=TTSSettings(model=None, voice=voice, language=None),
            **kwargs,
        )
        self._api_key = api_key
        self._url = url
        self._options: dict[str, Any] = {"speed": speed, "profile": profile, "digits": digits}
        if temperature is not None:
            self._options["temperature"] = temperature

        self._receive_task: Optional[asyncio.Task] = None
        self._request_ids = itertools.count(1)
        # request_id -> (context_id, message, busy retries used), for every
        # sentence sent on the current connection and not yet finished.
        self._requests: dict[str, tuple[str, dict[str, Any], int]] = {}
        self._speaking_request: Optional[str] = None

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def _connect(self):
        await super()._connect()
        await self._connect_websocket()
        if self._websocket and (self._receive_task is None or self._receive_task.done()):
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return
            self._websocket = await websocket_connect(
                self._url, additional_headers={"x-api-key": self._api_key}
            )
            logger.debug(f"{self} connected to Vartalaap")
            await self._call_event_handler("on_connected")
        except Exception as e:
            self._websocket = None
            await self.push_error(error_msg=f"{self} connection error: {e}", exception=e)
            await self._call_event_handler("on_connection_error", f"{e}")

    async def _disconnect_websocket(self):
        try:
            await self.stop_all_metrics()
            if self._websocket:
                await self._websocket.close()
        except Exception as e:
            logger.error(f"{self} error closing websocket: {e}")
        finally:
            # Closing the socket drops everything queued on it.
            self._requests.clear()
            self._speaking_request = None
            self._websocket = None
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("Websocket not connected")

    async def _handle_interruption(self, frame: InterruptionFrame, direction: FrameDirection):
        # Skip InterruptibleTTSService's version: it only reconnects while the
        # bot is audibly speaking, but sentences still queued on the server
        # would hold up the next reply just the same.
        await super(InterruptibleTTSService, self)._handle_interruption(frame, direction)
        if self._bot_speaking or self._requests:
            await self._disconnect()
            await self._connect()

    async def _receive_messages(self):
        async for message in self._get_websocket():
            try:
                if isinstance(message, bytes):
                    await self._handle_audio(message)
                else:
                    await self._handle_event(json.loads(message))
            except Exception as e:
                await self.push_error(error_msg=f"{self} error processing message: {e}", exception=e)

    def _context_for(self, request_id: Optional[str]) -> Optional[str]:
        request = self._requests.get(request_id) if request_id else None
        return request[0] if request else None

    async def _handle_audio(self, audio: bytes):
        context_id = self._context_for(self._speaking_request) or self.get_active_audio_context_id()
        await self.stop_ttfb_metrics()
        await self.append_to_audio_context(
            context_id,
            TTSAudioRawFrame(audio, VARTALAAP_SAMPLE_RATE, 1, context_id=context_id),
        )

    @staticmethod
    def _event_kind(event: dict[str, Any]) -> Optional[str]:
        """The event's name, read from its tag or, failing that, its fields.

        The docs name the events but not the field that carries the name, so
        the fields each event documents are a fallback. Missing an ``error``
        would leave the caller in silence, so it is checked first.
        """
        kind = event.get("type") or event.get("event") or event.get("kind")
        if isinstance(kind, str):
            return kind
        if "error" in event:
            return "error"
        if "ms" in event and "chunk" in event:
            return "first_audio_ms"
        if {"rtf", "wall_ms", "ttfa_ms"} & event.keys():
            return "end"
        if {"normalized", "postfx"} & event.keys() or ("chunks" in event and "voice" in event):
            return "start"
        return None

    async def _handle_event(self, event: dict[str, Any]):
        kind = self._event_kind(event)
        request_id = event.get("request_id")

        if kind == "start":
            self._speaking_request = request_id
        elif kind == "first_audio_ms":
            logger.debug(f"{self} first audio after {event.get('ms')} ms ({request_id})")
        elif kind == "end":
            await self._finish_request(request_id)
        elif kind == "error":
            await self._handle_error(request_id, event)

    async def _finish_request(self, request_id: Optional[str]):
        request = self._requests.pop(request_id, None) if request_id else None
        if request_id == self._speaking_request:
            self._speaking_request = None
        if request is None:
            return
        context_id = request[0]
        # End the bot's turn when its last queued sentence is done, rather
        # than waiting out the idle timer. A later sentence of the same reply
        # reopens the context.
        still_queued = any(other[0] == context_id for other in self._requests.values())
        if not still_queued and self.audio_context_available(context_id):
            await self.append_to_audio_context(context_id, TTSStoppedFrame(context_id=context_id))
            await self.remove_audio_context(context_id)

    async def _handle_error(self, request_id: Optional[str], event: dict[str, Any]):
        status = event.get("status")
        reason = event.get("error") or "unknown error"
        request = self._requests.get(request_id) if request_id else None

        if status == _BUSY_STATUS and request and request[2] < _MAX_BUSY_RETRIES:
            wait = min(float(event.get("retry_after") or 1), _MAX_BUSY_WAIT_S)
            logger.warning(f"{self} Vartalaap busy, retrying {request_id} in {wait}s")
            # In its own task so audio for other sentences keeps flowing.
            self.create_task(self._retry_busy(request_id, wait))
            return

        # The status goes in the text so a rejected key or exhausted balance
        # (401/402/403) is recognised by the call's provider-error handling.
        error_msg = f"Vartalaap TTS error (status {status}): {reason}" if status else f"Vartalaap TTS error: {reason}"
        logger.error(f"{self} {error_msg}")
        await self.push_error(error_msg=error_msg)
        await self._finish_request(request_id)

    async def _retry_busy(self, request_id: str, wait: float):
        await asyncio.sleep(wait)
        request = self._requests.get(request_id)
        if request is None or not self._websocket:
            return  # interrupted or reconnected meanwhile
        context_id, message, retries = request
        self._requests[request_id] = (context_id, message, retries + 1)
        try:
            await self._websocket.send(json.dumps(message))
        except Exception as e:
            await self.push_error(error_msg=f"{self} error resending text: {e}", exception=e)
            await self._finish_request(request_id)

    def _build_message(self, text: str, request_id: str) -> dict[str, Any]:
        return {
            "text": text,
            "voice": self._settings.voice,
            "format": "pcm16",
            "request_id": request_id,
            **self._options,
        }

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        logger.debug(f"{self}: Generating TTS [{text}]")
        try:
            if not self._websocket or self._websocket.state is State.CLOSED:
                await self._connect()
            request_id = f"{context_id}:{next(self._request_ids)}"
            message = self._build_message(text, request_id)
            try:
                self._requests[request_id] = (context_id, message, 0)
                await self._get_websocket().send(json.dumps(message))
                await self.start_tts_usage_metrics(text)
            except Exception as e:
                self._requests.pop(request_id, None)
                yield ErrorFrame(error=f"{self} error sending text: {e}")
                yield TTSStoppedFrame(context_id=context_id)
                await self._disconnect()
                await self._connect()
                return
            yield None
        except Exception as e:
            yield ErrorFrame(error=f"{self} error: {e}")
