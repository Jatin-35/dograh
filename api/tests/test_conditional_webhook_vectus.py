"""Conditional Webhook end to end, for the Vectus WhatsApp flow.

A finished Vectus call goes through the real post-call path
(`_run_integrations_for_run`) against the test database: the agent carries two
Conditional Webhooks, one WhatsApp to the customer (only if they agreed and an
Area Manager was found) and one to the Area Manager (only if one was found).
The last test sends a delivery over real HTTP to a local server standing in for
the WhatsApp API, to pin exactly what it receives.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    OrganizationModel,
    UserModel,
    WebhookDeliveryModel,
    WorkflowDefinitionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.workflow.dto import ReactFlowDTO
from api.tasks.run_integrations import _run_integrations_for_run

WHATSAPP_URL = "https://whatsapp.example/send"

TO_CUSTOMER = {
    "id": "wa_customer",
    "type": "conditionalWebhook",
    "position": {"x": 600, "y": 0},
    "data": {
        "name": "WhatsApp to customer",
        "enabled": True,
        "endpoint_url": WHATSAPP_URL,
        "http_method": "POST",
        "conditions": [
            {"variable": "gathered_context.whatsapp_consent", "operator": "is_true"},
            {
                "variable": "gathered_context.area_manager_number",
                "operator": "is_not_empty",
            },
        ],
        "condition_match": "all",
        "payload_template": {
            "to": "{{initial_context.phone_number | phone_digits}}",
            "template": "area_manager_details",
            "params": [
                "{{gathered_context.area_manager_name}}",
                "{{gathered_context.area_manager_number}}",
            ],
        },
    },
}

TO_MANAGER = {
    "id": "wa_manager",
    "type": "conditionalWebhook",
    "position": {"x": 600, "y": 200},
    "data": {
        "name": "WhatsApp to Area Manager",
        "enabled": True,
        "endpoint_url": WHATSAPP_URL,
        "http_method": "POST",
        "conditions": [
            {
                "variable": "gathered_context.area_manager_number",
                "operator": "is_not_empty",
            }
        ],
        "payload_template": {
            "to": "{{gathered_context.area_manager_number | phone_digits}}",
            "template": "new_lead",
            "params": [
                "{{initial_context.customer_name}}",
                "{{initial_context.phone_number | phone_digits}}",
                "{{gathered_context.requirement}}",
                "{{gathered_context.city}}",
            ],
        },
    },
}

START = {
    "id": "start",
    "type": "startCall",
    "position": {"x": 0, "y": 0},
    "data": {"name": "Start", "prompt": "Greet the caller.", "is_start": True},
}

AGENT = {"nodes": [START, TO_CUSTOMER, TO_MANAGER], "edges": []}

CUSTOMER = {"phone_number": "+919018737669", "customer_name": "Jatin"}

FOUND = {
    "area_manager_name": "Balvinder Kumar",
    "area_manager_number": "7006485067",
    "requirement": "1000 litre water tank, 1 unit, for home",
    "city": "Samba",
}


async def _finished_call(async_session, gathered: dict) -> int:
    org = OrganizationModel(provider_id=f"vectus-{uuid4()}")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(provider_id=f"user-{uuid4()}", selected_organization_id=org.id)
    async_session.add(user)
    await async_session.flush()
    workflow = WorkflowModel(
        name="Vectus Smart Care",
        user_id=user.id,
        organization_id=org.id,
        workflow_definition=AGENT,
        template_context_variables={},
    )
    async_session.add(workflow)
    await async_session.flush()
    definition = WorkflowDefinitionModel(workflow_json=AGENT, workflow_id=workflow.id)
    async_session.add(definition)
    await async_session.flush()
    run = WorkflowRunModel(
        name="Vectus call",
        workflow_id=workflow.id,
        definition_id=definition.id,
        mode="voicelink",
        initial_context=CUSTOMER,
        gathered_context=gathered,
    )
    async_session.add(run)
    await async_session.flush()
    return run.id


async def _process(async_session, run_id: int):
    """Run the real post-call step; return the deliveries it queued."""
    enqueue = AsyncMock()
    with patch("api.tasks.arq.enqueue_job", enqueue):
        await _run_integrations_for_run(run_id)
    rows = (
        await async_session.execute(
            select(WebhookDeliveryModel)
            .where(WebhookDeliveryModel.workflow_run_id == run_id)
            .order_by(WebhookDeliveryModel.webhook_node_id)
        )
    ).scalars().all()
    return {r.webhook_node_id: r for r in rows}, enqueue


async def _annotations(async_session, run_id: int) -> dict:
    run = await async_session.get(WorkflowRunModel, run_id)
    await async_session.refresh(run)
    return run.annotations or {}


# ─── the agent saves ───────────────────────────────────────────────────────


def test_an_agent_with_both_conditional_webhooks_validates():
    dto = ReactFlowDTO.model_validate(AGENT)
    assert [n.type for n in dto.nodes] == [
        "startCall",
        "conditionalWebhook",
        "conditionalWebhook",
    ]


# ─── the four outcomes of a Vectus call ────────────────────────────────────


@pytest.mark.asyncio
async def test_agreed_and_found_sends_both_with_formatted_numbers(
    async_session, db_session
):
    run_id = await _finished_call(
        async_session, {**FOUND, "whatsapp_consent": True}
    )
    sent, enqueue = await _process(async_session, run_id)

    assert set(sent) == {"wa_customer", "wa_manager"}
    assert sent["wa_customer"].endpoint_url == WHATSAPP_URL
    assert sent["wa_customer"].payload == {
        "to": "919018737669",
        "template": "area_manager_details",
        "params": ["Balvinder Kumar", "7006485067"],
        "call_disposition": "",
    }
    assert sent["wa_manager"].payload == {
        "to": "917006485067",
        "template": "new_lead",
        "params": [
            "Jatin",
            "919018737669",
            "1000 litre water tank, 1 unit, for home",
            "Samba",
        ],
        "call_disposition": "",
    }
    assert enqueue.await_count == 2  # each queued for sending once
    notes = await _annotations(async_session, run_id)
    assert notes["conditional_webhook_wa_customer"]["sent"] is True
    assert notes["conditional_webhook_wa_manager"]["sent"] is True


@pytest.mark.asyncio
async def test_declined_sends_only_to_the_area_manager(async_session, db_session):
    run_id = await _finished_call(
        async_session, {**FOUND, "whatsapp_consent": False}
    )
    sent, _ = await _process(async_session, run_id)

    assert set(sent) == {"wa_manager"}
    notes = await _annotations(async_session, run_id)
    assert notes["conditional_webhook_wa_customer"] == {
        "name": "WhatsApp to customer",
        "sent": False,
        "reason": "conditions_not_met",
        "failed_conditions": ["gathered_context.whatsapp_consent is true"],
    }


@pytest.mark.asyncio
async def test_no_area_manager_found_sends_nothing(async_session, db_session):
    run_id = await _finished_call(
        async_session,
        {"whatsapp_consent": True, "area_manager_name": "", "city": "Unknown"},
    )
    sent, enqueue = await _process(async_session, run_id)

    assert sent == {}
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_call_that_ended_before_anything_was_extracted_sends_nothing(
    async_session, db_session
):
    run_id = await _finished_call(async_session, {})
    sent, _ = await _process(async_session, run_id)
    assert sent == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "consent,customer_gets_it",
    [("haan", True), ("Yes", True), ("ji haan", True), ("nahi", False), ("No", False)],
)
async def test_hinglish_answers_from_extraction(
    async_session, db_session, consent, customer_gets_it
):
    run_id = await _finished_call(async_session, {**FOUND, "whatsapp_consent": consent})
    sent, _ = await _process(async_session, run_id)
    assert ("wa_customer" in sent) is customer_gets_it
    assert "wa_manager" in sent


@pytest.fixture
async def real_db(setup_test_database):
    """Real commits, as in production. The duplicate guard rolls back its own
    transaction, which the shared test transaction of ``db_session`` can't
    survive, so this path needs a real session factory."""
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=True)
    original_engine, original_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, factory
    yield factory
    db_client.engine, db_client.async_session = original_engine, original_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_processing_the_same_call_twice_never_sends_twice(real_db):
    async with real_db() as session:
        run_id = await _finished_call(session, {**FOUND, "whatsapp_consent": True})
        await session.commit()

    async with real_db() as session:
        first, first_enqueue = await _process(session, run_id)
    async with real_db() as session:
        second, second_enqueue = await _process(session, run_id)  # a retried job

    assert set(first) == set(second) == {"wa_customer", "wa_manager"}
    assert {r.id for r in first.values()} == {r.id for r in second.values()}
    assert first_enqueue.await_count == 2
    second_enqueue.assert_not_awaited()  # nothing queued the second time


# ─── what the WhatsApp API actually receives ───────────────────────────────


class _WhatsAppStub(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _WhatsAppStub.received.append(
            {"path": self.path, "body": json.loads(body), "headers": dict(self.headers)}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"sent"}')

    def log_message(self, *args):
        pass


@pytest.mark.asyncio
async def test_the_whatsapp_api_receives_the_rendered_message(async_session, db_session):
    from api.tasks.webhook_delivery import deliver_webhook

    server = HTTPServer(("127.0.0.1", 0), _WhatsAppStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _WhatsAppStub.received = []
    local_url = f"http://127.0.0.1:{server.server_port}/send"

    try:
        run_id = await _finished_call(async_session, {**FOUND, "whatsapp_consent": True})
        sent, _ = await _process(async_session, run_id)
        for row in sent.values():
            row.endpoint_url = local_url  # point the queued sends at the stub
        await async_session.flush()

        for row in sent.values():
            await deliver_webhook({}, row.id)
    finally:
        server.shutdown()

    bodies = sorted((r["body"] for r in _WhatsAppStub.received), key=lambda b: b["to"])
    assert bodies == [
        {
            "to": "917006485067",
            "template": "new_lead",
            "params": [
                "Jatin",
                "919018737669",
                "1000 litre water tank, 1 unit, for home",
                "Samba",
            ],
            "call_disposition": "",
        },
        {
            "to": "919018737669",
            "template": "area_manager_details",
            "params": ["Balvinder Kumar", "7006485067"],
            "call_disposition": "",
        },
    ]
    for row in sent.values():
        await async_session.refresh(row)
        assert row.status == "succeeded"
