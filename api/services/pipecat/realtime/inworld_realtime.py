"""Dograh subclass of pipecat's Inworld Realtime LLM service.

Inworld Realtime runs speech-to-text, an LLM chosen through Inworld's router
(``openai/...``, ``google/...``, ``anthropic/...``) and Inworld TTS in one
session. Its event model follows OpenAI Realtime, so this wrapper mirrors
:mod:`api.services.pipecat.realtime.grok_realtime` for the Dograh engine:

- **User-mute audio gating** via ``UserMuteStarted/StoppedFrame``.
- **TTSSpeakFrame as initial-response trigger** for the greeting flow.
- **One-off LLMMessagesAppendFrame handling** (user-idle prompts) without
  mutating Dograh's local ``LLMContext``; the base class only warns.
- **Workflow-control deferral**: node transitions, call end and transfers wait
  for the bot's current audio to finish; ordinary tools run at once.
- **finalized=True on TranscriptionFrame**, broadcast both ways, for parity
  with the other realtime providers.

It also carries three fixes upstream pipecat made after our pinned version:

- ``providerData`` is the wire name Inworld reads; the pinned events model
  serialises ``provider_data``, so Inworld ignored it. It now also carries
  ``auto_tool_response: false`` (pipecat answers tool calls itself; leaving it
  on risks a second reply) and the TTS language.
- Tool results were JSON-encoded twice (sent as one quoted string).
- The session key was a millisecond timestamp, so two calls starting in the
  same millisecond collided; it is now a UUID.

The caller's language goes to ``audio.input.transcription.language`` and the
agent's expected words to ``audio.input.transcription.prompts`` (Inworld STT's
vocabulary hints), which the pinned events model has no fields for.

Every session also asks for the low-latency options Inworld's docs give,
unless the agent chose otherwise: reasoning effort NONE (models that can
reason otherwise add thinking time), the ``fast_start`` TTS segmenter (sooner
first audio) and prompt caching (the large agent prompt and tools are resent
every turn). The agent's other controls map to Inworld's documented session
fields: noise reduction, max output tokens, sampling and penalties, memory,
and semantic or server VAD turn detection with their tuning.

With ``stt_tts_source="dograh"`` the session runs in text mode: Dograh's own
transcriber hears the caller and Dograh's own voice speaks, while this
Inworld Realtime session (persistent, server-side context and caching) is the
LLM. Each turn's new messages go in as text, Inworld replies with text only,
and the greeting and transition lines go straight to the voice.

Replies are held while one is running, as upstream pipecat now does. A
``response.create`` sent while the bot is speaking makes Inworld cancel that
reply mid-sentence and start the new one (seen on calls when a tool result
arrived while the bot was still talking); it can also be refused with
``conversation_already_has_active_response``, which the pinned service only
debug-logs. Either way the next reply now waits for ``response.done``.
Inworld's turn events, replies and errors are logged at info so a silence on a
call can be traced.
"""

import json
import urllib.parse
import uuid
from collections import deque
from typing import Any

from loguru import logger

from api.services.pipecat.realtime.local_turns import SPEECH_GAP_SECS, audio_secs
from api.services.pipecat.realtime.static_greeting import format_static_greeting_prompt
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.inworld.realtime import events
from pipecat.services.inworld.realtime import llm as inworld_llm
from pipecat.services.inworld.realtime.llm import InworldRealtimeLLMService
from pipecat.services.llm_service import FunctionCallFromLLM
from pipecat.utils.time import time_now_iso8601

# In Dograh-driven (manual) turns a commit can still find the buffer empty. The
# pinned service treats unknown error codes as fatal and stops reading the
# session, which would leave the call silent; this one must stay non-fatal.
inworld_llm._NON_FATAL_ERROR_CODES.add("input_audio_buffer_commit_empty")

# Lead-in kept from before the caller's voice is detected, so the first
# syllables reach Inworld in Dograh-driven turns.
_PREROLL_SECS = 0.5

