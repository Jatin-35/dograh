"""Value translations (field_mapping.value_maps) end to end on a real Postgres
and Redis: saved and read back through the HTTP API, applied by the receiver,
carried into the queued call, the CRM callback and the mapping preview."""

# The `env` fixture is imported from the receiver tests, so test arguments
# named `env` "redefine" it; that is how pytest fixtures are shared.

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.db import db_client
from api.db.models import QueuedRunModel
from api.routes.webhook_sync import router
from api.services.auth.depends import get_user
from api.services.webhook_sync.callback import build_payload
from api.services.webhook_sync.test_lead import TEST_SOURCE, send_test_lead
from api.tests.test_webhook_sync_calling import _open_hours_endpoint
from api.tests.test_webhook_sync_receiver import env, key_headers, post  # noqa: F401

# The client's sheet: Lead source → Update as.
SOURCE_TABLE = {
    "Contact us": "Website",
    "Indiamart": "Indiamart",
    "trade india": "trade india",
    "FB Leads ad": "Facebook",
    "Inbound toll free": "Toll free",
    "landingpage": "Website",
    "Whats app": "Whats app",
}
MAPPING = {"value_maps": {"source": SOURCE_TABLE}}


def _client(env, key="a") -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_user] = lambda: env.orgs[key].user
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def no_kick():
    from api.services.webhook_sync import calling

    with patch.object(calling, "_kick", AsyncMock()) as kick:
        yield kick


# ---------------------------------------------------------------------------
# Saving through the API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_table_saved_through_the_api_reads_back_and_is_audited(env):
    endpoint = await env.make_endpoint(field_mapping={"phone": "mobile"})
    async with _client(env) as client:
        saved = await client.patch(
            f"/api/v1/webhook-sync/endpoints/{endpoint.id}",
            json={"field_mapping": {"phone": "mobile", "custom": {}, **MAPPING}},
        )
        read = await client.get(f"/api/v1/webhook-sync/endpoints/{endpoint.id}")
        audit = await client.get("/api/v1/webhook-sync/audit")

    assert saved.status_code == 200, saved.text
    assert saved.json()["field_mapping"]["value_maps"] == {"source": SOURCE_TABLE}
    # Other mapping settings survive the save.
    assert read.json()["field_mapping"]["phone"] == "mobile"
    assert read.json()["field_mapping"]["value_maps"] == {"source": SOURCE_TABLE}
    stored = await db_client.get_webhook_endpoint(endpoint.id, env.orgs["a"].org)
    assert stored.field_mapping["value_maps"]["source"]["FB Leads ad"] == "Facebook"
    updates = [e for e in audit.json()["entries"] if e["action"] == "updated"]
    assert updates and "field_mapping" in str(updates[0])


@pytest.mark.asyncio
async def test_an_endpoint_saved_before_this_feature_still_reads(env):
    endpoint = await env.make_endpoint(field_mapping={"phone": "mobile", "custom": {}})
    async with _client(env) as client:
        read = await client.get(f"/api/v1/webhook-sync/endpoints/{endpoint.id}")
    assert read.status_code == 200
    assert read.json()["field_mapping"]["phone"] == "mobile"
    assert read.json()["field_mapping"]["value_maps"] == {}


