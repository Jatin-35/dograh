from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from api.services.integrations.base import IntegrationNodeRegistration
from api.services.workflow.dto import CustomHeaderDTO
from api.services.workflow.node_data import BaseNodeData
from api.services.workflow.node_specs._base import (
    GraphConstraints,
    NodeCategory,
    NodeExample,
    PropertyOption,
    PropertyType,
)
from api.services.workflow.node_specs.model_spec import (
    build_spec,
    node_spec,
    spec_field,
)

from .conditions import Operator

TYPE_NAME = "conditionalWebhook"


class WebhookConditionDTO(BaseModel):
    variable: str = spec_field(
        ...,
        ui_type=PropertyType.string,
        display_name="Variable",
        description=(
            "Value from the call to check, e.g. gathered_context.whatsapp_consent "
            "or initial_context.phone_number."
        ),
        placeholder="gathered_context.whatsapp_consent",
        required=True,
    )
    operator: Operator = spec_field(
        "is_true",
        ui_type=PropertyType.options,
        display_name="Check",
        description="How to test the variable.",
        options=[
            PropertyOption(value="is_true", label="is yes / true"),
            PropertyOption(value="is_false", label="is no / false"),
            PropertyOption(value="equals", label="equals"),
            PropertyOption(value="not_equals", label="does not equal"),
            PropertyOption(value="contains", label="contains"),
            PropertyOption(value="is_empty", label="is empty"),
            PropertyOption(value="is_not_empty", label="is not empty"),
        ],
        required=True,
    )
    value: Optional[str] = spec_field(
        default=None,
        ui_type=PropertyType.string,
        display_name="Value",
        description="Only for equals / does not equal / contains.",
    )


@node_spec(
    name=TYPE_NAME,
    display_name="Conditional Webhook",
    description="Send an HTTP request after the call, only if its conditions hold.",
    llm_hint=(
        "Like the Webhook node, but sent only when its 'Send only if' rules hold "
        "for the finished call. Each rule checks a variable (usually an extracted "
        "`gathered_context.*` value or an `initial_context.*` field) with is_true, "
        "is_false, equals, not_equals, contains, is_empty or is_not_empty. "
        "`condition_match` is 'all' or 'any'. The payload is templated like the "
        "Webhook node's. Not connected to other nodes."
    ),
    category=NodeCategory.integration,
    icon="GitBranch",
    examples=[
        NodeExample(
            name="whatsapp_on_consent",
            data={
                "name": "WhatsApp to customer",
                "enabled": True,
                "conditions": [
                    {
                        "variable": "gathered_context.whatsapp_consent",
                        "operator": "is_true",
                    }
                ],
                "condition_match": "all",
                "http_method": "POST",
                "endpoint_url": "https://api.example.com/whatsapp/send",
                "payload_template": {
                    "to": "{{initial_context.phone_number | phone_digits}}",
                    "manager": "{{gathered_context.area_manager_name}}",
                },
            },
        )
    ],
    graph_constraints=GraphConstraints(
        min_incoming=0, max_incoming=0, min_outgoing=0, max_outgoing=0
    ),
    # endpoint_url first: the canvas card previews the first text setting.
    property_order=(
        "name",
        "enabled",
        "endpoint_url",
        "http_method",
        "conditions",
        "condition_match",
        "credential_uuid",
        "custom_headers",
        "payload_template",
    ),
    field_overrides={
        "name": {
            "spec_default": "Conditional Webhook",
            "description": "Short identifier shown in the canvas and run logs.",
        },
        "enabled": {
            "display_name": "Enabled",
            "description": "When false, the webhook is never sent.",
        },
        "conditions": {
            "display_name": "Send only if",
            "description": (
                "Rules checked against the finished call. With no rules it is "
                "always sent, like a normal Webhook."
            ),
        },
        "condition_match": {
            "display_name": "Match",
            "description": "All rules must hold, or any one of them.",
            "spec_default": "all",
        },
        "http_method": {
            "display_name": "HTTP Method",
            "description": "HTTP verb used for the outbound request.",
            "spec_default": "POST",
        },
        "endpoint_url": {
            "display_name": "Endpoint URL",
            "description": "URL the request is sent to.",
            "placeholder": "https://api.example.com/webhook",
        },
        "credential_uuid": {
            "display_name": "Authentication",
            "description": "Optional credential applied as the Authorization header.",
            "llm_hint": "Credential UUID from `list_credentials`.",
        },
        "custom_headers": {
            "display_name": "Custom Headers",
            "description": "Additional HTTP headers to include with the request.",
        },
        "payload_template": {
            "display_name": "Payload Template",
            "description": (
                "JSON body of the request, rendered against the run context: "
                "`{{workflow_run_id}}`, `{{gathered_context.foo}}`, "
                "`{{initial_context.phone_number | phone_digits}}`, etc."
            ),
            "spec_default": {
                "call_id": "{{workflow_run_id}}",
                "phone": "{{initial_context.phone_number | phone_digits}}",
            },
        },
    },
)
class ConditionalWebhookNodeData(BaseNodeData):
    enabled: bool = spec_field(default=True, ui_type=PropertyType.boolean)
    conditions: Optional[list[WebhookConditionDTO]] = spec_field(default=None)
    condition_match: Literal["all", "any"] = spec_field(
        default="all",
        ui_type=PropertyType.options,
        options=[
            PropertyOption(value="all", label="All rules"),
            PropertyOption(value="any", label="Any rule"),
        ],
    )
    http_method: Optional[str] = spec_field(
        default=None,
        ui_type=PropertyType.options,
        options=[
            PropertyOption(value=m, label=m)
            for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
        ],
    )
    endpoint_url: Optional[str] = spec_field(default=None, ui_type=PropertyType.url)
    credential_uuid: Optional[str] = spec_field(
        default=None, ui_type=PropertyType.credential_ref
    )
    custom_headers: Optional[list[CustomHeaderDTO]] = spec_field(default=None)
    payload_template: Optional[dict] = spec_field(
        default=None, ui_type=PropertyType.json
    )


SPEC = build_spec(ConditionalWebhookNodeData)

NODE = IntegrationNodeRegistration(
    type_name=TYPE_NAME,
    data_model=ConditionalWebhookNodeData,
    node_spec=SPEC,
)