# Text-reply deltas, by the GA and the beta event names.
_TEXT_DELTA_EVENTS = {"response.output_text.delta", "response.text.delta"}

# Server events worth a log line; audio and text deltas are not.
_LOGGED_SERVER_EVENTS = {
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.turn_suggestion",
    "input_audio_buffer.turn_suggestion_revoked",
    "input_audio_buffer.timeout_triggered",
}


class _ServerEventTap:
    """Wraps the Inworld socket to see each server event before the base loop."""

    def __init__(self, websocket, on_event):
        self._websocket = websocket
        self._on_event = on_event

    def __getattr__(self, name):
        return getattr(self._websocket, name)

    async def __aiter__(self):
        async for message in self._websocket:
            # Audio deltas are most of the traffic; skip decoding them twice.
            head = message[:120] if isinstance(message, str) else ""
            if "output_audio.delta" not in head and "transcript.delta" not in head:
                try:
                    await self._on_event(json.loads(message))
                except Exception as e:
                    logger.debug(f"Inworld event tap skipped a message: {e}")
            yield message


class DograhInworldRealtimeLLMService(InworldRealtimeLLMService):
    """Inworld Realtime with Dograh engine integration and upstream fixes."""

    def __init__(
        self,
        *,
        language: str | None = None,
        eagerness: str | None = None,
        expected_words: list[str] | None = None,
        temperature: float | None = None,
        speed: float | None = None,
        delivery_mode: str | None = None,
        speech_threshold: float | None = None,
        min_silence_ms: int | None = None,
        max_silence_ms: int | None = None,
        turn_confidence: float | None = None,
        noise_reduction: str | None = None,
        max_reply_tokens: int | None = None,
        reasoning_effort: str | None = None,
        top_p: float | None = None,
        frequency_penalty: float | None = None,
        presence_penalty: float | None = None,
        memory: bool = False,
        audio_start: str | None = None,
        turn_detection: str | None = None,
        server_threshold: float | None = None,
        server_silence_ms: int | None = None,
        server_prefix_padding_ms: int | None = None,
        stt_tts_source: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._noise_reduction = noise_reduction if noise_reduction not in (None, "off") else None
        self._memory = bool(memory)
        self._audio_start = audio_start or "fast_start"
        self._generation_config: dict[str, Any] = {
            "reasoning": {"effort": (reasoning_effort or "none").upper()},
            **{
                key: value
                for key, value in {
                    # Inworld enforces maxNewTokens; the session-level
                    # max_output_tokens is echoed but ignored (measured live).
                    "maxNewTokens": max_reply_tokens,
                    "topP": top_p,
                    "frequencyPenalty": frequency_penalty,
                    "presencePenalty": presence_penalty,
                }.items()
                if value is not None
            },
        }
        self._server_vad = turn_detection == "server_vad"
        # Dograh decides turns: Inworld's detection is off (manual mode), so
        # the base service's manual-mode paths commit and create replies.
        self._text_io = stt_tts_source == "dograh"
        # Dograh's turn rules decide turns in text mode too.
        self._local_turns = turn_detection == "dograh_local" and not self._text_io
        if self._local_turns or self._text_io:
            self._settings.session_properties.audio.input.turn_detection = None
        if self._text_io:
            self._settings.session_properties.output_modalities = ["text"]
        # Index into the call's context up to which Inworld has the messages.
        self._text_sent_upto = 0
        self._audio_gate_open = False
        self._caller_turn_active = False
        # Silence (s) since voice stopped without a turn starting; the audio is
        # thrown away once it reaches SPEECH_GAP_SECS (a pause between words
        # must not discard the caller's first words).
        self._unturned_gap_secs: float | None = None
        self._preroll: deque = deque()
        self._preroll_bytes = 0
        self._audio_pending = False
        self._response_requested = False
        self._server_vad_settings = {
            key: value
            for key, value in {
                "threshold": server_threshold,
                "silence_duration_ms": server_silence_ms,
                "prefix_padding_ms": server_prefix_padding_ms,
            }.items()
            if value is not None
        }
        if temperature is not None:
            self._settings.temperature = temperature
            self._settings.session_properties.temperature = temperature
        self._voice_speed = speed
        self._delivery_mode = (
            delivery_mode.upper() if delivery_mode and delivery_mode != "default" else None
        )
        # Explicit providerData.stt values override the eagerness preset field
        # by field; unset ones keep it.
        self._stt_overrides = {
            key: value
            for key, value in {
                "vad_threshold": speech_threshold,
                "min_end_of_turn_silence": min_silence_ms,
                "max_turn_silence": max_silence_ms,
                "end_of_turn_confidence_threshold": turn_confidence,
            }.items()
            if value is not None
        }
        self._dograh_language = (language or "").strip() or None
        self._expected_words = [w.strip() for w in expected_words or [] if w.strip()]
        turn_detection = self._settings.session_properties.audio.input.turn_detection
        if eagerness and turn_detection is not None:
            turn_detection.eagerness = eagerness
        self._response_active: bool = False
        self._held_response_create: events.ResponseCreateEvent | None = None
        self._last_response_create: events.ResponseCreateEvent | None = None
        self._user_is_muted: bool = False
        self._handled_initial_context: bool = False
        self._bot_is_speaking: bool = False
        self._deferred_node_transition_function_calls: list[FunctionCallFromLLM] = []
        self._pending_initial_greeting_text: str | None = None

    # ── Wire format fixes ─────────────────────────────────────────────

    def _session_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rename provider_data to providerData and add Dograh's session settings."""
        session = data.get("session")
        if not isinstance(session, dict):
            return data
        provider_data = dict(session.pop("provider_data", None) or {})
        provider_data.update(session.pop("providerData", None) or {})
        metadata = dict(provider_data.get("metadata") or {})
        metadata["sdk"] = "pipecat-realtime"
        provider_data["metadata"] = metadata
        provider_data["auto_tool_response"] = False
        provider_data.setdefault("caching", {"enabled": True})
        provider_data.setdefault("text_generation_config", dict(self._generation_config))
        tts = dict(provider_data.get("tts") or {})
        tts.setdefault("segmenter_strategy", self._audio_start)
        provider_data["tts"] = tts
        if self._memory:
            provider_data.setdefault("memory", {"enabled": True})
        if self._dograh_language:
            tts = dict(provider_data.get("tts") or {})
            tts.setdefault("language", self._dograh_language)
            provider_data["tts"] = tts
            transcription = (
                session.get("audio", {}).get("input", {}).get("transcription")
            )
            if isinstance(transcription, dict):
                transcription.setdefault("language", self._dograh_language)
        if self._expected_words:
            transcription = session.get("audio", {}).get("input", {}).get("transcription")
            if isinstance(transcription, dict):
                transcription["prompts"] = list(self._expected_words)
        if self._delivery_mode:
            provider_data["tts"]["delivery_mode"] = self._delivery_mode
        audio_in = session.get("audio", {}).get("input")
        if isinstance(audio_in, dict):
            if self._noise_reduction:
                audio_in["noise_reduction"] = {"type": self._noise_reduction}
            if self._local_turns or self._text_io:
                # Explicit null: dropping the field (as exclude_none does)
                # would leave Inworld's own turn detection on.
                audio_in["turn_detection"] = None
            turn = audio_in.get("turn_detection")
            if self._server_vad and isinstance(turn, dict):
                # Silero VAD + Smart Turn: eagerness and Inworld-STT tuning
                # don't apply; it takes its own threshold and silence.
                turn["type"] = "server_vad"
                turn.pop("eagerness", None)
                turn.update(self._server_vad_settings)
        if (
            self._stt_overrides
            and not self._server_vad
            and not self._local_turns
            and not self._text_io
        ):
            stt = dict(provider_data.get("stt") or {})
            stt.update(self._stt_overrides)
            provider_data["stt"] = stt
        output = session.get("audio", {}).get("output")
        if self._voice_speed is not None and isinstance(output, dict):
            output["speed"] = self._voice_speed
        session["providerData"] = provider_data
        return data

    async def send_client_event(self, event: events.ClientEvent):
        if isinstance(event, events.ResponseCreateEvent):
            if self._response_active:
                logger.info(f"{self}: Inworld reply still running; holding the next one")
                self._held_response_create = event
                return
            self._last_response_create = event
            self._response_requested = True
        data = event.model_dump(exclude_none=True)
        if isinstance(event, events.SessionUpdateEvent):
            data = self._session_payload(data)
        await self._ws_send(data)
        if isinstance(event, events.InputAudioBufferAppendEvent):
            self._audio_pending = True
        elif isinstance(
            event, (events.InputAudioBufferCommitEvent, events.InputAudioBufferClearEvent)
        ):
            self._audio_pending = False

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

    async def _connect(self):
        """Connect with a unique session key (the base uses a millisecond timestamp)."""
        try:
            if self._websocket:
                return
            scheme = "Bearer" if self._auth_type == "bearer" else "Basic"
            params = urllib.parse.urlencode(
                {"key": f"pipecat-realtime-{uuid.uuid4().hex}", "protocol": "realtime"}
            )
            separator = "&" if "?" in self.base_url else "?"
            websocket = await inworld_llm.websocket_connect(
                uri=f"{self.base_url}{separator}{params}",
                additional_headers={"Authorization": f"{scheme} {self.api_key}"},
            )
            self._websocket = _ServerEventTap(websocket, self._on_server_event)
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as e:
            await self.push_error(error_msg=f"Error connecting to Inworld: {e}", exception=e)
            self._websocket = None

    async def _disconnect(self):
        await super()._disconnect()
        self._response_active = False
        self._held_response_create = None
        self._last_response_create = None
        self._response_requested = False
        self._audio_pending = False
        self._audio_gate_open = False
        self._caller_turn_active = False
        self._unturned_gap_secs = None
        self._preroll.clear()
        self._preroll_bytes = 0

    # ── Reply holding and turn logging ────────────────────────────────

    async def _on_server_event(self, evt: dict[str, Any]):
        """See each server event (bar audio) before the base receive loop does."""
        kind = evt.get("type", "")
        if kind in _TEXT_DELTA_EVENTS:
            delta = evt.get("delta") or ""
            if delta and self._text_io:
                await self.stop_ttfb_metrics()
                await self.push_frame(LLMTextFrame(delta))
            return
        if kind == "response.created":
            self._response_active = True
            self._response_requested = False
            logger.info(f"{self}: Inworld reply started")
        elif kind in _LOGGED_SERVER_EVENTS:
            logger.info(f"{self}: Inworld {kind.removeprefix('input_audio_buffer.')}")
        elif kind == "error":
            error = evt.get("error") or {}
            code = error.get("code")
            logger.warning(f"{self}: Inworld error {code}: {error.get('message')}")
            if code == "conversation_already_has_active_response":
                # Refused because a reply we had not yet heard of is running:
                # send ours once that one is done.
                self._response_active = True
                if self._held_response_create is None:
                    self._held_response_create = self._last_response_create

    async def _handle_evt_response_done(self, evt):
        await super()._handle_evt_response_done(evt)
        self._response_active = False
        self._response_requested = False
        response = getattr(evt, "response", None)
        outputs = [getattr(o, "type", "?") for o in getattr(response, "output", None) or []]
        logger.info(
            f"{self}: Inworld reply done ({getattr(response, 'status', '?')}, {outputs})"
        )
        held = self._held_response_create
        if held is not None:
            self._held_response_create = None
            logger.info(f"{self}: sending the held Inworld reply")
            await self.send_client_event(held)

    async def _handle_evt_speech_started(self, evt):
        # The caller's new turn gets a reply of its own.
        self._held_response_create = None
        await super()._handle_evt_speech_started(evt)

    # ── Dograh-driven turns (manual mode) ─────────────────────────────

    async def _handle_user_stopped_speaking(self, frame):
        """Dograh ended the caller's turn: commit their speech and ask for a reply."""
        if self._text_io:
            # The turn's text arrives as a context frame; no audio to commit.
            return
        if not self._local_turns:
            await super()._handle_user_stopped_speaking(frame)
            return
        self._caller_turn_active = False
        self._close_audio_gate()
        if not self._audio_pending:
            # Nothing reached Inworld this turn (e.g. the caller was muted):
            # an empty commit would only draw an error.
            logger.info(f"{self}: caller turn ended with no audio sent; no reply asked")
            return
        logger.info(f"{self}: caller turn ended; committing and asking for a reply")
        await self.start_ttfb_metrics()
        await self.start_processing_metrics()
        await self.send_client_event(events.InputAudioBufferCommitEvent())
        await self.send_client_event(events.ResponseCreateEvent())

    async def _handle_interruption(self):
        """The caller really interrupted: stop the reply, keep their speech.

        Unlike the base manual mode, the input buffer is not cleared here: it
        already holds the start of the caller's words (sent from the pre-roll),
        which clearing would cut off.
        """
        if not (self._local_turns or self._text_io):
            await super()._handle_interruption()
            return
        self._held_response_create = None
        if self._response_active or self._response_requested:
            await self.send_client_event(events.ResponseCancelEvent())
        await self._truncate_current_audio_response()
        await self.stop_all_metrics()
        if self._current_assistant_response:
            await self.push_frame(LLMFullResponseEndFrame())
            await self.push_frame(TTSStoppedFrame())

    async def _open_audio_gate(self):
        if self._audio_gate_open:
            return
        self._audio_gate_open = True
        preroll = list(self._preroll)
        self._preroll.clear()
        self._preroll_bytes = 0
        for frame in preroll:
            await self._send_user_audio(frame)

    def _close_audio_gate(self):
        self._audio_gate_open = False

    async def _discard_unturned_audio(self):
        """Voice that never became a turn (noise, echo) must not join the next one."""
        self._unturned_gap_secs = None
        self._close_audio_gate()
        self._audio_buffer = b""
        if self._audio_pending:
            await self.send_client_event(events.InputAudioBufferClearEvent())

    def _remember_preroll(self, frame):
        self._preroll.append(frame)
        self._preroll_bytes += len(frame.audio)
        limit = int(frame.sample_rate * 2 * frame.num_channels * _PREROLL_SECS)
        while self._preroll and self._preroll_bytes > limit:
            self._preroll_bytes -= len(self._preroll.popleft().audio)

    async def _track_local_turn_frame(self, frame: Frame):
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._unturned_gap_secs = None
            await self._open_audio_gate()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._caller_turn_active = True
            self._unturned_gap_secs = None
            await self._open_audio_gate()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if not self._caller_turn_active:
                self._unturned_gap_secs = 0.0

    # ── Text mode (Dograh's own transcriber and voice) ────────────────

    async def _handle_text_turn(self, context: LLMContext):
        """Send the turn's new messages as text and ask Inworld for a reply."""
        self._handled_initial_context = True
        self._context = context
        messages = context.get_messages()
        if self._llm_needs_conversation_setup or not self._api_session_ready:
            # The first reply sets the conversation up with every message so
            # far (or waits for the session, then does).
            self._text_sent_upto = len(messages)
            await self._create_response()
            return
        new_items = [
            item
            for item in (
                self._message_to_conversation_item(m)
                for m in messages[self._text_sent_upto :]
                if isinstance(m, dict) and m.get("role") in {"user", "system", "developer"}
            )
            if item is not None
        ]
        self._text_sent_upto = len(messages)
        await self._process_completed_function_calls(send_new_results=True)
        if not new_items:
            return
        for item in new_items:
            evt = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[evt.item.id] = True
            await self.send_client_event(evt)
        await self._create_response()

    async def _note_spoken_text(self, text: str):
        """Tell Inworld about a line the voice spoke that isn't in the context."""
        if not text or not self._api_session_ready or self._llm_needs_conversation_setup:
            return
        item = events.ConversationItem(
            type="message",
            role="system",
            content=[
                events.ItemContent(
                    type="input_text",
                    text=f'You just said this to the caller; do not repeat it: "{text}"',
                )
            ],
        )
        evt = events.ConversationItemCreateEvent(item=item)
        self._messages_added_manually[evt.item.id] = True
        await self.send_client_event(evt)

    # ── Dograh engine integration (as in the Grok wrapper) ────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, UserMuteStartedFrame):
            self._user_is_muted = True
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, UserMuteStoppedFrame):
            self._user_is_muted = False
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSSpeakFrame) and self._text_io:
            # Dograh's voice speaks fixed lines itself, word for word. The
            # greeting reaches the context (append_to_context) and so Inworld at
            # set-up; a transition line doesn't, so Inworld is told of it.
            if not self._handled_initial_context:
                self._handled_initial_context = True
            elif not frame.append_to_context:
                await self._note_spoken_text((frame.text or "").strip())
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSSpeakFrame):
            if not self._handled_initial_context:
                greeting_text = frame.text.strip() if frame.text else ""
                if greeting_text:
                    await self._handle_initial_greeting(self._context, greeting_text)
                else:
                    await self._handle_context(self._context)
            else:
                logger.warning(
                    f"{self}: TTSSpeakFrame after initial context already "
                    "handled; Inworld Realtime owns audio generation, ignoring"
                )
            return
        if isinstance(frame, LLMMessagesAppendFrame):
            await self._handle_messages_append(frame)
            return
        if self._local_turns:
            await self._track_local_turn_frame(frame)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_is_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_is_speaking = False
            await self._run_pending_node_transition_function_calls()
        await super().process_frame(frame, direction)

    async def _handle_messages_append(self, frame: LLMMessagesAppendFrame):
        """Consume a one-off append frame without mutating the local LLMContext."""
        if self._disconnecting:
            return
        if not self._api_session_ready:
            if frame.run_llm:
                self._run_llm_when_api_session_ready = True
            return
        appended_any = False
        for message in frame.messages:
            item = self._message_to_conversation_item(message)
            if item is None:
                continue
            evt = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[evt.item.id] = True
            await self.send_client_event(evt)
            appended_any = True
        if frame.run_llm and appended_any:
            await self._send_manual_response_create()

    async def _handle_context(self, context: LLMContext):
        if self._text_io:
            await self._handle_text_turn(context)
            return
        if not self._handled_initial_context:
            if context is None:
                logger.warning(f"{self}: initial context trigger before context was set")
                return
            self._handled_initial_context = True
            self._context = context
            await self._create_response()
        else:
            self._context = context
            await self._process_completed_function_calls(send_new_results=True)

    async def _handle_initial_greeting(self, context: LLMContext, greeting_text: str):
        if context is None:
            logger.warning(f"{self}: initial greeting trigger before context was set")
            return
        self._handled_initial_context = True
        self._context = context
        await self._create_initial_greeting_response(greeting_text)

    async def _create_initial_greeting_response(self, greeting_text: str):
        if self._disconnecting:
            return
        if not self._api_session_ready:
            self._pending_initial_greeting_text = greeting_text
            self._run_llm_when_api_session_ready = True
            return
        self._pending_initial_greeting_text = None
        await self._ensure_conversation_setup()
        item = events.ConversationItem(
            type="message",
            role="user",
            content=[
                events.ItemContent(
                    type="input_text", text=format_static_greeting_prompt(greeting_text)
                )
            ],
        )
        evt = events.ConversationItemCreateEvent(item=item)
        self._messages_added_manually[evt.item.id] = True
        await self.send_client_event(evt)
        await self._send_manual_response_create()

    async def _ensure_conversation_setup(self):
        if not self._llm_needs_conversation_setup:
            return
        adapter = self.get_llm_adapter()
        params = adapter.get_llm_invocation_params(
            self._context, system_instruction=self._settings.system_instruction
        )
        for item in params["messages"]:
            evt = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[evt.item.id] = True
            await self.send_client_event(evt)
        await self._send_session_update()
        self._llm_needs_conversation_setup = False

    async def _handle_evt_session_updated(self, evt):
        self._api_session_ready = True
        if self._pending_initial_greeting_text is not None:
            greeting_text = self._pending_initial_greeting_text
            self._run_llm_when_api_session_ready = False
            await self._create_initial_greeting_response(greeting_text)
        elif self._run_llm_when_api_session_ready:
            self._run_llm_when_api_session_ready = False
            await self._create_response()

    async def _send_user_audio(self, frame):
        if self._text_io:
            # Dograh's own transcriber hears the caller.
            return
        if self._user_is_muted:
            return
        if self._local_turns and not self._audio_gate_open:
            # Only the caller's speech goes to Inworld in Dograh-driven turns;
            # keep a short lead-in for when their voice is detected.
            self._remember_preroll(frame)
            return
        await super()._send_user_audio(frame)
        if self._local_turns and self._unturned_gap_secs is not None:
            self._unturned_gap_secs += audio_secs(frame)
            if self._unturned_gap_secs >= SPEECH_GAP_SECS:
                await self._discard_unturned_audio()

    def _message_to_conversation_item(
        self, message: dict[str, Any]
    ) -> events.ConversationItem | None:
        if not isinstance(message, dict):
            logger.warning(f"{self}: skipping unsupported appended message {message!r}")
            return None
        role = message.get("role")
        if role not in {"user", "system", "developer"}:
            logger.warning(f"{self}: skipping appended message role {role!r}")
            return None
        text = self._extract_text_content(message.get("content"))
        if not text:
            logger.warning(f"{self}: skipping appended message content {message!r}")
            return None
        return events.ConversationItem(
            type="message",
            role="system" if role in {"system", "developer"} else "user",
            content=[events.ItemContent(type="input_text", text=text)],
        )

    @staticmethod
    def _extract_text_content(content: Any) -> str | None:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    return None
                text = part.get("text")
                if not isinstance(text, str):
                    return None
                parts.append(text)
            return "\n".join(parts) if parts else None
        return None

    async def _send_manual_response_create(self):
        """Trigger inference after manually appending conversation items."""
        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        await self.send_client_event(
            events.ResponseCreateEvent(
                response=events.ResponseProperties(modalities=["text", "audio"])
            )
        )

    async def _run_pending_node_transition_function_calls(self):
        if not self._deferred_node_transition_function_calls:
            return
        function_calls = self._deferred_node_transition_function_calls
        self._deferred_node_transition_function_calls = []
        await self.run_function_calls(function_calls)

    async def _handle_evt_function_call_arguments_done(self, evt):
        """Run ordinary tools immediately and defer workflow-control calls."""
        try:
            args = json.loads(evt.arguments) if evt.arguments else {}
            item = self._pending_function_calls.pop(evt.call_id, None)
            if item is None:
                logger.warning(f"No tracked function call for call_id {evt.call_id}")
                return
            name = getattr(evt, "name", None) or item.name
            calls = [
                FunctionCallFromLLM(
                    context=self._context,
                    tool_call_id=evt.call_id,
                    function_name=name,
                    arguments=args,
                )
            ]
            if self._bot_is_speaking and self._function_is_node_transition(name):
                self._deferred_node_transition_function_calls.extend(calls)
            else:
                await self.run_function_calls(calls)
        except Exception as e:
            logger.error(f"Failed to process function call arguments: {e}")

    async def _handle_evt_input_audio_transcription_completed(self, evt):
        self._interim_transcription_text = ""
        await self._call_event_handler("on_conversation_item_updated", evt.item_id, None)
        transcript = evt.transcript.strip() if evt.transcript else ""
        if not transcript:
            return
        await self.broadcast_frame(
            TranscriptionFrame,
            text=transcript,
            user_id="",
            timestamp=time_now_iso8601(),
            result=evt,
            finalized=True,
        )
