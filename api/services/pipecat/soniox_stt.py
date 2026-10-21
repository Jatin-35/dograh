"""Soniox real-time STT with Dograh's error handling, and its session context.

DograhSonioxSTTService authenticates in the Authorization header (Soniox
retires the start request's api_key field on 15 January 2027), sends the v5-only
turn settings only to v5 models, and retries a failed first connection.

Soniox reports a rejected account inside the stream, as a message carrying an
HTTP status ("Error: 402 (_receive_messages) - ..."), then closes the socket.
Pipecat's service turns that into a bare error string and reconnects three
times. DograhSonioxSTTService attaches the status as an exception, so the
campaign circuit breaker reads it (a 402, out of credit, has no wording our
detector would catch), and stops reconnecting once the account is refused.
"""

import json
import re

from loguru import logger
from websockets.protocol import State

import pipecat.services.soniox.stt as _pipecat_soniox
from api.services.pipecat.provider_rejection import PROVIDER_PAUSE_STATUSES
from pipecat.services.settings import assert_given
from pipecat.services.soniox.stt import (
    SonioxContextGeneralItem,
    SonioxContextObject,
    SonioxSTTService,
)

_IN_STREAM_ERROR_RE = re.compile(r"^Error:\s*(\d{3})\b")

# Soniox caps a session's context at about 10,000 characters; stay under it.
_CONTEXT_CHAR_BUDGET = 9000
# Short descriptions go in `general`, which Soniox weighs more than `text`.
_GENERAL_MAX_CHARS = 500


class SonioxRejectedError(Exception):
    """A Soniox in-stream error, carrying its HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DograhSonioxSTTService(SonioxSTTService):
    """SonioxSTTService that reports in-stream statuses and stops on a refusal."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rejected_status: int | None = None

    async def push_error(self, error_msg: str, exception: Exception | None = None, fatal: bool = False):
        if exception is None:
            match = _IN_STREAM_ERROR_RE.match(error_msg or "")
            if match:
                status = int(match.group(1))
                exception = SonioxRejectedError(status, error_msg)
                if status in PROVIDER_PAUSE_STATUSES:
                    self._rejected_status = status
        await super().push_error(error_msg, exception, fatal)

    async def _connect_websocket(self):
        """Connect with the key in the Authorization header, as upstream pipecat does.

        Soniox deprecated the start request's ``api_key`` field: from 15 January
        2027 it refuses a connection that sends its key only there. The start
        request is otherwise pipecat's, minus that field.
        """
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            logger.debug("Connecting to Soniox STT")
            # Looked up on the module so tests can stand in for Soniox.
            self._websocket = await _pipecat_soniox.websocket_connect(
                self._url,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if not self._websocket:
                await self.push_error(error_msg=f"Unable to connect to Soniox API at {self._url}")
                raise Exception(f"Unable to connect to Soniox API at {self._url}")

            s = self._settings
            context = s.context
            if isinstance(context, SonioxContextObject):
                context = context.model_dump()
            # Sensitivity and speed level exist only on the v5 models; Soniox
            # refuses the request if they are sent to an older one.
            v5 = str(s.model).startswith("stt-rt-v5")
            config = {
                "model": s.model,
                "audio_format": self._audio_format,
                "num_channels": self._num_channels,
                "enable_endpoint_detection": not self._vad_force_turn_endpoint,
                "max_endpoint_delay_ms": s.max_endpoint_delay_ms,
                "endpoint_sensitivity": s.endpoint_sensitivity if v5 else None,
                "endpoint_latency_adjustment_level": (
                    s.endpoint_latency_adjustment_level if v5 else None
                ),
                "sample_rate": self.sample_rate,
                "language_hints": _pipecat_soniox._prepare_language_hints(
                    assert_given(s.language_hints)
                ),
                "language_hints_strict": s.language_hints_strict,
                "context": context,
                "enable_speaker_diarization": s.enable_speaker_diarization,
                "enable_language_identification": s.enable_language_identification,
                "client_reference_id": s.client_reference_id,
            }
            await self._websocket.send(json.dumps(config))

            await self._call_event_handler("on_connected")
            logger.debug("Connected to Soniox STT")
        except Exception as e:
            self._websocket = None
            await self.push_error(error_msg=f"Unable to connect to Soniox: {e}", exception=e)

    async def _connect(self):
        # As pipecat's, but the receive loop starts even when this first connect
        # failed (a network blip at call start): the loop finds no socket and
        # reconnects with backoff, so the call isn't left without transcription.
        # Upstream pipecat 03dfc04e6 does the same.
        await self._connect_websocket()
        await super(SonioxSTTService, self)._connect()
        if not self._receive_task:
            self._receive_task = self.create_task(self._receive_task_handler(self._report_error))

    async def _maybe_try_reconnect(self, error_message, report_error, error=None):
        if self._rejected_status is not None:
            # The key or the account was refused: another connection is refused
            # the same way, so stop instead of reconnecting.
            return False
        return await super()._maybe_try_reconnect(error_message, report_error, error)


def build_soniox_context(
    terms: list[str] | None, domain_context: str | None
) -> SonioxContextObject | None:
    """The session context from the agent's dictionary and its description.

    A short description goes in ``general``, which Soniox weighs more than free
    text; a long one goes in ``text``. Everything is kept under Soniox's size
    limit, terms first since they matter most.
    """
    budget = _CONTEXT_CHAR_BUDGET
    kept_terms: list[str] = []
    for term in terms or []:
        term = term.strip()
        if not term or term in kept_terms or len(term) + 1 > budget:
            continue
        kept_terms.append(term)
        budget -= len(term) + 1

    general = None
    text = None
    description = (domain_context or "").strip()[:budget]
    if description:
        if len(description) <= _GENERAL_MAX_CHARS:
            general = [SonioxContextGeneralItem(key="domain", value=description)]
        else:
            text = description

    if not (kept_terms or general or text):
        return None
    return SonioxContextObject(general=general, text=text, terms=kept_terms or None)
