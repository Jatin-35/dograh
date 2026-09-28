"""Webhook Sync end to end on a real Postgres and Redis: the receiver, lead
storage, dedupe/idempotency (including concurrent requests), rate limiting,
request logs, and the organization-scoped management API."""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db import db_client
from api.db.models import OrganizationModel, UserModel, WorkflowModel
from api.db.webhook_sync_models import (
    WebhookEndpointModel,
    WebhookLeadModel,
    WebhookRequestLogModel,
)
from api.services.webhook_sync import rate_limit
from api.services.webhook_sync.auth import sign
from api.services.webhook_sync.receiver import receive_webhook

pytestmark = pytest.mark.skipif(
    "REDIS_URL" not in os.environ, reason="Requires Redis and Postgres (.env.test)"
)

JSON = {"content-type": "application/json"}


@pytest.fixture
async def env(setup_test_database):
    """db_client on the test database; two orgs, each with a user and an
    agent; a fresh Redis client for this test's event loop."""
    engine = create_async_engine(setup_test_database, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, factory
    rate_limit._redis = None

    orgs = {}
    async with factory() as session:
        for key in ("a", "b"):
            org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            user = UserModel(
                provider_id=f"test-user-{uuid.uuid4().hex[:8]}",
                selected_organization_id=org.id,
            )
            session.add(user)
            await session.flush()
            workflow = WorkflowModel(
                name=f"agent-{key}",
                user_id=user.id,
                organization_id=org.id,
                workflow_definition={"nodes": [], "edges": []},
                template_context_variables={},
            )
            session.add(workflow)
            await session.flush()
            orgs[key] = SimpleNamespace(
                org=org.id,
                workflow=workflow.id,
                user=SimpleNamespace(
                    id=user.id, selected_organization_id=org.id, is_superuser=False
                ),
            )
        await session.commit()

    async def make_endpoint(key="a", **overrides):
        fields = dict(
            organization_id=orgs[key].org,
            name="CRM",
            workflow_id=orgs[key].workflow,
            auth_type="api_key",
            secret=f"secret-{uuid.uuid4().hex}",
            is_active=True,
            auto_call=True,
            field_mapping={},
            call_settings={},
            rate_limit_per_minute=1000,
            created_by=orgs[key].user.id,
        )
        fields.update(overrides)
        return await db_client.create_webhook_endpoint(**fields)

    async def leads(endpoint):
        async with factory() as session:
            result = await session.execute(
                select(WebhookLeadModel)
                .where(WebhookLeadModel.endpoint_id == endpoint.id)
                .order_by(WebhookLeadModel.id)
            )
            return list(result.scalars().all())

    async def logs(endpoint):
        async with factory() as session:
            result = await session.execute(
                select(WebhookRequestLogModel)
                .where(WebhookRequestLogModel.endpoint_id == endpoint.id)
                .order_by(WebhookRequestLogModel.id)
            )
            return list(result.scalars().all())

    yield SimpleNamespace(
        orgs=orgs, make_endpoint=make_endpoint, leads=leads, logs=logs, factory=factory
    )

    if rate_limit._redis is not None:
        await rate_limit._redis.aclose()
        rate_limit._redis = None
    db_client.engine, db_client.async_session = original
    await engine.dispose()


async def post(endpoint, body, headers=None, query=None, raw=None):
    raw_body = raw if raw is not None else json.dumps(body).encode()
    return await receive_webhook(
        endpoint.endpoint_uuid, raw_body, headers or {}, query or {}, "10.0.0.5"
    )


def key_headers(endpoint, **extra):
    return {**JSON, "X-API-Key": endpoint.secret, **extra}


# ---------------------------------------------------------------------------
# Auth modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_lead_is_stored_with_mapped_fields(env):
    endpoint = await env.make_endpoint()
    result = await post(
        endpoint,
        {
            "name": "Rahul Kumar",
            "mobile": "98765 43210",
            "city": "Patna",
            "source": "Facebook Ads",
            "email": "rahul@example.in",
        },
        key_headers(endpoint),
    )
    assert result.status_code == 200, result.body
    assert result.body["success"] is True
    (lead,) = await env.leads(endpoint)
    assert result.body["lead_ids"] == [lead.id]
    assert (lead.phone, lead.phone_raw, lead.status) == (
        "+919876543210",
        "98765 43210",
        "received",
    )
    assert (lead.name, lead.email, lead.source) == (
        "Rahul Kumar",
        "rahul@example.in",
        "Facebook Ads",
    )
    assert lead.variables["city"] == "Patna"
    assert lead.organization_id == env.orgs["a"].org
    assert lead.raw_payload["mobile"] == "98765 43210"


@pytest.mark.asyncio
async def test_wrong_or_missing_api_key_is_401_and_logged_without_the_secret(env):
    endpoint = await env.make_endpoint()
    for headers in ({**JSON, "X-API-Key": "wrong"}, JSON):
        result = await post(endpoint, {"mobile": "9876543210"}, headers)
        assert result.status_code == 401
    assert await env.leads(endpoint) == []
    first, second = await env.logs(endpoint)
    assert first.response_code == second.response_code == 401
    # The reason is for debugging in the log; the client only sees a generic 401.
    assert (first.error, second.error) == ("Wrong API key", "Missing X-API-Key header")
    assert first.headers["x-api-key"] == "[redacted]"
    assert "wrong" not in json.dumps(first.headers)


@pytest.mark.asyncio
async def test_hmac_signature_mode(env):
    import time

    endpoint = await env.make_endpoint(auth_type="hmac")
    raw = json.dumps({"mobile": "9876543210"}).encode()
    ts = str(int(time.time()))
    signed = {
        **JSON,
        "X-Botrix-Timestamp": ts,
        "X-Botrix-Signature": sign(endpoint.secret, raw, ts),
    }
    ok = await post(endpoint, None, signed, raw=raw)
    assert ok.status_code == 200
    tampered = raw.replace(b"9876543210", b"9123456789")
    assert (await post(endpoint, None, signed, raw=tampered)).status_code == 401
    assert len(await env.leads(endpoint)) == 1


@pytest.mark.asyncio
async def test_a_captured_signed_request_cannot_be_replayed_later(env):
    import time

    endpoint = await env.make_endpoint(auth_type="hmac")
    raw = json.dumps({"mobile": "9876543210"}).encode()
    old = str(int(time.time()) - 3600)
    captured = {
        **JSON,
        "X-Botrix-Timestamp": old,
        "X-Botrix-Signature": sign(endpoint.secret, raw, old),
    }
    result = await post(endpoint, None, captured, raw=raw)
    assert result.status_code == 401
    assert await env.leads(endpoint) == []
    (log,) = await env.logs(endpoint)
    assert log.error == "Signature timestamp is more than 5 minutes off"


@pytest.mark.asyncio
async def test_url_token_mode(env):
    endpoint = await env.make_endpoint(auth_type="url_token")
    ok = await post(
        endpoint, {"mobile": "9876543210"}, JSON, {"token": endpoint.secret}
    )
    assert ok.status_code == 200
    assert (
        await post(endpoint, {"mobile": "9876543210"}, JSON, {"token": "x"})
    ).status_code == 401


# ---------------------------------------------------------------------------
# Endpoint state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_endpoint_is_404(env):
    result = await receive_webhook(str(uuid.uuid4()), b"{}", JSON, {}, None)
    assert result.status_code == 404


@pytest.mark.asyncio
async def test_paused_endpoint_is_404_and_stores_nothing(env):
    endpoint = await env.make_endpoint(is_active=False)
    result = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert result.status_code == 404
    assert await env.leads(endpoint) == []
    (log,) = await env.logs(endpoint)
    assert (log.response_code, log.error) == (404, "Endpoint is paused")


# ---------------------------------------------------------------------------
# Phone handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_number_is_stored_as_invalid_number(env):
    endpoint = await env.make_endpoint()
    result = await post(endpoint, {"mobile": "12345"}, key_headers(endpoint))
    assert result.status_code == 200
    assert result.body["leads"][0]["status"] == "invalid_number"
    (lead,) = await env.leads(endpoint)
    assert (lead.phone, lead.phone_raw, lead.status) == (
        None,
        "12345",
        "invalid_number",
    )


@pytest.mark.asyncio
async def test_missing_phone_is_422_and_stores_nothing(env):
    endpoint = await env.make_endpoint()
    result = await post(endpoint, {"name": "No Number"}, key_headers(endpoint))
    assert result.status_code == 422
    assert result.body["error"] == "phone is required"
    assert await env.leads(endpoint) == []
    (log,) = await env.logs(endpoint)
    assert log.response_code == 422


@pytest.mark.asyncio
async def test_mapping_config_is_applied(env):
    endpoint = await env.make_endpoint(
        field_mapping={
            "phone": "data.lead.contact_no",
            "name": "data.lead.full",
            "custom": {"product": "data.interest.product"},
        }
    )
    result = await post(
        endpoint,
        {
            "data": {
                "lead": {"contact_no": "+91 91234 56780", "full": "Asha"},
                "interest": {"product": "Water tank"},
            }
        },
        key_headers(endpoint),
    )
    assert result.status_code == 200
    (lead,) = await env.leads(endpoint)
    assert (lead.phone, lead.name) == ("+919123456780", "Asha")
    assert lead.variables["product"] == "Water tank"


@pytest.mark.asyncio
async def test_form_encoded_lead(env):
    endpoint = await env.make_endpoint()
    result = await post(
        endpoint,
        None,
        {
            "content-type": "application/x-www-form-urlencoded",
            "X-API-Key": endpoint.secret,
        },
        raw=b"name=Rahul+Kumar&mobile_number=09876543210",
    )
    assert result.status_code == 200
    (lead,) = await env.leads(endpoint)
    assert (lead.phone, lead.name) == ("+919876543210", "Rahul Kumar")


# ---------------------------------------------------------------------------
# Bulk, idempotency and dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_request_stores_each_lead_and_reports_bad_ones(env):
    endpoint = await env.make_endpoint()
    result = await post(
        endpoint,
        [{"mobile": "9876543210"}, {"name": "no phone"}, {"mobile": "9123456780"}],
        key_headers(endpoint),
    )
    assert result.status_code == 200
    assert len(result.body["lead_ids"]) == 2
    assert result.body["errors"] == [{"index": 1, "error": "phone is required"}]
    assert len(await env.leads(endpoint)) == 2


@pytest.mark.asyncio
async def test_bulk_request_without_any_phone_is_422(env):
    endpoint = await env.make_endpoint()
    result = await post(endpoint, [{"name": "a"}, {"name": "b"}], key_headers(endpoint))
    assert result.status_code == 422
    assert len(result.body["errors"]) == 2


@pytest.mark.asyncio
async def test_idempotency_key_replays_instead_of_storing_again(env):
    endpoint = await env.make_endpoint()
    headers = key_headers(endpoint, **{"Idempotency-Key": "crm-evt-1"})
    first = await post(endpoint, {"mobile": "9876543210"}, headers)
    again = await post(endpoint, {"mobile": "9876543210"}, headers)
    assert first.body["lead_ids"] == again.body["lead_ids"]
    assert again.body["leads"][0]["replayed"] is True
    assert len(await env.leads(endpoint)) == 1


@pytest.mark.asyncio
async def test_bulk_idempotency_key_covers_each_lead(env):
    endpoint = await env.make_endpoint()
    headers = key_headers(endpoint, **{"Idempotency-Key": "batch-7"})
    body = [{"mobile": "9876543210"}, {"mobile": "9123456780"}]
    first = await post(endpoint, body, headers)
    again = await post(endpoint, body, headers)
    assert first.body["lead_ids"] == again.body["lead_ids"]
    assert [lead.idempotency_key for lead in await env.leads(endpoint)] == [
        "batch-7#0",
        "batch-7#1",
    ]


@pytest.mark.asyncio
async def test_same_crm_lead_id_replays(env):
    endpoint = await env.make_endpoint()
    first = await post(
        endpoint, {"lead_id": "LSQ-99", "mobile": "9876543210"}, key_headers(endpoint)
    )
    again = await post(
        endpoint,
        {"lead_id": "LSQ-99", "mobile": "9876543210", "note": "update"},
        key_headers(endpoint),
    )
    assert first.body["lead_ids"] == again.body["lead_ids"]
    assert len(await env.leads(endpoint)) == 1


@pytest.mark.asyncio
async def test_same_phone_within_window_is_a_duplicate(env):
    endpoint = await env.make_endpoint()
    first = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    second = await post(endpoint, {"mobile": "+91 98765 43210"}, key_headers(endpoint))
    assert second.body["leads"][0]["status"] == "duplicate"
    original, duplicate = await env.leads(endpoint)
    assert duplicate.duplicate_of_lead_id == original.id == first.body["lead_ids"][0]


@pytest.mark.asyncio
async def test_same_phone_after_the_window_is_new(env):
    endpoint = await env.make_endpoint(call_settings={"dedupe_window_hours": 24})
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    async with env.factory() as session:
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.endpoint_id == endpoint.id)
            .values(received_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()
    again = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert again.body["leads"][0]["status"] == "received"


@pytest.mark.asyncio
async def test_dedupe_window_zero_disables_phone_dedupe(env):
    endpoint = await env.make_endpoint(call_settings={"dedupe_window_hours": 0})
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    again = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert again.body["leads"][0]["status"] == "received"


@pytest.mark.asyncio
async def test_invalid_numbers_are_never_the_original_for_dedupe(env):
    endpoint = await env.make_endpoint()
    await post(endpoint, {"mobile": "12345"}, key_headers(endpoint))
    second = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert second.body["leads"][0]["status"] == "received"


@pytest.mark.asyncio
async def test_dedupe_is_per_endpoint(env):
    one = await env.make_endpoint()
    two = await env.make_endpoint()
    await post(one, {"mobile": "9876543210"}, key_headers(one))
    other = await post(two, {"mobile": "9876543210"}, key_headers(two))
    assert other.body["leads"][0]["status"] == "received"


@pytest.mark.asyncio
async def test_concurrent_retries_with_one_idempotency_key_store_one_lead(env):
    endpoint = await env.make_endpoint()
    headers = key_headers(endpoint, **{"Idempotency-Key": "race-1"})
    results = await asyncio.gather(
        *(post(endpoint, {"mobile": "9876543210"}, headers) for _ in range(10))
    )
    assert all(r.status_code == 200 for r in results)
    assert len({r.body["lead_ids"][0] for r in results}) == 1
    assert len(await env.leads(endpoint)) == 1


@pytest.mark.asyncio
async def test_concurrent_same_phone_leads_yield_exactly_one_received(env):
    endpoint = await env.make_endpoint()
    results = await asyncio.gather(
        *(
            post(endpoint, {"mobile": "9876543210", "n": i}, key_headers(endpoint))
            for i in range(10)
        )
    )
    assert all(r.status_code == 200 for r in results)
    statuses = sorted(lead.status for lead in await env.leads(endpoint))
    assert statuses == ["duplicate"] * 9 + ["received"]


# ---------------------------------------------------------------------------
# Rate limiting and logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_rejects_with_retry_after_and_does_not_log(env):
    endpoint = await env.make_endpoint(rate_limit_per_minute=3)
    codes = [
        (
            await post(endpoint, {"mobile": f"98765432{i:02d}"}, key_headers(endpoint))
        ).status_code
        for i in range(5)
    ]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]
    limited = await post(endpoint, {"mobile": "9876543299"}, key_headers(endpoint))
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert len(await env.logs(endpoint)) == 3


