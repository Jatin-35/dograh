"""Dograh-specific Gemini adapter customizations."""

from typing import Any, cast

from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.adapters.services.gemini_adapter import GeminiLLMAdapter
from pipecat.processors.aggregators.llm_context import (
    LLMContextMessage,
    LLMSpecificMessage,
)


class DograhGeminiJSONSchemaAdapter(GeminiLLMAdapter):
    """Use Gemini's full JSON Schema tool parameter field.

    Pipecat's default Gemini adapter maps ``FunctionSchema.parameters`` into
    ``FunctionDeclaration.parameters``, which is backed by Google GenAI's
    stricter OpenAPI-style ``Schema`` model. MCP and imported tools may contain
    valid JSON Schema keywords such as ``const`` and ``not`` that are rejected
    by that model. ``parameters_json_schema`` is the Google GenAI field intended
    for full JSON Schema payloads.
    """

    def to_provider_tools_format(
        self, tools_schema: ToolsSchema
    ) -> list[dict[str, Any]]:
        functions_schema = tools_schema.standard_tools
        if functions_schema:
            formatted_functions = []
            for func in functions_schema:
                func_dict = func.to_default_dict()
                parameters = func_dict.pop("parameters")
                func_dict["parameters_json_schema"] = parameters
                formatted_functions.append(func_dict)
            formatted_standard_tools = [{"function_declarations": formatted_functions}]
        else:
            formatted_standard_tools = []

        custom_gemini_tools = []
        if tools_schema.custom_tools:
            custom_gemini_tools = tools_schema.custom_tools.get(AdapterType.GEMINI, [])

        return formatted_standard_tools + custom_gemini_tools


class DograhGeminiLiveJSONSchemaAdapter(DograhGeminiJSONSchemaAdapter):
    """The JSON Schema adapter for Gemini Live, with tool history sent as text.

    Gemini Live's ``send_client_content`` accepts only text and media parts. A
    workflow node change opens a fresh session and seeds it from the context,
    which by then holds the transition call (``move_to_main_agenda``) and any
    lookups with their results. Sent as function-call / function-response
    parts they can close the connection with a 1007 or leave the model without
    the conversation it was seeded with. Each call and result is summarized as
    a text turn instead, ported from pipecat's ``GeminiLiveLLMAdapter``
    (pipecat 48c0e281, fixing 1007 on handoff, ec11fef1).

    Only the Live services use this; the regular Gemini LLM keeps real
    function-call parts.
    """

    def _from_universal_context_messages(
        self,
        universal_context_messages: list[LLMContextMessage],
        *,
        system_instruction: str | None = None,
    ) -> GeminiLLMAdapter.ConvertedMessages:
        return super()._from_universal_context_messages(
            convert_tool_calls_to_text(universal_context_messages),
            system_instruction=system_instruction,
        )


def convert_tool_calls_to_text(
    messages: list[LLMContextMessage],
) -> list[LLMContextMessage]:
    """Replace each tool call and tool result with a text message describing it."""
    tool_call_names: dict[str, str] = {}
    converted: list[LLMContextMessage] = []
    for message in messages:
        if isinstance(message, LLMSpecificMessage):
            converted.append(message)
            continue
        msg = cast(dict[str, Any], message)
        if msg.get("tool_calls"):
            # Keep what the assistant said alongside the call; upstream drops it.
            if msg.get("content"):
                converted.append({"role": "assistant", "content": msg["content"]})
            summaries = []
            for tool_call in msg["tool_calls"]:
                function = tool_call["function"]
                tool_call_names[tool_call["id"]] = function["name"]
                summaries.append(
                    f"[Called function {function['name']} with args {function['arguments']}]"
                )
            converted.append({"role": "assistant", "content": " ".join(summaries)})
        elif msg.get("role") == "tool":
            # Same fallback name the Gemini adapter uses for a result whose
            # call isn't in the context.
            name = tool_call_names.get(msg.get("tool_call_id", ""), "tool_call_result")
            converted.append(
                {"role": "user", "content": f"[Function {name} returned {msg['content']}]"}
            )
        else:
            converted.append(message)
    return converted
