"""A WhatsApp tool's ``sender_phone`` comes from the caller, formatted by the
server, never from the model.

Seen on 2026-09-29: with ``sender_phone`` agent-filled, the agent asked the
caller for their number instead of sending, and the API wants ``919018737669``
while the call context holds ``+919018737669``.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.services.workflow.tools.custom_tool import (
    execute_http_tool,
    tool_to_function_schema,
)
from api.tests.test_custom_tools import MockToolModel

SEND_WHATSAPP = MockToolModel(
    tool_uuid="wa-1",
    name="send_area_manager_whatsapp",
    description="Send the Area Manager's details on WhatsApp",
    category="http_api",
    definition={
        "schema_version": 1,
        "type": "http_api",
        "config": {
            "method": "POST",
            "url": "https://whatsapp.example/send",
            "timeout_ms": 5000,
            "parameters": [
                {"name": "manager_name", "type": "string", "required": True},
                {"name": "manager_phone", "type": "string", "required": True},
            ],
            "preset_parameters": [
                {
                    "name": "sender_phone",
                    "type": "string",
                    "value_template": "{{caller_number | phone_digits}}",
                    "required": True,
                }
            ],
        },
    },
)

AGENT_ARGS = {"manager_name": "Balvinder Kumar", "manager_phone": "7006485067"}


async def _send(call_context_vars):
    with patch(
        "api.services.workflow.tools.custom_tool.httpx.AsyncClient"
    ) as client_class:
        client = AsyncMock()
        response = Mock(status_code=200)
        response.json.return_value = {"sent": True}
        client.request.return_value = response
        client_class.return_value.__aenter__.return_value = client
        result = await execute_http_tool(
            SEND_WHATSAPP, dict(AGENT_ARGS), call_context_vars=call_context_vars
        )
    return result, client.request


def test_the_model_is_never_asked_for_the_number():
    props = tool_to_function_schema(SEND_WHATSAPP)["function"]["parameters"][
        "properties"
    ]
    assert set(props) == {"manager_name", "manager_phone"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller_number", ["+919018737669", "919018737669", "9018737669"]
)
async def test_the_callers_number_is_sent_as_digits_with_91(caller_number):
    result, request = await _send(
        {"caller_number": caller_number, "direction": "inbound"}
    )
    assert result["status"] == "success"
    assert request.await_args.kwargs["json"] == {
        **AGENT_ARGS,
        "sender_phone": "919018737669",
    }


@pytest.mark.asyncio
async def test_without_a_caller_number_nothing_is_sent():
    result, request = await _send({"direction": "inbound"})
    assert result["status"] == "error"
    assert "sender_phone" in result["error"]
    request.assert_not_awaited()
