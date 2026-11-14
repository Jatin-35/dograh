"""Boson AI Higgs Realtime, on top of Dograh's OpenAI Realtime service.

Higgs Realtime is one speech-to-speech model behind an OpenAI Realtime (GA)
compatible WebSocket, so the OpenAI wrapper's Dograh integration (mute gating,
greeting trigger, one-off appended messages, deferred node transitions) is
reused as is. Where Boson differs from OpenAI, this class adapts:

- **Nothing arrives on connect.** Boson sends ``session.created`` only in
  reply to the first ``session.update``, so the update goes out right after
  connecting, and ``session.created`` marks the session ready.
- **event_id** is required by the pinned parser on every server event and
  is not promised by Boson; it is added where missing.
- **Extra server events** (``conversation.context.summarized``,
  ``session.idle_timeout``, ``state.changed`` and others) would stop the
  pinned parser, so only types it has a model for reach it.
- **Session settings**: turn detection is fixed once the session starts, so
  it goes in the first ``session.update`` only; semantic VAD must say
  ``eagerness: "auto"``. A repeated caller transcript is passed on once.
- **A new reply cuts the one still playing**: Boson interrupts an in-flight
  response for each ``response.create``, so one sent while a reply runs (a
  tool result's follow-up, say) waits until that reply is done.
- **Errors keep the session open** on Boson; the base treats every ``error``
  as fatal and stops reading, leaving the call silent. Errors are logged, and
  only refusals that end the session are reported.
- **Close codes say why a session ended** (3000 rejected key, 4429 no credit,
  1013 too many sessions); they are reported in words the provider-rejection
  check and the campaign circuit breaker recognise.
- **No ``system`` role in conversation items**: appended instructions go in
  as ``user`` items.
- **Tool results** are sent as given (the base JSON-encodes strings twice).
- **Transcripts of the caller** need ``higgs-stt-3.1`` set explicitly.
"""

import json
from typing import Any

from loguru import logger
from websockets.exceptions import ConnectionClosed

from api.services.pipecat.realtime.openai_realtime import DograhOpenAIRealtimeLLMService
from pipecat.services.openai.realtime import events
from pipecat.services.openai.realtime import llm as openai_realtime_llm

BOSON_REALTIME_URL = "wss://api.boson.ai/v1/realtime"
BOSON_REALTIME_MODEL = "higgs-realtime"
BOSON_TRANSCRIPTION_MODEL = "higgs-stt-3.1"

# Events Boson sends that the pinned OpenAI parser has no model for.
_BOSON_ONLY_EVENTS = {
    "conversation.context.summarized",
    "response.output_audio_transcript.length",
    "session.idle_timeout",
    "session.max_duration_reached",
}
# Everything the pinned parser has a model for; anything else is dropped.
_PARSEABLE_EVENTS = frozenset(events._server_event_types)
# Errors that refuse a response.create outright (no response.done follows).
# Boson shows some of these as the error type rather than the code.
_RESPONSE_CREATE_REFUSALS = {
    "voice_output_task_ongoing",
    "invalid_reference_item_id",
    "invalid_value",
    "rate_limit_exceeded",
}
# Session-ending close codes, in words provider_rejection.py recognises.
_CLOSE_REASONS = {
    3000: "Boson Higgs Realtime rejected the API key (invalid API key)",
    4429: (
        "Boson Higgs Realtime refused the session: insufficient_quota "
        "(no credit, spending cap reached or contract ended)"
    ),
    1013: "Boson Higgs Realtime is at its concurrent session limit; try again shortly",
}


class _BosonEventFilter:
    """Wraps the socket so the base receive loop sees only events it can parse."""

    def __init__(self, websocket, service: "DograhBosonRealtimeLLMService"):
        self._websocket = websocket
        self._service = service

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    async def __aiter__(self):
        try:
            async for message in self._websocket:
                forwarded = await self._service._filter_server_message(message)
                if forwarded is not None:
                    yield forwarded
        except ConnectionClosed as e:
            await self._service._report_close(e)


