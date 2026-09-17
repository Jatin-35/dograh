"""Dograh subclass of pipecat's Gemini Live LLM service.

Layers Dograh engine integration quirks onto upstream-pristine
:class:`GeminiLiveLLMService`:

- **Deferred connect.** Connection is held back until ``system_instruction``
  is set via :meth:`_update_settings`, so pre-call-fetch template variables
  land before the live session opens.
- **Reconnect on node transitions.** Gemini Live cannot update
  ``system_instruction`` mid-session, so a setting change triggers a
  reconnect (deferred until the bot turn ends if currently responding).
- **Node-transition deferral.** Node-transition calls emitted mid-turn are
  queued and run when the bot stops speaking, to avoid cutting off its audio.
- **User-mute audio gating.** ``UserMuteStarted/StoppedFrame`` from the
  user aggregator gates whether incoming audio is forwarded to Gemini.
- **TTSSpeakFrame as greeting trigger.** The engine queues a TTSSpeakFrame
  to kick off the first response after node setup; the service intercepts
  it and runs the initial-context path.
"""

import asyncio
import os
from typing import Any

from google.genai.types import Content, LiveServerMessage, Part
from loguru import logger

from api.services.pipecat.gemini_json_schema_adapter import (
    DograhGeminiJSONSchemaAdapter,
)
from api.services.pipecat.realtime.static_greeting import format_static_greeting_prompt
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    TTSSpeakFrame,
    UserMuteStartedFrame,
    UserMuteStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallFromLLM
from pipecat.utils.tracing.service_decorators import traced_gemini_live

# Gemini Live bills the whole accumulated context on every model turn, and holds
# spoken history in the session as audio — far more tokens than the equivalent
# transcript, at four times the per-token price. A reconnect reseeds the fresh
# session from LLMContext, which holds text, so refreshing the session once the
# context passes this many tokens trades one reconnect for a much cheaper
# context from there on.
#
# Off by default: the refresh costs a reconnect (~1s on this pipeline), during
# which the user hears no difference but the bot is not listening. Measure that
# gap on a test call before turning it on, and pick a trigger high enough that it
# fires once on a long call rather than every few turns.
CONTEXT_COMPACTION_TRIGGER_TOKENS = int(
    os.getenv("GEMINI_LIVE_CONTEXT_COMPACTION_TRIGGER_TOKENS", "0")
)

# Cap on user audio held during a compaction refresh. Sized well past the
# expected reconnect so a stalled one sheds the oldest audio instead of growing
# without bound. ~5s of 16 kHz mono PCM.
MAX_COMPACTION_BUFFERED_AUDIO_BYTES = 16_000 * 2 * 5

# Server-side sliding-window compression. Bounds growth between compactions and
# lifts Gemini's 15-minute cap on uncompressed audio sessions. 0 disables.
COMPRESSION_TRIGGER_TOKENS = int(
    os.getenv("GEMINI_LIVE_COMPRESSION_TRIGGER_TOKENS", "16000")
)