@pytest.mark.asyncio
async def test_successful_request_is_logged_with_lead_ids(env):
    endpoint = await env.make_endpoint()
    result = await post(
        endpoint,
        {"mobile": "9876543210"},
        key_headers(endpoint, **{"User-Agent": "LeadSquared/1.0"}),
    )
    (log,) = await env.logs(endpoint)
    assert log.response_code == 200
    assert log.lead_ids == result.body["lead_ids"]
    assert log.ip == "10.0.0.5"
    assert log.headers["user-agent"] == "LeadSquared/1.0"
    assert log.headers["x-api-key"] == "[redacted]"
    assert json.loads(log.raw_body) == {"mobile": "9876543210"}
    assert endpoint.secret not in json.dumps(log.headers)


@pytest.mark.asyncio
async def test_storage_failure_is_500_and_safe_to_retry(env):
    endpoint = await env.make_endpoint()
    with patch.object(
        db_client, "insert_webhook_lead", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        result = await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert result.status_code == 500
    (log,) = await env.logs(endpoint)
    assert log.response_code == 500 and "RuntimeError" in log.error


@pytest.mark.asyncio
async def test_retention_job_deletes_old_logs_and_clears_old_payloads(env):
    from api.tasks.webhook_sync_tasks import cleanup_webhook_sync_data

    endpoint = await env.make_endpoint()
    await post(endpoint, {"name": "Old", "mobile": "9876543210"}, key_headers(endpoint))
    await post(endpoint, {"name": "New", "mobile": "9123456780"}, key_headers(endpoint))
    old_log, recent_log = await env.logs(endpoint)
    old_lead, recent_lead = await env.leads(endpoint)
    long_ago = datetime.now(UTC) - timedelta(days=31)
    async with env.factory() as session:
        await session.execute(
            update(WebhookRequestLogModel)
            .where(WebhookRequestLogModel.id == old_log.id)
            .values(received_at=long_ago)
        )
        await session.execute(
            update(WebhookLeadModel)
            .where(WebhookLeadModel.id == old_lead.id)
            .values(received_at=long_ago)
        )
        await session.commit()

    result = await cleanup_webhook_sync_data(None)
    assert result["request_logs_deleted"] >= 1 and result["payloads_cleared"] >= 1
    assert [log.id for log in await env.logs(endpoint)] == [recent_log.id]
    old_after, recent_after = await env.leads(endpoint)
    # The old lead keeps its fields; only the full CRM payload is gone.
    assert old_after.raw_payload is None
    assert (old_after.name, old_after.phone) == ("Old", "+919876543210")
    assert recent_after.raw_payload == {"name": "New", "mobile": "9123456780"}


@pytest.mark.asyncio
async def test_batch_reply_counts_what_happened(env):
    endpoint = await env.make_endpoint()
    await post(endpoint, {"mobile": "9000000009"}, key_headers(endpoint))
    headers = key_headers(endpoint, **{"Idempotency-Key": "k"})
    body = [
        {"mobile": "9876543210"},  # new
        {"mobile": "9000000009"},  # duplicate of the lead above
        {"mobile": "12345"},  # invalid number
        {"name": "no phone"},  # rejected
    ]
    first = await post(endpoint, body, headers)
    keys = ("received", "created", "duplicates", "invalid", "replayed", "rejected")
    assert {k: first.body[k] for k in keys} == {
        "received": 4,
        "created": 1,
        "duplicates": 1,
        "invalid": 1,
        "replayed": 0,
        "rejected": 1,
    }
    again = await post(endpoint, body, headers)
    assert (again.body["created"], again.body["replayed"]) == (0, 3)


@pytest.mark.asyncio
async def test_request_logs_record_the_response_time(env):
    endpoint = await env.make_endpoint()
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    (log,) = await env.logs(endpoint)
    assert log.duration_ms is not None and 0 <= log.duration_ms < 5000


# ---------------------------------------------------------------------------
# The HTTP route itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_route_end_to_end(env):
    from api.app import app

    endpoint = await env.make_endpoint()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            f"/api/v1/webhooks/inbound/{endpoint.endpoint_uuid}",
            json={"name": "Rahul", "mobile": "9876543210"},
            headers={"X-API-Key": endpoint.secret},
        )
        too_big = await client.post(
            f"/api/v1/webhooks/inbound/{endpoint.endpoint_uuid}",
            content=b"{}",
            headers={"X-API-Key": endpoint.secret, "Content-Length": "2000000"},
        )

        async def chunks():
            for _ in range(20):
                yield b"x" * 100_000  # 2 MB, streamed with no Content-Length

        streamed = await client.post(
            f"/api/v1/webhooks/inbound/{endpoint.endpoint_uuid}",
            content=chunks(),
            headers={"X-API-Key": endpoint.secret},
        )
    assert ok.status_code == 200 and ok.json()["success"] is True
    assert ok.json()["created"] == 1
    assert too_big.status_code == 413
    assert streamed.status_code == 413


