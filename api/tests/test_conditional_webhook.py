"""The Conditional Webhook node: sent after the call only when its rules hold.

Built for sending WhatsApp messages deterministically (one to the customer when
they agreed, one to the Area Manager when a lead was found) instead of trusting
the model to call a tool mid-conversation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.integrations.base import IntegrationCompletionContext
from api.services.integrations.conditional_webhook.completion import run_completion
from api.services.integrations.conditional_webhook.conditions import (
    conditions_met,
    resolve_variable,
    rule_holds,
)
from api.services.integrations.conditional_webhook.node import (
    ConditionalWebhookNodeData,
    WebhookConditionDTO,
)
from api.services.workflow.dto import get_node_data_model

CALL = {
    "initial_context": {"phone_number": "+919018737669", "customer_name": "Jatin"},
    "gathered_context": {
        "whatsapp_consent": "yes",
        "area_manager_name": "Balvinder Kumar",
        "area_manager_number": "7006485067",
        "city": "Samba",
        "callback_requested": False,
        "products": ["Water tank", "Bath-ware"],
        "notes": "",
    },
}


def rule(variable, operator, value=None):
    return WebhookConditionDTO(variable=variable, operator=operator, value=value)


# ─── single rules ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "variable,operator,value,expected",
    [
        ("gathered_context.whatsapp_consent", "is_true", None, True),
        ("gathered_context.callback_requested", "is_false", None, True),
        ("gathered_context.callback_requested", "is_true", None, False),
        ("gathered_context.city", "equals", "samba", True),  # case-insensitive
        ("gathered_context.city", "not_equals", "Jammu", True),
        ("gathered_context.products", "contains", "water tank", True),
        ("gathered_context.area_manager_name", "contains", "balvinder", True),
        ("gathered_context.area_manager_number", "is_not_empty", None, True),
        ("gathered_context.notes", "is_empty", None, True),
        ("gathered_context.missing", "is_empty", None, True),
        ("gathered_context.missing", "is_true", None, False),
        ("gathered_context.missing", "is_false", None, False),
        ("gathered_context.missing", "equals", "x", False),
        ("initial_context.phone_number", "is_not_empty", None, True),
    ],
)
def test_rules(variable, operator, value, expected):
    assert rule_holds(rule(variable, operator, value), CALL) is expected


@pytest.mark.parametrize("value", [True, "true", "Yes", " y ", "1"])
def test_yes_values(value):
    assert rule_holds(rule("v", "is_true"), {"gathered_context": {"v": value}})


@pytest.mark.parametrize("value", [False, "false", "No", "0", None, "", "maybe"])
def test_not_yes_values(value):
    assert not rule_holds(rule("v", "is_true"), {"gathered_context": {"v": value}})


def test_variable_forms():
    assert resolve_variable("whatsapp_consent", CALL) == "yes"  # bare name
    assert resolve_variable("{{gathered_context.city}}", CALL) == "Samba"
    assert resolve_variable("phone_number", CALL) == "+919018737669"
    assert resolve_variable("", CALL) is None


# ─── all / any ─────────────────────────────────────────────────────────────


def test_all_and_any():
    good = rule("gathered_context.whatsapp_consent", "is_true")
    bad = rule("gathered_context.city", "equals", "Delhi")
    assert conditions_met([good], "all", CALL) == (True, [])
    met, failed = conditions_met([good, bad], "all", CALL)
    assert not met and failed == ["gathered_context.city equals 'Delhi'"]
    assert conditions_met([good, bad], "any", CALL)[0] is True
    assert conditions_met([bad], "any", CALL)[0] is False
    assert conditions_met([], "all", CALL) == (True, [])  # no rules: always send


# ─── the node and its post-call run ────────────────────────────────────────


def test_node_type_is_registered():
    assert get_node_data_model("conditionalWebhook") is ConditionalWebhookNodeData


def _node(node_id, **data):
    base = {
        "name": node_id,
        "http_method": "POST",
        "endpoint_url": "https://wa.example/send",
        "payload_template": {"to": "{{initial_context.phone_number | phone_digits}}"},
    }
    return {"id": node_id, "type": "conditionalWebhook", "data": {**base, **data}}


async def _run(nodes):
    context = IntegrationCompletionContext(
        workflow_run_id=601,
        workflow_run=SimpleNamespace(),
        workflow_definition={"nodes": nodes},
        definition_id=1,
        organization_id=5,
        public_token=None,
    )
    enqueue = AsyncMock()
    notes = AsyncMock(return_value=True)
    with patch(
        "api.tasks.run_integrations._build_render_context", return_value=CALL
    ), patch("api.tasks.run_integrations._enqueue_webhook_delivery", enqueue), patch(
        "api.services.integrations.conditional_webhook.completion.db_client"
        ".patch_workflow_run_annotation",
        notes,
    ):
        results = await run_completion(nodes, context)
    _run.notes = notes
    return results, enqueue


@pytest.mark.asyncio
async def test_sends_only_the_webhooks_whose_conditions_hold():
    customer = _node(
        "wa_customer",
        conditions=[{"variable": "gathered_context.whatsapp_consent", "operator": "is_true"}],
    )
    manager = _node(
        "wa_manager",
        conditions=[{"variable": "gathered_context.area_manager_number", "operator": "is_not_empty"}],
    )
    skipped = _node(
        "wa_delhi",
        conditions=[{"variable": "gathered_context.city", "operator": "equals", "value": "Delhi"}],
    )
    off = _node("wa_off", enabled=False)

    results, enqueue = await _run([customer, manager, skipped, off])

    sent = [c.kwargs["webhook_node_id"] for c in enqueue.await_args_list]
    assert sent == ["wa_customer", "wa_manager"]
    first = enqueue.await_args_list[0].kwargs
    assert first["organization_id"] == 5 and first["workflow_run_id"] == 601
    assert first["webhook_data"].endpoint_url == "https://wa.example/send"
    assert first["webhook_data"].payload_template == {
        "to": "{{initial_context.phone_number | phone_digits}}"
    }
    # A sent node writes its own "queued" note (only if absent) before it is
    # queued, and is not in the returned results, which the caller stores later.
    request = {
        "method": "POST",
        "url": "https://wa.example/send",
        "payload": {"to": "919018737669"},  # rendered, exactly as sent
    }
    assert [c.args for c in _run.notes.await_args_list] == [
        (601, "conditional_webhook_wa_customer",
         {"name": "wa_customer", "status": "queued", "sent": False, "request": request}),
        (601, "conditional_webhook_wa_manager",
         {"name": "wa_manager", "status": "queued", "sent": False, "request": request}),
    ]
    assert all(c.kwargs == {"create": True} for c in _run.notes.await_args_list)
    assert "conditional_webhook_wa_customer" not in results
    assert results["conditional_webhook_wa_delhi"] == {
        "name": "wa_delhi",
        "status": "not_sent",
        "sent": False,
        "reason": "conditions_not_met",
        "failed_conditions": ["gathered_context.city equals 'Delhi'"],
    }
    assert results["conditional_webhook_wa_off"]["reason"] == "disabled"


@pytest.mark.asyncio
async def test_an_invalid_node_is_skipped_not_fatal():
    broken = {
        "id": "bad",
        "type": "conditionalWebhook",
        "data": {"name": "x", "conditions": [{"operator": "is_true"}]},  # no variable
    }
    results, enqueue = await _run([broken, _node("ok")])
    assert results["conditional_webhook_bad"]["reason"] == "invalid_configuration"
    assert [c.kwargs["webhook_node_id"] for c in enqueue.await_args_list] == ["ok"]


def test_the_api_key_in_a_whatsapp_url_is_masked():
    from api.services.integrations.conditional_webhook.outcome import mask_url

    url = (
        "https://pannel.ailifebot.com/API_V2/Whatsapp/send_template/"
        "Y0xLWDhOckNvd2dmbXBuSkYyOUwrQT09"
    )
    assert mask_url(url) == (
        "https://pannel.ailifebot.com/API_V2/Whatsapp/send_template/Y0xL••••"
    )
    assert mask_url("https://x.example/send?api_key=abcdef123&to=91") == (
        "https://x.example/send?api_key=abcd••••&to=91"
    )
    assert mask_url("https://wa.example/send") == "https://wa.example/send"
