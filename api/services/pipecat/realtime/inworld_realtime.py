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

The caller's language goes to ``audio.input.transcription.language``, which
the pinned events model has no field for.
"""

import json
import urllib.parse
import uuid
from typing import Any

from loguru import logger

from api.services.pipecat.realtime.static_greeting import format_static_greeting_prompt
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.inworld.realtime import events
from pipecat.services.inworld.realtime import llm as inworld_llm
from pipecat.services.inworld.realtime.llm import InworldRealtimeLLMService
from pipecat.services.llm_service import FunctionCallFromLLM
from pipecat.utils.time import time_now_iso8601


class DograhInworldRealtimeLLMService(InworldRealtimeLLMService):
    """Inworld Realtime with Dograh engine integration and upstream fixes."""

    def __init__(self, *, language: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._dograh_language = (language or "").strip() or None
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
        if self._dograh_language:
            tts = dict(provider_data.get("tts") or {})
            tts.setdefault("language", self._dograh_language)
            provider_data["tts"] = tts
            transcription = (
                session.get("audio", {}).get("input", {}).get("transcription")
            )
            if isinstance(transcription, dict):
                transcription.setdefault("language", self._dograh_language)
        session["providerData"] = provider_data
        return data

    async def send_client_event(self, event: events.ClientEvent):
        data = event.model_dump(exclude_none=True)
        if isinstance(event, events.SessionUpdateEvent):
            data = self._session_payload(data)
        await self._ws_send(data)

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
            self._websocket = await inworld_llm.websocket_connect(
                uri=f"{self.base_url}{separator}{params}",
                additional_headers={"Authorization": f"{scheme} {self.api_key}"},
            )
            self._receive_task = self.create_task(self._receive_task_handler())
        except Exception as e:
            await self.push_error(error_msg=f"Error connecting to Inworld: {e}", exception=e)
            self._websocket = None

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
        if self._user_is_muted:
            return
        await super()._send_user_audio(frame)

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
