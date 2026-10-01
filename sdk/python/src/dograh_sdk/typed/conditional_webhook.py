"""GENERATED — do not edit by hand.

Regenerate with `python -m dograh_sdk.codegen` against the target
Dograh backend. Source of truth: the backend's model-backed node-spec
catalog served from `/api/v1/node-types`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Optional

from dograh_sdk.typed._base import TypedNode


@dataclass(kw_only=True)
class ConditionalWebhook_ConditionsRow:
    """
    Rules checked against the finished call. With no rules it is always
    sent, like a normal Webhook.
    """

    variable: str
    """
    Value from the call to check, e.g. gathered_context.whatsapp_consent or
    initial_context.phone_number.
    """
    operator: Literal['is_true', 'is_false', 'equals', 'not_equals', 'contains', 'is_empty', 'is_not_empty'] = 'is_true'
    """
    How to test the variable.
    """
    value: Optional[str] = None
    """
    Only for equals / does not equal / contains.
    """
@dataclass(kw_only=True)
class ConditionalWebhook_Custom_headersRow:
    """
    Additional HTTP headers to include with the request.
    """

    key: str
    """
    HTTP header name (e.g., 'X-Source').
    """
    value: str
    """
    Header value (supports {{template_variables}}).
    """

@dataclass(kw_only=True)
class ConditionalWebhook(TypedNode):
    """
    Send an HTTP request after the call, only if its conditions hold.  LLM
    hint: Like the Webhook node, but sent only when its 'Send only if' rules
    hold for the finished call. Each rule checks a variable (usually an
    extracted `gathered_context.*` value or an `initial_context.*` field)
    with is_true, is_false, equals, not_equals, contains, is_empty or
    is_not_empty. `condition_match` is 'all' or 'any'. The payload is
    templated like the Webhook node's. Not connected to other nodes.
    """

    type: ClassVar[str] = 'conditionalWebhook'

    name: str = 'Conditional Webhook'
    """
    Short identifier shown in the canvas and run logs.
    """

    enabled: bool = True
    """
    When false, the webhook is never sent.
    """

    endpoint_url: Optional[str] = None
    """
    URL the request is sent to.
    """

    http_method: Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] = 'POST'
    """
    HTTP verb used for the outbound request.
    """

    conditions: list[ConditionalWebhook_ConditionsRow] = field(default_factory=list)
    """
    Rules checked against the finished call. With no rules it is always
    sent, like a normal Webhook.
    """

    condition_match: Literal['all', 'any'] = 'all'
    """
    All rules must hold, or any one of them.
    """

    credential_uuid: Optional[str] = None
    """
    Optional credential applied as the Authorization header.
    """

    custom_headers: list[ConditionalWebhook_Custom_headersRow] = field(default_factory=list)
    """
    Additional HTTP headers to include with the request.
    """

    payload_template: dict[str, Any] = field(default_factory=lambda: {'call_id': '{{workflow_run_id}}', 'phone': '{{initial_context.phone_number | phone_digits}}'})
    """
    JSON body of the request, rendered against the run context:
    `{{workflow_run_id}}`, `{{gathered_context.foo}}`,
    `{{initial_context.phone_number | phone_digits}}`, etc.
    """

