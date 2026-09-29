"""SmartFlo inbound calls on the shared ``/inbound/run`` dispatcher.

SmartFlo's connect request carries the called number but no account id (the
login email the config is keyed by), so it must be routed by the number alone.
Before this, every SmartFlo call was refused as "number not configured": the
account-scoped lookup refuses to run without an account id. Seen live on
2026-09-28 with a correctly configured DID.
"""

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db import db_client
from api.db.models import OrganizationModel
from api.routes import telephony as telephony_routes

DID = "+917921731184"

# The shape SmartFlo sent on the live test call (numbers as seen in our log).
SMARTFLO_CONNECT = {
    "callId": "7c1f0e2a-smartflo",
    "fromNumber": "+919065594150",
    "toNumber": DID,
}


def _request(payload: dict):
    body = json.dumps(payload).encode()
    request = SimpleNamespace()
    request.body = AsyncMock(return_value=body)
    request.json = AsyncMock(return_value=payload)
    request.headers = {"content-type": "application/json"}
    request.url = "https://voice-app.example/api/v1/telephony/inbound/run"
    return request


def _route(org_id: int = 5, config_id: int = 11, workflow_id: int = 7):
    config = SimpleNamespace(id=config_id, organization_id=org_id)
    phone = SimpleNamespace(id=21, inbound_workflow_id=workflow_id)
    return config, phone