class DograhBosonRealtimeLLMService(DograhOpenAIRealtimeLLMService):
    """Higgs Realtime with Dograh engine integration. See module docstring."""

    def __init__(self, *, temperature: float | None = None, **kwargs):
        kwargs.setdefault("base_url", BOSON_REALTIME_URL)
        super().__init__(**kwargs)
        self._boson_temperature = temperature
        self._response_active = False
        # A response.create we sent that Boson has not yet started.
        self._awaiting_response = False
        self._held_response_create: events.ResponseCreateEvent | None = None
        # Caller transcripts already passed on, by item (Boson can repeat one).
        self._transcribed_items: dict[str, None] = {}
        # Turn detection is fixed once the session starts; it goes in the
        # first session.update only.
        self._session_started = False

    # ── Connection ────────────────────────────────────────────────────

    async def _connect(self):
        try:
            if self._websocket:
                return
            websocket = await openai_realtime_llm.websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
            )
            self._websocket = _BosonEventFilter(websocket, self)
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as e:
            await self.push_error(
                error_msg=f"Error connecting to Boson Higgs Realtime: {e}", exception=e
            )
            self._websocket = None
            return
        # Boson waits for the first session.update before saying anything.
        await self._send_session_update()

    async def _handle_evt_session_created(self, evt):
        # The reply to our first session.update: the session is configured.
        await self._handle_evt_session_updated(evt)

    async def _disconnect(self):
        await super()._disconnect()
        self._response_active = False
        self._awaiting_response = False
        self._held_response_create = None
        # A new connection is a new session: it needs turn detection again.
        self._session_started = False
        self._transcribed_items.clear()

    async def _handle_evt_response_done(self, evt):
        await super()._handle_evt_response_done(evt)
        # Done, cancelled or failed: the next reply may start.
        await self._release_held_response()

    async def _handle_evt_speech_started(self, evt):
        # The caller cut in. Boson answers when they stop, and that reply sees
        # everything sent so far (a tool's result included), so a held request
        # would only make a second, overlapping reply.
        if self._held_response_create is not None:
            logger.info(f"{self}: caller spoke; dropping the held Boson reply")
            self._held_response_create = None
        await super()._handle_evt_speech_started(evt)

    async def _release_held_response(self):
        self._response_active = False
        self._awaiting_response = False
        held, self._held_response_create = self._held_response_create, None
        if held is not None:
            await self.send_client_event(held)

    async def _report_close(self, error: ConnectionClosed):
        code = error.rcvd.code if error.rcvd else None
        reason = _CLOSE_REASONS.get(code)
        if self._disconnecting:
            return
        if reason:
            await self.push_error(
                error_msg=f"{reason} (close code {code})", exception=error
            )
        else:
            logger.warning(f"{self}: Boson session closed (code {code}): {error}")

    # ── Outgoing ──────────────────────────────────────────────────────

    async def send_client_event(self, event: events.ClientEvent):
        if isinstance(event, events.ResponseCreateEvent):
            if self._response_active:
                # Boson would cut the reply still playing to start this one.
                logger.info(f"{self}: Boson reply still running; holding the next one")
                self._held_response_create = event
                return
            # Counts as running from now: a second request before
            # response.created arrives must wait too.
            self._response_active = True
            self._awaiting_response = True
        data = event.model_dump(exclude_none=True)
        if isinstance(event, events.SessionUpdateEvent):
            self._adapt_session(data.get("session") or {})
            self._session_started = True
        await self._ws_send(data)

    def _adapt_session(self, session: dict[str, Any]) -> None:
        # Fields Boson rejects outright; the GA model may carry them.
        for field in (
            "include",
            "prompt",
            "tracing",
            "reasoning",
            "expires_at",
            "id",
            "object",
        ):
            session.pop(field, None)
        if self._boson_temperature is not None:
            session["temperature"] = self._boson_temperature
        audio_in = session.get("audio", {}).get("input")
        if isinstance(audio_in, dict):
            turn = audio_in.get("turn_detection")
            if self._session_started:
                audio_in.pop("turn_detection", None)
            elif isinstance(turn, dict) and turn.get("type") == "semantic_vad":
                # Boson ends the session for any eagerness but "auto".
                turn["eagerness"] = "auto"
            transcription = audio_in.get("transcription")
            if isinstance(transcription, dict):
                transcription["model"] = BOSON_TRANSCRIPTION_MODEL
                transcription.pop("prompt", None)

    async def _send_tool_result(self, tool_call_id: str, result: Any):
        if result is None:
            output = ""
        elif isinstance(result, str):
            output = result
        else:
            output = json.dumps(result, ensure_ascii=False)
        item = events.ConversationItem(
            type="function_call_output", call_id=tool_call_id, output=output
        )
        await self.send_client_event(events.ConversationItemCreateEvent(item=item))

    def _message_to_conversation_item(self, message: dict[str, Any]):
        item = super()._message_to_conversation_item(message)
        if item is not None and item.role == "system":
            # Boson rejects system-role items; instructions belong to the session.
            item.role = "user"
        return item

    # ── Incoming ──────────────────────────────────────────────────────

    async def _filter_server_message(self, message):
        """Return the message for the base loop, or None when handled here."""
        if isinstance(message, str) and "output_audio.delta" in message[:80]:
            # Most of the traffic: skip decoding, only make sure it parses.
            if '"event_id"' in message:
                return message
            return '{"event_id":"boson",' + message.lstrip()[1:]
        try:
            evt = json.loads(message)
        except (TypeError, ValueError):
            return message
        kind = evt.get("type", "")
        if kind == "response.created":
            # Ours, or one Boson started itself when the caller stopped talking.
            self._response_active = True
            self._awaiting_response = False
        # The pinned parser requires event_id, which Boson need not send.
        added_id = "event_id" not in evt
        evt.setdefault("event_id", "boson")
        if kind in _BOSON_ONLY_EVENTS:
            await self._handle_boson_event(kind, evt)
            return None
        if kind not in _PARSEABLE_EVENTS:
            # Boson sends more than it documents (state.changed, ...); the
            # pinned parser raises on any type it lacks, ending the call.
            logger.debug(f"{self}: ignoring Boson event {kind}")
            return None
        if kind == "conversation.item.input_audio_transcription.completed":
            item_id = evt.get("item_id")
            if item_id in self._transcribed_items:
                return None
            self._transcribed_items[item_id] = None
            if len(self._transcribed_items) > 4096:
                self._transcribed_items.pop(next(iter(self._transcribed_items)))
        if kind == "error":
            await self._handle_boson_error(evt.get("error") or {})
            return None
        if kind in ("session.created", "session.updated"):
            # Only the event matters here; Boson's session echo carries fields
            # (truncation, opus formats, extra VAD keys) the GA model can't parse.
            return json.dumps(
                {"type": kind, "event_id": evt["event_id"], "session": {}}
            )
        if kind == "response.done" and isinstance(evt.get("response"), dict):
            # The base reads usage unguarded; make sure every field it needs exists.
            usage = evt["response"].get("usage") or {}
            for field in ("total_tokens", "input_tokens", "output_tokens"):
                usage[field] = usage.get(field) or 0
            for field in ("input_token_details", "output_token_details"):
                usage[field] = usage.get(field) or {}
            evt["response"]["usage"] = usage
            return json.dumps(evt)
        return json.dumps(evt) if added_id else message

    async def _handle_boson_event(self, kind: str, evt: dict[str, Any]):
        if kind == "conversation.context.summarized":
            logger.info(
                f"{self}: Boson summarised older context "
                f"({evt.get('token_count_before')} -> {evt.get('token_count_after')} tokens)"
            )
        elif kind == "session.idle_timeout":
            logger.warning(
                f"{self}: Boson closed the session after {evt.get('seconds_idle')}s "
                "without caller speech"
            )
        elif kind == "session.max_duration_reached":
            await self.push_error(
                error_msg=(
                    "Boson Higgs Realtime session reached its maximum length "
                    f"({evt.get('max_duration_sec')}s)"
                )
            )

    async def _handle_boson_error(self, error: dict[str, Any]):
        code = error.get("code")
        kind = error.get("type")
        message = error.get("message") or ""
        if code == "conversation_already_has_active_response":
            logger.debug(f"{self}: {message}")
            return
        logger.warning(f"{self}: Boson error {kind}/{code}: {message}")
        if self._awaiting_response and (
            code in _RESPONSE_CREATE_REFUSALS or kind in _RESPONSE_CREATE_REFUSALS
        ):
            # Our response.create was refused, so no response.done will come
            # for it; don't leave later replies held behind it.
            await self._release_held_response()
        if kind == "insufficient_quota":
            # Followed by close 4429; say why now, in words the circuit breaker knows.
            await self.push_error(
                error_msg=f"Boson Higgs Realtime: insufficient_quota ({code}): {message}"
            )