@pytest.mark.asyncio
async def test_clearing_the_table_removes_it(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    async with _client(env) as client:
        saved = await client.patch(
            f"/api/v1/webhook-sync/endpoints/{endpoint.id}",
            json={"field_mapping": {"custom": {}}},
        )
    assert saved.json()["field_mapping"]["value_maps"] == {}
    await post(endpoint, {"mobile": "9876543210", "source": "FB Leads ad"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert lead.source == "FB Leads ad"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value_maps",
    [
        {"source": {"Contact us": ""}},
        {"source": {"  ": "Website"}},
        {"": {"Contact us": "Website"}},
        {"source": {f"v{i}": "x" for i in range(201)}},
        {"source": "Website"},
    ],
)
async def test_a_bad_table_is_refused_and_the_saved_one_is_kept(env, value_maps):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    async with _client(env) as client:
        refused = await client.patch(
            f"/api/v1/webhook-sync/endpoints/{endpoint.id}",
            json={"field_mapping": {"custom": {}, "value_maps": value_maps}},
        )
    assert refused.status_code == 422
    stored = await db_client.get_webhook_endpoint(endpoint.id, env.orgs["a"].org)
    assert stored.field_mapping["value_maps"] == {"source": SOURCE_TABLE}


@pytest.mark.asyncio
async def test_another_org_cannot_change_the_table(env):
    endpoint = await env.make_endpoint("a", field_mapping=MAPPING)
    async with _client(env, "b") as client:
        refused = await client.patch(
            f"/api/v1/webhook-sync/endpoints/{endpoint.id}",
            json={"field_mapping": {"custom": {}, "value_maps": {"source": {"a": "b"}}}},
        )
    assert refused.status_code == 404
    stored = await db_client.get_webhook_endpoint(endpoint.id, env.orgs["a"].org)
    assert stored.field_mapping["value_maps"] == {"source": SOURCE_TABLE}


# ---------------------------------------------------------------------------
# Leads arriving from the CRM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent, stored",
    [
        ("Contact us", "Website"),
        ("Indiamart", "Indiamart"),
        ("trade india", "trade india"),
        ("FB Leads ad", "Facebook"),
        ("Inbound toll free", "Toll free"),
        ("landingpage", "Website"),
        ("Whats app", "Whats app"),
        # The CRM's spelling drifts; still matched.
        ("CONTACT US", "Website"),
        ("  fb leads ad ", "Facebook"),
        ("WhatsApp", "Whats app"),
        ("Landing-Page", "Website"),
        # Not in the table: kept as sent.
        ("Google Ads", "Google Ads"),
    ],
)
async def test_each_source_is_stored_translated(env, sent, stored):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    result = await post(
        endpoint,
        {"name": "Rahul", "mobile": "9876543210", "source": sent},
        key_headers(endpoint),
    )
    assert result.status_code == 200
    (lead,) = await env.leads(endpoint)
    assert lead.source == stored
    assert lead.variables["source"] == stored
    # The CRM's own value is kept in the stored payload.
    assert lead.raw_payload["source"] == sent


@pytest.mark.asyncio
async def test_a_batch_translates_every_lead(env):
    endpoint = await env.make_endpoint(
        field_mapping=MAPPING, call_settings={"dedupe_window_hours": 0}
    )
    batch = [
        {"mobile": f"98765432{i:02d}", "source": source}
        for i, source in enumerate(SOURCE_TABLE)
    ]
    result = await post(endpoint, batch, key_headers(endpoint))
    assert result.status_code == 200
    leads = await env.leads(endpoint)
    assert [lead.source for lead in leads] == list(SOURCE_TABLE.values())


@pytest.mark.asyncio
async def test_form_posts_and_leadsquared_fields_are_translated(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    await post(
        endpoint,
        None,
        {**key_headers(endpoint), "content-type": "application/x-www-form-urlencoded"},
        raw=b"mobile=9876543210&source=FB+Leads+ad",
    )
    other = await env.make_endpoint(
        field_mapping={"value_maps": {"source": SOURCE_TABLE, "city": {"BLR": "Bengaluru"}}}
    )
    await post(
        other,
        {"Current": {"Phone": "+91-9123456780", "Source": "Contact us", "mx_City": "blr"}},
        key_headers(other),
    )
    (form_lead,) = await env.leads(endpoint)
    (lsq_lead,) = await env.leads(other)
    assert form_lead.source == "Facebook"
    assert lsq_lead.source == "Website"
    assert lsq_lead.variables["city"] == "Bengaluru"


@pytest.mark.asyncio
async def test_an_explicitly_mapped_source_path_is_translated(env):
    endpoint = await env.make_endpoint(
        field_mapping={"source": "meta.channel", **MAPPING}
    )
    await post(
        endpoint,
        {"mobile": "9876543210", "meta": {"channel": "Inbound toll free"}},
        key_headers(endpoint),
    )
    (lead,) = await env.leads(endpoint)
    assert lead.source == "Toll free"


@pytest.mark.asyncio
async def test_only_mapped_still_gives_the_agent_the_translated_source(env):
    endpoint = await env.make_endpoint(
        field_mapping={"only_mapped": True, **MAPPING}
    )
    await post(
        endpoint,
        {"mobile": "9876543210", "source": "FB Leads ad", "junk": "x"},
        key_headers(endpoint),
    )
    (lead,) = await env.leads(endpoint)
    assert lead.variables == {"source": "Facebook"}


@pytest.mark.asyncio
async def test_a_lead_without_a_source_is_unaffected(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    await post(endpoint, {"name": "Asha", "mobile": "9876543210"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    assert lead.source is None and "source" not in lead.variables


@pytest.mark.asyncio
async def test_the_dashboards_test_lead_is_kept_as_sent(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    await send_test_lead(endpoint, phone="9876543210")
    (lead,) = await env.leads(endpoint)
    assert lead.source == TEST_SOURCE


# ---------------------------------------------------------------------------
# What the call and the CRM get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_queued_call_gets_the_translated_source(env, no_kick):
    endpoint = await _open_hours_endpoint(env, field_mapping=MAPPING)
    result = await post(
        endpoint,
        {"name": "Rahul", "mobile": "9876543210", "source": "FB Leads ad"},
        key_headers(endpoint),
    )
    assert result.status_code == 200
    endpoint = await db_client.get_webhook_endpoint(endpoint.id, endpoint.organization_id)
    async with env.factory() as session:
        (run,) = (
            await session.execute(
                select(QueuedRunModel).where(
                    QueuedRunModel.campaign_id == endpoint.campaign_id
                )
            )
        ).scalars().all()
    assert run.context_variables["source"] == "Facebook"
    assert run.context_variables["name"] == "Rahul"


@pytest.mark.asyncio
async def test_the_crm_callback_carries_the_stored_source(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    await post(endpoint, {"mobile": "9876543210", "source": "landingpage"}, key_headers(endpoint))
    (lead,) = await env.leads(endpoint)
    payload = build_payload(lead, endpoint, None)
    assert payload["lead"]["source"] == "Website"


@pytest.mark.asyncio
async def test_leads_list_and_csv_show_the_translated_source(env):
    endpoint = await env.make_endpoint(field_mapping=MAPPING)
    await post(endpoint, {"mobile": "9876543210", "source": "FB Leads ad"}, key_headers(endpoint))
    async with _client(env) as client:
        listed = await client.get(
            "/api/v1/webhook-sync/leads", params={"endpoint_id": endpoint.id}
        )
        csv = await client.get(
            "/api/v1/webhook-sync/leads/export", params={"endpoint_id": endpoint.id}
        )
    assert listed.status_code == 200, listed.text
    assert listed.json()["leads"][0]["source"] == "Facebook"
    assert csv.status_code == 200 and "Facebook" in csv.text


# ---------------------------------------------------------------------------
# The mapping screen's preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_preview_route_shows_the_translation_without_storing(env):
    endpoint = await env.make_endpoint()
    async with _client(env) as client:
        preview = await client.post(
            "/api/v1/webhook-sync/mapping-preview",
            json={
                "sample": '{"name": "Rahul", "mobile": "9876543210", "source": "Inbound toll free"}',
                "content_type": "application/json",
                "field_mapping": {"custom": {}, **MAPPING},
            },
        )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["fields"]["source"] == "Toll free"
    assert body["variables"]["source"] == "Toll free"
    assert await env.leads(endpoint) == []
