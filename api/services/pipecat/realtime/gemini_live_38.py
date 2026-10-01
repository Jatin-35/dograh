"""Gemini 3.8 Live, kept apart from the Gemini 3.1 path.

The 3.8 Live family changed how it runs tools, and the pipecat we ship
(dograh-hq/pipecat at the v1.5 merge) predates that. Rather than upgrade
pipecat for the whole app, this service carries the 3.8-specific behaviour,
ported from pipecat v1.12 (``GeminiLiveLLMService._tag_tool_behaviors`` and
``_tool_result``), and is selected only through its own provider
(``ServiceProviders.GEMINI_LIVE_38``). The Gemini 3.1 provider and
``DograhGeminiLiveLLMService`` are untouched.

What differs on 3.8:

- **Tools default to NON_BLOCKING.** Without an explicit ``BLOCKING``
  declaration, 3.8 keeps talking while a tool runs and answers before the
  result lands — on a Vectus call it invented the Area Manager instead of
  waiting for ``vectus_area_manager_lookup``. Synchronous tools are declared
  ``BLOCKING``; tools registered with ``cancel_on_interruption=False`` stay
  ``NON_BLOCKING`` (with the ``WHEN_IDLE`` scheduling hint on their results).
- **No nudge after a tool result.** Older 3.x needed a blank realtime-input
  nudge to continue after a tool response; 3.8 continues on its own, and the
  extra input can read as a new user turn.

Only plain ``gemini-3.8-live`` is supported here. The extended-thinking
variant runs every tool NON_BLOCKING and reports ``interaction_status``, which
needs pipecat v1.12's turn handling; use it only after that upgrade.
"""

from __future__ import annotations

from typing import Any

from google.genai.types import FunctionResponse
from loguru import logger

from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService

DEFAULT_MODEL = "gemini-3.8-live"


def tag_tool_behaviors(tools: Any, is_async) -> int:
    """Declare each function ``BLOCKING`` or ``NON_BLOCKING`` for 3.8.

    ``is_async(name)`` says whether the tool was registered with
    ``cancel_on_interruption=False``. Returns how many declarations were
    tagged. Mutates ``tools`` (Gemini's dict form) in place.
    """
    tagged = 0
    if not isinstance(tools, list):
        return tagged
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        decls = tool.get("function_declarations")
        if not isinstance(decls, list):
            continue
        for decl in decls:
            if not isinstance(decl, dict):
                continue
            name = decl.get("name")
            if not isinstance(name, str):
                continue
            decl["behavior"] = "NON_BLOCKING" if is_async(name) else "BLOCKING"
            tagged += 1
    return tagged


class DograhGemini38LiveLLMService(DograhGeminiLiveLLMService):
    """``DograhGeminiLiveLLMService`` with Gemini 3.8 Live tool semantics."""

    @property
    def _supports_non_blocking_tools(self) -> bool:
        # 3.8 supports NON_BLOCKING declarations and the scheduling hint on
        # results (older 3.x did not, which is what the parent assumes for any
        # gemini-3 model).
        return True

    async def _connection_task_handler(self, config):
        # The finished LiveConnectConfig, just before the session opens: make
        # every synchronous tool BLOCKING, since 3.8 would otherwise run it in
        # the background and answer without the result.
        tagged = tag_tool_behaviors(getattr(config, "tools", None), self._function_is_async)
        if tagged:
            logger.debug(f"{self}: declared behavior on {tagged} Gemini 3.8 tool(s)")
        return await super()._connection_task_handler(config)

    async def _tool_result(
        self, tool_call_id: str, tool_name: str, tool_result_message: dict[str, Any]
    ) -> bool:
        """Send a tool result, without the older-3.x continuation nudge.

        Same as pipecat's ``_tool_result`` at the pinned version, minus the
        trailing ``send_realtime_input(text=" ")``, matching pipecat v1.12.
        """
        if self._disconnecting or not self._session:
            logger.debug(
                f"{self}: queueing tool result for tool_call_id={tool_call_id} until session is ready"
            )
            self._pending_tool_results[tool_call_id] = (tool_name, tool_result_message)
            return False

        if self._function_is_async(tool_name):
            response_payload = {**tool_result_message, "scheduling": "WHEN_IDLE"}
        else:
            response_payload = tool_result_message

        response = FunctionResponse(name=tool_name, id=tool_call_id, response=response_payload)
        try:
            await self._session.send_tool_response(function_responses=response)
        except Exception as e:
            self._pending_tool_results[tool_call_id] = (tool_name, tool_result_message)
            await self._handle_send_error(e)
            return False

        self._pending_tool_results.pop(tool_call_id, None)
        return True