# ---------------------------------------------------------------------------
# Management API (organization-scoped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_endpoint_requires_an_agent_in_the_callers_org(env):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointCreateRequest

    a, b = env.orgs["a"], env.orgs["b"]
    with patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    ):
        created = await routes.create_endpoint(
            WebhookEndpointCreateRequest(name="LeadSquared", workflow_id=a.workflow),
            user=a.user,
        )
        with pytest.raises(HTTPException) as exc:
            await routes.create_endpoint(
                WebhookEndpointCreateRequest(name="Steal", workflow_id=a.workflow),
                user=b.user,
            )
    assert exc.value.status_code == 404
    assert created.webhook_url == (
        f"https://voice-app.example/api/v1/webhooks/inbound/{created.endpoint_uuid}"
    )
    assert len(created.secret) >= 40
    assert created.call_settings.calling_hours.start == "09:00"


@pytest.mark.asyncio
async def test_url_token_endpoint_url_carries_the_token(env):
    from api.services.webhook_sync.management import webhook_url

    endpoint = await env.make_endpoint(auth_type="url_token")
    with patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    ):
        url = await webhook_url(endpoint)
    assert url.endswith(f"/{endpoint.endpoint_uuid}?token={endpoint.secret}")


@pytest.mark.asyncio
async def test_other_orgs_cannot_see_or_change_an_endpoint(env):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import WebhookEndpointUpdateRequest

    endpoint = await env.make_endpoint("a")
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    intruder = env.orgs["b"].user
    for call in (
        routes.get_endpoint(endpoint.id, user=intruder),
        routes.update_endpoint(
            endpoint.id, WebhookEndpointUpdateRequest(is_active=False), user=intruder
        ),
        routes.regenerate_secret(endpoint.id, user=intruder),
        routes.delete_endpoint(endpoint.id, user=intruder),
        routes.list_request_logs(endpoint.id, limit=50, offset=0, user=intruder),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404
    listed = await routes.list_leads(
        endpoint_id=None,
        status=None,
        search=None,
        received_from=None,
        received_to=None,
        limit=50,
        offset=0,
        user=intruder,
    )
    assert listed.total == 0
    (lead,) = await env.leads(endpoint)
    with pytest.raises(HTTPException):
        await routes.get_lead(lead.id, user=intruder)
    still = await db_client.get_webhook_endpoint(endpoint.id, env.orgs["a"].org)
    assert still.is_active is True


@pytest.mark.asyncio
async def test_regenerating_the_secret_retires_the_old_one(env):
    from api.routes import webhook_sync as routes

    endpoint = await env.make_endpoint()
    old = endpoint.secret
    with patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    ):
        fresh = await routes.regenerate_secret(endpoint.id, user=env.orgs["a"].user)
    assert fresh.secret != old
    assert (
        await receive_webhook(
            endpoint.endpoint_uuid,
            b'{"mobile":"9876543210"}',
            {**JSON, "X-API-Key": old},
            {},
            None,
        )
    ).status_code == 401
    assert (
        await receive_webhook(
            endpoint.endpoint_uuid,
            b'{"mobile":"9876543210"}',
            {**JSON, "X-API-Key": fresh.secret},
            {},
            None,
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_leads_are_masked_for_orgs_with_masking_on(env):
    from api.routes import webhook_sync as routes

    endpoint = await env.make_endpoint()
    await post(
        endpoint,
        {"mobile": "9876543210", "alt_mobile": "9123456780", "city": "Patna"},
        key_headers(endpoint),
    )
    (stored,) = await env.leads(endpoint)
    user = env.orgs["a"].user
    with patch.object(
        routes, "should_mask_phone_numbers", AsyncMock(return_value=True)
    ):
        listed = await routes.list_leads(
            endpoint_id=endpoint.id,
            status=None,
            search=None,
            received_from=None,
            received_to=None,
            limit=50,
            offset=0,
            user=user,
        )
        single = await routes.get_lead(stored.id, user=user)
    (lead,) = listed.leads
    assert "9876543210" not in json.dumps(lead.model_dump(mode="json"))
    assert "9123456780" not in json.dumps(single.model_dump(mode="json"))
    assert single.raw_payload is None
    assert lead.variables["city"] == "Patna"
    with patch.object(
        routes, "should_mask_phone_numbers", AsyncMock(return_value=False)
    ):
        clear = await routes.get_lead(stored.id, user=user)
    assert (
        clear.phone == "+919876543210" and clear.raw_payload["mobile"] == "9876543210"
    )


@pytest.mark.asyncio
async def test_lead_filters_and_search(env):
    from api.routes import webhook_sync as routes

    endpoint = await env.make_endpoint()
    await post(
        endpoint, {"name": "Rahul", "mobile": "9876543210"}, key_headers(endpoint)
    )
    await post(endpoint, {"name": "Asha", "mobile": "12345"}, key_headers(endpoint))
    user = env.orgs["a"].user

    async def ask(**kw):
        args = dict(
            endpoint_id=endpoint.id,
            status=None,
            search=None,
            received_from=None,
            received_to=None,
            limit=50,
            offset=0,
            user=user,
        )
        args.update(kw)
        return await routes.list_leads(**args)

    with patch.object(
        routes, "should_mask_phone_numbers", AsyncMock(return_value=False)
    ):
        assert (await ask()).total == 2
        assert [x.name for x in (await ask(status=["invalid_number"])).leads] == [
            "Asha"
        ]
        assert [x.name for x in (await ask(search="rah")).leads] == ["Rahul"]
        assert (await ask(search="98765")).total == 1
        assert (await ask(limit=1)).total == 2 and len((await ask(limit=1)).leads) == 1
        with pytest.raises(HTTPException):
            await ask(status=["bogus"])


@pytest.mark.asyncio
async def test_list_endpoints_counts_leads(env):
    from api.routes import webhook_sync as routes

    endpoint = await env.make_endpoint()
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    await post(endpoint, {"mobile": "9123456780"}, key_headers(endpoint))
    with patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    ):
        listed = await routes.list_endpoints(user=env.orgs["a"].user)
        other = await routes.list_endpoints(user=env.orgs["b"].user)
    (row,) = [e for e in listed.endpoints if e.id == endpoint.id]
    assert (row.leads_today, row.leads_total, row.workflow_name) == (2, 2, "agent-a")
    assert all(e.id != endpoint.id for e in other.endpoints)


@pytest.mark.asyncio
async def test_deleting_an_endpoint_removes_its_leads_and_logs(env):
    from api.routes import webhook_sync as routes

    endpoint = await env.make_endpoint()
    await post(endpoint, {"mobile": "9876543210"}, key_headers(endpoint))
    assert (await routes.delete_endpoint(endpoint.id, user=env.orgs["a"].user)) == {
        "success": True
    }
    assert await env.leads(endpoint) == [] and await env.logs(endpoint) == []
    async with env.factory() as session:
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(WebhookEndpointModel)
                .where(WebhookEndpointModel.id == endpoint.id)
            )
        ).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_management_action_is_audited_without_the_secret(env):
    from api.routes import webhook_sync as routes
    from api.schemas.webhook_sync import (
        WebhookEndpointCreateRequest,
        WebhookEndpointUpdateRequest,
    )

    a = env.orgs["a"]
    backend = patch(
        "api.services.webhook_sync.management.get_backend_endpoints",
        AsyncMock(return_value=("https://voice-app.example", "wss://x")),
    )
    with backend:
        created = await routes.create_endpoint(
            WebhookEndpointCreateRequest(name="LSQ", workflow_id=a.workflow),
            user=a.user,
        )
        await routes.update_endpoint(
            created.id,
            WebhookEndpointUpdateRequest(name="LeadSquared", rate_limit_per_minute=50),
            user=a.user,
        )
        await routes.update_endpoint(
            created.id, WebhookEndpointUpdateRequest(is_active=False), user=a.user
        )
        await routes.update_endpoint(
            created.id, WebhookEndpointUpdateRequest(is_active=True), user=a.user
        )
        # A save that changes nothing records nothing.
        await routes.update_endpoint(
            created.id, WebhookEndpointUpdateRequest(name="LeadSquared"), user=a.user
        )
        rotated = await routes.regenerate_secret(created.id, user=a.user)
        await routes.delete_endpoint(created.id, user=a.user)

    audit = await routes.list_audit_log(
        endpoint_id=created.id, limit=50, offset=0, user=a.user
    )
    actions = [entry.action for entry in reversed(audit.entries)]
    assert actions == [
        "created",
        "updated",
        "paused",
        "resumed",
        "secret_regenerated",
        "deleted",
    ]
    updated = next(e for e in audit.entries if e.action == "updated")
    assert updated.changes == {
        "name": {"from": "LSQ", "to": "LeadSquared"},
        "rate_limit_per_minute": {"from": 100, "to": 50},
    }
    assert all(e.user_id == a.user.id for e in audit.entries)
    # History survives the deletion, with the endpoint's name.
    assert audit.entries[0].endpoint_name == "LeadSquared"
    dumped = json.dumps([e.model_dump(mode="json") for e in audit.entries])
    assert created.secret not in dumped and rotated.secret not in dumped


@pytest.mark.asyncio
async def test_audit_log_is_organization_scoped(env):
    from api.routes import webhook_sync as routes
    from api.services.webhook_sync.management import record_audit

    endpoint = await env.make_endpoint("a")
    await record_audit(endpoint, env.orgs["a"].user.id, "created")
    theirs = await routes.list_audit_log(
        endpoint_id=endpoint.id, limit=50, offset=0, user=env.orgs["b"].user
    )
    assert theirs.total == 0
    ours = await routes.list_audit_log(
        endpoint_id=endpoint.id, limit=50, offset=0, user=env.orgs["a"].user
    )
    assert ours.total == 1