class _Dispatch:
    """Patches everything /inbound/run touches beyond routing."""

    def __init__(self, *, by_account=None, by_number=()):
        self.provider = MagicMock()
        self.provider.verify_inbound_signature = AsyncMock(return_value=True)
        self.provider.start_inbound_stream = AsyncMock(
            side_effect=lambda **kw: {"success": True, "wss_url": kw["websocket_url"]}
        )
        self.db = MagicMock()
        self.db.find_inbound_route_by_account = AsyncMock(return_value=by_account)
        self.db.find_inbound_routes_by_number = AsyncMock(return_value=list(by_number))
        self.db.get_workflow = AsyncMock(return_value=SimpleNamespace(id=7, user_id=3))
        self.concurrency = MagicMock()
        self.concurrency.acquire_org_slot = AsyncMock(return_value="slot")
        self.concurrency.bind_workflow_run = AsyncMock()
        self.concurrency.release_workflow_run_slot = AsyncMock()
        self.concurrency.release_slot = AsyncMock()

    def __enter__(self):
        self._patches = [
            patch.object(telephony_routes, "db_client", self.db),
            patch.object(
                telephony_routes,
                "get_telephony_provider_by_id",
                AsyncMock(return_value=self.provider),
            ),
            patch.object(telephony_routes, "call_concurrency", self.concurrency),
            patch.object(
                telephony_routes,
                "_create_inbound_workflow_run",
                AsyncMock(return_value=901),
            ),
            patch.object(
                telephony_routes,
                "authorize_workflow_run_start",
                AsyncMock(return_value=SimpleNamespace(has_quota=True)),
            ),
            patch.object(
                telephony_routes,
                "get_backend_endpoints",
                AsyncMock(
                    return_value=(
                        "https://voice-app.example",
                        "wss://voice-app.example",
                    )
                ),
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _body(response) -> dict:
    if isinstance(response, dict):
        return response
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_a_smartflo_call_is_routed_by_its_number_alone():
    with _Dispatch(by_number=[_route()]) as d:
        response = await telephony_routes.handle_inbound_run(_request(SMARTFLO_CONNECT))

    assert _body(response) == {
        "success": True,
        "wss_url": "wss://voice-app.example/api/v1/telephony/ws/7/5/901",
    }
    d.db.find_inbound_routes_by_number.assert_awaited_once()
    assert d.db.find_inbound_routes_by_number.await_args.kwargs["to_number"] == DID
    # The signature (shared-secret) check still runs against the matched config.
    d.provider.verify_inbound_signature.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_number_under_two_smartflo_configs_is_refused_not_guessed():
    with _Dispatch(by_number=[_route(org_id=5), _route(org_id=9, config_id=12)]) as d:
        response = await telephony_routes.handle_inbound_run(_request(SMARTFLO_CONNECT))

    assert _body(response) == {"success": False, "wss_url": "wss://declined.invalid/"}
    d.provider.start_inbound_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_number_is_still_refused():
    with _Dispatch(by_number=[]) as d:
        response = await telephony_routes.handle_inbound_run(_request(SMARTFLO_CONNECT))

    assert _body(response)["success"] is False
    d.provider.start_inbound_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_providers_that_send_an_account_id_never_route_by_number_alone():
    twilio_call = {
        "CallSid": "CA123",
        "AccountSid": "AC-other",
        "To": DID,
        "From": "+919065594150",
        "Direction": "inbound",
        "CallStatus": "ringing",
    }
    from api.services.telephony.providers.twilio.provider import TwilioProvider

    with (
        _Dispatch(by_number=[_route()]) as d,
        patch.object(
            telephony_routes, "_detect_provider", AsyncMock(return_value=TwilioProvider)
        ),
    ):
        await telephony_routes.handle_inbound_run(_request(twilio_call))

    # The account-scoped lookup ran (and missed); the number-only one did not.
    d.db.find_inbound_route_by_account.assert_awaited_once()
    d.db.find_inbound_routes_by_number.assert_not_awaited()
    d.provider.start_inbound_stream.assert_not_awaited()


# ---------------------------------------------------------------------------
# The number-only lookup itself, on a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(setup_test_database):
    engine = create_async_engine(setup_test_database, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, factory

    async def org() -> int:
        async with factory() as session:
            row = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
            session.add(row)
            await session.commit()
            return row.id

    async def number(org_id: int, provider: str, address: str, active: bool = True):
        config = await db_client.create_telephony_configuration(
            organization_id=org_id,
            name=f"{provider}-{uuid.uuid4().hex[:6]}",
            provider=provider,
            credentials={"email": f"{uuid.uuid4().hex[:6]}@example.test"},
        )
        await db_client.create_phone_number(
            organization_id=org_id,
            telephony_configuration_id=config.id,
            address=address,
            is_active=active,
        )
        return config

    yield SimpleNamespace(org=org, number=number)
    db_client.engine, db_client.async_session = original
    await engine.dispose()


pytestmark_db = pytest.mark.skipif(
    "REDIS_URL" not in os.environ, reason="Requires Postgres (.env.test)"
)


@pytestmark_db
@pytest.mark.asyncio
async def test_number_lookup_matches_across_formats_and_skips_inactive_and_other_providers(
    db,
):
    did = f"+9179{uuid.uuid4().int % 10**8:08d}"
    first = await db.org()
    config = await db.number(first, "tata_smartflo", did)
    # Same number, but inactive / another provider: not a SmartFlo route.
    await db.number(await db.org(), "tata_smartflo", did, active=False)
    await db.number(await db.org(), "twilio", did)

    found = await db_client.find_inbound_routes_by_number("tata_smartflo", did)
    assert [(c.id, p.address_normalized) for c, p in found] == [(config.id, did)]
    # SmartFlo may send the number without the "+".
    assert (
        len(await db_client.find_inbound_routes_by_number("tata_smartflo", did[1:]))
        == 1
    )

    # A second active SmartFlo config with the same number makes it ambiguous.
    await db.number(await db.org(), "tata_smartflo", did)
    assert len(await db_client.find_inbound_routes_by_number("tata_smartflo", did)) == 2


@pytest.mark.parametrize(
    "sent", ["+918065607348", "918065607348", "08065607348", "8065607348"]
)
def test_a_number_without_country_code_is_read_as_indian(sent):
    from api.services.telephony.providers.tata_smartflo.provider import (
        TataSmartfloProvider,
    )
    from api.utils.telephony_address import normalize_telephony_address

    data = TataSmartfloProvider.parse_inbound_webhook(
        {**SMARTFLO_CONNECT, "toNumber": sent}
    )
    assert normalize_telephony_address(
        data.to_number, country_hint=data.to_country
    ).canonical == ("+918065607348")


def test_the_request_fields_are_logged_by_name_never_by_value():
    from loguru import logger

    from api.services.telephony.providers.tata_smartflo.provider import (
        TataSmartfloProvider,
    )

    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}")
    try:
        TataSmartfloProvider.parse_inbound_webhook(
            {**SMARTFLO_CONNECT, "accountId": "acc-42"}
        )
    finally:
        logger.remove(sink)

    (line,) = [entry for entry in lines if "request fields" in entry]
    assert "['accountId', 'callId', 'fromNumber', 'toNumber']" in line
    for value in (*SMARTFLO_CONNECT.values(), "acc-42", "9065594150", "7921731184"):
        assert value not in line