class DograhGeminiLiveLLMService(GeminiLiveLLMService):
    """Gemini Live with Dograh engine integration quirks. See module docstring."""

    # Gemini input transcription is delivered independently from tool calls.
    # Give late transcription messages a small window to arrive before running
    # a node-transition function and tearing down the current Live connection.
    _NODE_TRANSITION_TRANSCRIPTION_GRACE_SECONDS = 0.5

    # Route tool schemas through Gemini's ``parameters_json_schema`` field so
    # MCP/imported tools that use JSON Schema keywords (``const``, ``not``,
    # nested ``anyOf``) rejected by the strict ``Schema`` model are accepted.
    # Mirrors the non-realtime ``DograhGoogleLLMService`` fix;
    # ``DograhGeminiLiveVertexLLMService`` inherits this via MRO.
    adapter_class = DograhGeminiJSONSchemaAdapter

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # User-mute state, driven by broadcast UserMute{Started,Stopped}Frames.
        # Audio is not forwarded to Gemini while muted.
        self._user_is_muted: bool = False
        # Guards initial-response triggering against double-firing across the
        # initial TTSSpeakFrame and any LLMContextFrame that may arrive.
        self._handled_initial_context: bool = False
        # Node-transition calls emitted mid-bot-turn are deferred here so the
        # transition does not tear down Gemini while it is still producing audio.
        self._pending_node_transition_function_calls: list[FunctionCallFromLLM] = []
        # Text greeting captured from the first TTSSpeakFrame while the Gemini
        # session is still connecting.
        self._pending_initial_greeting_text: str | None = None
        self._transition_function_call_task: asyncio.Task | None = None
        # Intentional node changes use a fresh, context-seeded connection rather
        # than a potentially stale session-resumption handle. The new connection
        # remains gated until the function-call result has landed in LLMContext.
        self._awaiting_node_transition_context: bool = False
        self._node_transition_context_received: bool = False
        self._node_transition_context_seed_started: bool = False
        # Cost compaction: set when the reported prompt size crosses the trigger,
        # acted on at the next safe point (bot turn ended, user not speaking).
        # Stays set until a compaction actually runs, so a skipped turn is
        # retried rather than dropped.
        self._context_compaction_pending: bool = False
        self._awaiting_context_compaction_seed: bool = False
        # User audio captured while the compaction reconnect is in flight.
        # Upstream discards audio whenever the session is down, so we hold it
        # here and replay it once the fresh session is seeded.
        self._compaction_audio_frames: list[Any] = []
        self._compaction_audio_bytes: int = 0

    # ------------------------------------------------------------------
    # Hooks from upstream GeminiLiveLLMService
    # ------------------------------------------------------------------

    def _should_connect_on_start(self) -> bool:
        # Hold the connection until the engine sets a system_instruction. This
        # lets pre-call fetch populate template variables first.
        return bool(self._settings.system_instruction)

    def _requires_node_transition_context_aggregation(self) -> bool:
        # A node transition replaces the current Gemini Live connection and
        # seeds the new one from our local LLMContext. Wait for the upstream
        # user aggregator to commit any final TranscriptionFrame before
        # set_node() changes the prompt and starts that reconnect.
        return True

    async def cleanup(self) -> None:
        """Cancel a delayed transition before tearing down the Live session."""
        if self._transition_function_call_task:
            await self.cancel_task(self._transition_function_call_task)
            self._transition_function_call_task = None
        self._discard_compaction_audio()
        await super().cleanup()

    async def _handle_changed_settings(self, changed: dict[str, Any]) -> set[str]:
        if "system_instruction" not in changed:
            return set()

        # PipecatEngine updates system_instruction only from set_node(). The
        # first set_node happens before a Live session exists; every later one
        # is a node transition whose tool call has already been deferred until
        # the current bot turn finishes.
        if not self._session:
            # First-time setting after deferred-connect.
            await self._connect()
        else:
            await self._reconnect_for_node_transition()
        return {"system_instruction"}

    async def _run_or_defer_function_calls(
        self, function_calls_llm: list[FunctionCallFromLLM]
    ):
        if not self._contains_node_transition(function_calls_llm):
            await super()._run_or_defer_function_calls(function_calls_llm)
            return

        # Keep a provider tool-call batch together. Splitting a mixed batch here
        # would discard Pipecat's shared function-call group and could trigger an
        # LLM run before every result from the original batch has arrived.
        if self._bot_is_responding:
            # Latest batch wins; Gemini emits tool calls as one batch per
            # tool_call message, so this overwrite is intentional.
            self._pending_node_transition_function_calls = function_calls_llm
            logger.debug(
                f"{self}: deferring {len(function_calls_llm)} node-transition "
                "function call(s) "
                "until bot turn ends"
            )
            return

        self._schedule_node_transition_function_calls(function_calls_llm)

    def _contains_node_transition(
        self, function_calls_llm: list[FunctionCallFromLLM]
    ) -> bool:
        return any(self._is_node_transition(fc) for fc in function_calls_llm)

    def _is_node_transition(self, function_call: FunctionCallFromLLM) -> bool:
        return self._function_is_node_transition(function_call.function_name)

    def _schedule_node_transition_function_calls(
        self, function_calls_llm: list[FunctionCallFromLLM]
    ) -> None:
        """Run transition calls after late input transcription has settled."""
        if (
            self._transition_function_call_task
            and not self._transition_function_call_task.done()
        ):
            logger.warning(
                f"{self}: node-transition function call already pending; "
                "ignoring duplicate batch"
            )
            return

        async def _run_after_transcription_grace() -> None:
            try:
                await asyncio.sleep(self._NODE_TRANSITION_TRANSCRIPTION_GRACE_SECONDS)
                await self._flush_pending_user_transcription()
                await self.run_function_calls(function_calls_llm)
            finally:
                self._transition_function_call_task = None

        self._transition_function_call_task = self.create_task(
            _run_after_transcription_grace(),
            name=f"{self}::node-transition-function-calls",
        )

    async def _flush_pending_user_transcription(self) -> None:
        """Publish any punctuationless user transcript before a node handoff."""
        if self._transcription_timeout_task:
            if not self._transcription_timeout_task.done():
                await self.cancel_task(self._transcription_timeout_task)
            self._transcription_timeout_task = None

        if not self._user_transcription_buffer:
            return

        text = self._user_transcription_buffer
        self._user_transcription_buffer = ""
        logger.debug(
            f"{self}: flushing pending user transcription before node transition"
        )
        await self._push_user_transcription(text, result=None)

    # ------------------------------------------------------------------
    # State-transition side effects
    # ------------------------------------------------------------------

    async def _set_bot_is_responding(self, responding: bool):
        was_responding = self._bot_is_responding
        await super()._set_bot_is_responding(responding)
        if was_responding and not responding:
            await self._run_pending_node_transition_function_calls()
            await self._maybe_compact_context()

    async def _run_pending_node_transition_function_calls(self):
        """Run any node-transition calls deferred during the bot's last turn."""
        if not self._pending_node_transition_function_calls:
            return
        fcs = self._pending_node_transition_function_calls
        self._pending_node_transition_function_calls = []
        logger.debug(
            f"{self}: executing {len(fcs)} deferred node-transition call(s) "
            "after bot turn ended"
        )
        self._schedule_node_transition_function_calls(fcs)

    async def _reconnect_for_node_transition(self) -> None:
        """Start a fresh connection and wait to seed the completed context.

        Gemini can report ``resumable=False`` while generating or executing a
        function call. A workflow transition happens at exactly that boundary,
        so using the last (older) resumption handle can omit the triggering user
        turn. Use the local LLMContext as the source of truth for this intentional
        handoff instead.
        """
        self._awaiting_node_transition_context = True
        self._node_transition_context_received = False
        self._node_transition_context_seed_started = False
        self._session_resumption_handle = None
        await self._disconnect()
        await self._connect(session_resumption_handle=None)

    # ------------------------------------------------------------------
    # Cost compaction: trade a reconnect for a cheaper context
    # ------------------------------------------------------------------

    @property
    def _context_compaction_enabled(self) -> bool:
        # A Gemini 2.5 reseed forces a recap utterance (see upstream
        # _create_initial_response), which is too intrusive to pay mid-call for a
        # cost win. On 3.x the reseed is silent, so compaction is free of UX cost.
        return CONTEXT_COMPACTION_TRIGGER_TOKENS > 0 and self._is_gemini_3

    async def _handle_msg_usage_metadata(self, message: LiveServerMessage):
        """Watch reported prompt size and flag compaction once it crosses the trigger."""
        await super()._handle_msg_usage_metadata(message)

        if not self._context_compaction_enabled or self._context_compaction_pending:
            return

        if self._awaiting_context_compaction_seed:
            # Gemini reports the finished turn's usage a moment after we start the
            # refresh, so this figure describes the context we are already
            # discarding. Acting on it would queue a second refresh against the
            # compacted session.
            return

        usage = message.usage_metadata
        prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
        if prompt_tokens >= CONTEXT_COMPACTION_TRIGGER_TOKENS:
            logger.debug(
                f"{self}: context at {prompt_tokens} tokens, over the "
                f"{CONTEXT_COMPACTION_TRIGGER_TOKENS} compaction trigger; "
                "scheduling a session refresh"
            )
            self._context_compaction_pending = True

    def _node_transition_in_flight(self) -> bool:
        return bool(
            self._pending_node_transition_function_calls
            or self._awaiting_node_transition_context
            or (
                self._transition_function_call_task
                and not self._transition_function_call_task.done()
            )
        )

    async def _maybe_compact_context(self) -> None:
        """Refresh the session if it is over budget and this is a safe moment."""
        if not self._context_compaction_pending:
            return

        if self._node_transition_in_flight():
            # A transition already reconnects and reseeds from text, which is the
            # same compaction; let it do the work.
            self._context_compaction_pending = False
            return

        if self._user_is_speaking:
            # Stay connected through the utterance and retry after the next turn.
            return

        if not self._session or self._disconnecting or self._context is None:
            return

        self._context_compaction_pending = False
        await self._compact_context_via_reconnect()

    async def _compact_context_via_reconnect(self) -> None:
        """Reconnect and reseed from LLMContext, dropping accumulated audio.

        The live session keeps spoken history as audio; LLMContext keeps it as
        transcripts. Seeding a fresh session from LLMContext therefore replaces
        the accumulated audio with its much smaller, much cheaper text form, and
        every later turn is billed against that.
        """
        logger.info(f"{self}: compacting Gemini Live context via session refresh")
        self._awaiting_context_compaction_seed = True
        self._session_resumption_handle = None
        await self._disconnect()
        await self._connect(session_resumption_handle=None)

    # ------------------------------------------------------------------
    # Frame handling: mute, TTSSpeakFrame, BotStoppedSpeakingFrame flush
    # ------------------------------------------------------------------

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
            # Greeting trigger: the engine queues a TTSSpeakFrame to start the
            # bot's first turn after node setup. Gemini Live renders its own
            # audio, so we don't pass the frame through. For configured static
            # text greetings, ask Gemini to say the exact greeting; otherwise
            # re-enter _handle_context to kick off the normal initial response.
            if not self._handled_initial_context:
                greeting_text = frame.text.strip() if frame.text else ""
                if greeting_text:
                    await self._handle_initial_greeting(self._context, greeting_text)
                else:
                    await self._handle_context(self._context)
            else:
                logger.warning(
                    f"{self}: TTSSpeakFrame after initial context already "
                    "handled — Gemini Live owns audio generation, ignoring"
                )
            return
        if isinstance(frame, BotStoppedSpeakingFrame):
            # Belt-and-suspenders: the main drain happens in
            # _set_bot_is_responding(False), but if Gemini delays turn_complete
            # past the audible end of the turn, flushing here ensures a pending
            # node transition fires promptly.
            await self._run_pending_node_transition_function_calls()
            # Fall through to super for the actual push.
        await super().process_frame(frame, direction)

    async def _send_user_audio(self, frame):
        if self._user_is_muted:
            return
        if self._awaiting_context_compaction_seed:
            self._hold_audio_during_compaction(frame)
            return
        await super()._send_user_audio(frame)

    def _hold_audio_during_compaction(self, frame) -> None:
        """Retain user audio while the compaction reconnect is in flight.

        Upstream drops audio whenever the session is down, which would cost the
        user the first word of a reply that happened to land on the refresh.
        Holding it turns that into a short delay instead.
        """
        self._compaction_audio_frames.append(frame)
        self._compaction_audio_bytes += len(frame.audio)

        if self._compaction_audio_bytes <= MAX_COMPACTION_BUFFERED_AUDIO_BYTES:
            return

        logger.warning(
            f"{self}: compaction reconnect still in flight after "
            f"{MAX_COMPACTION_BUFFERED_AUDIO_BYTES} bytes of held audio; "
            "dropping the oldest"
        )
        while (
            self._compaction_audio_bytes > MAX_COMPACTION_BUFFERED_AUDIO_BYTES
            and self._compaction_audio_frames
        ):
            dropped = self._compaction_audio_frames.pop(0)
            self._compaction_audio_bytes -= len(dropped.audio)

    async def _flush_compaction_audio(self) -> None:
        """Replay audio held during the refresh into the fresh session."""
        if not self._compaction_audio_frames:
            return

        frames = self._compaction_audio_frames
        self._compaction_audio_frames = []
        self._compaction_audio_bytes = 0
        logger.debug(
            f"{self}: replaying {len(frames)} user audio frame(s) held during compaction"
        )
        for frame in frames:
            await super()._send_user_audio(frame)

    def _discard_compaction_audio(self) -> None:
        self._compaction_audio_frames = []
        self._compaction_audio_bytes = 0

    # ------------------------------------------------------------------
    # Context lifecycle: Dograh pre-populates self._context via the engine,
    # so upstream's "first arrival === self._context is None" check doesn't
    # work. We gate on _handled_initial_context instead and skip the
    # init-instruction reconciliation (Dograh updates system_instruction at
    # runtime via _update_settings, not via init).
    # ------------------------------------------------------------------

    async def _handle_context(self, context: LLMContext):
        if self._awaiting_node_transition_context:
            self._context = context
            self._node_transition_context_received = True
            await self._maybe_seed_node_transition_context()
            return
        if not self._handled_initial_context:
            self._handled_initial_context = True
            self._context = context
            await self._create_initial_response()
        else:
            self._context = context
            await self._process_completed_function_calls(send_new_results=True)

    async def _handle_initial_greeting(self, context: LLMContext, greeting_text: str):
        """Trigger the first Gemini turn with an exact static text greeting."""
        if context is None:
            logger.warning(
                f"{self}: received initial greeting trigger before context was set"
            )
            return

        self._handled_initial_context = True
        self._context = context
        await self._create_initial_greeting_response(greeting_text)

    async def _create_initial_greeting_response(self, greeting_text: str):
        """Ask Gemini Live to speak the configured greeting exactly once."""
        if self._disconnecting:
            return

        if not self._session:
            self._pending_initial_greeting_text = greeting_text
            self._run_llm_when_session_ready = True
            return

        self._pending_initial_greeting_text = None
        prompt = format_static_greeting_prompt(greeting_text)
        turn = Content(role="user", parts=[Part(text=prompt)])

        logger.debug("Creating Gemini Live initial response from static greeting")

        await self.start_ttfb_metrics()

        try:
            await self._session.send_client_content(
                turns=[turn],
                turn_complete=True,
            )
            # Gemini 3.x also needs a realtime-input nudge to begin inference.
            if self._is_gemini_3:
                await self._session.send_realtime_input(text=" ")
        except Exception as e:
            await self._handle_send_error(e)

        self._ready_for_realtime_input = True

    # ------------------------------------------------------------------
    # Session lifecycle: drop upstream's automatic reconnect-seed and
    # initial-context-seed paths. The TTSSpeakFrame trigger and the
    # function-call-result LLMContextFrame are the only paths that should
    # kick off bot turns in the Dograh flow.
    # ------------------------------------------------------------------

    @traced_gemini_live(operation="llm_setup")
    async def _handle_session_ready(self, session):
        logger.debug(
            f"In _handle_session_ready self._run_llm_when_session_ready: {self._run_llm_when_session_ready}"
        )
        self._session = session
        if self._awaiting_node_transition_context:
            # Do not accept realtime input until the function-call result frame
            # has updated the shared context and that complete history is seeded.
            self._ready_for_realtime_input = False
            await self._maybe_seed_node_transition_context()
            return
        if self._awaiting_context_compaction_seed:
            # Compaction refresh: the context is already complete, so seed it
            # straight away. for_reconnect keeps the seed silent on Gemini 3.x,
            # which is the only place compaction is enabled.
            self._awaiting_context_compaction_seed = False
            self._ready_for_realtime_input = True
            await self._create_initial_response(for_reconnect=True)
            await self._flush_compaction_audio()
            await self._drain_pending_tool_results()
            return
        self._ready_for_realtime_input = True
        if self._run_llm_when_session_ready:
            # Context arrived before session was ready — fulfil the queued
            # initial response now.
            self._run_llm_when_session_ready = False
            if self._pending_initial_greeting_text is not None:
                await self._create_initial_greeting_response(
                    self._pending_initial_greeting_text
                )
            else:
                await self._create_initial_response()
        await self._drain_pending_tool_results()
        # Otherwise: no automatic seed. Reconnect after a session-resumption
        # update relies on the server-side restored state; reconnects without
        # a handle (e.g. node transitions before any handle was issued) are
        # followed by a function-call-result LLMContextFrame which feeds the
        # updated-context branch in _handle_context.

    async def _maybe_seed_node_transition_context(self) -> None:
        if (
            not self._awaiting_node_transition_context
            or not self._node_transition_context_received
            or not self._session
            or self._node_transition_context_seed_started
        ):
            return

        self._node_transition_context_seed_started = True
        try:
            # The complete tool result is already present in the history being
            # seeded, so mark it delivered locally instead of sending a provider
            # tool response for a call that the fresh session never issued.
            await self._process_completed_function_calls(send_new_results=False)
            await self._create_initial_response()
            self._awaiting_node_transition_context = False
            self._node_transition_context_received = False
            await self._drain_pending_tool_results()
        finally:
            self._node_transition_context_seed_started = False
