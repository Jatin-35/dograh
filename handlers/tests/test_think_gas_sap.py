"""SAP complaint handler.

The tests that matter here are the multi-Set-Cookie case and the concurrency
case. Both pass trivially with a hand-rolled cookie parser or a shared client
right up until they fail in production, which is exactly the kind of bug worth
paying for in advance.
"""

import asyncio
import json

import httpx
import pytest

from handlers.integrations import think_gas_sap as sap

VALID_EVENT = {
    "ImPartner": "5060000019",
    "ImQcode": "QPPO",
    "ImNotes": "Recharge issue. Balance not updated after recharge.",
    "ImTicketcreationtag": "voicebot",
}


@pytest.fixture(autouse=True)
def _sap_credentials(monkeypatch):
    monkeypatch.setenv("SAP_BASIC_AUTH_HEADER", "Basic ZmFrZTpmYWtl")


def _install_transport(monkeypatch, responder):
    """Route the handler's client at a mock transport, keeping its real cookie
    jar so cookie handling is genuinely exercised rather than stubbed."""
    real_new_client = sap._new_client

    def _patched():
        client = real_new_client()
        client._transport = httpx.MockTransport(responder)
        return client

    monkeypatch.setattr(sap, "_new_client", _patched)


def _created_body(ticket="1000012345"):
    return json.dumps({"d": {"ExObjectId": ticket}})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creates_a_complaint_and_returns_a_speakable_reference(monkeypatch):
    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-ABC",
                    "set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/; secure; HttpOnly",
                },
                json={"d": {"results": []}},
            )
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "success"
    assert result["ticket_id"] == "1000012345"
    # Digits spaced so TTS reads a reference number, not a quantity.
    assert "1 0 0 0 0 1 2 3 4 5" in result["speak"]


@pytest.mark.asyncio
async def test_the_token_and_cookie_never_leave_the_handler(monkeypatch):
    """They would otherwise enter the model's context and be persisted in the
    call transcript."""

    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-SECRET",
                    "set-cookie": "SAP_SESSIONID_S4D_120=SESSION-SECRET; path=/",
                },
                json={"d": {"results": []}},
            )
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    serialized = json.dumps(await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT)))

    assert "TOKEN-SECRET" not in serialized
    assert "SESSION-SECRET" not in serialized
    assert "Basic" not in serialized


# ---------------------------------------------------------------------------
# Cookie handling — the bug a hand-rolled parser ships
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_set_cookie_headers_with_commas_are_parsed_correctly(monkeypatch):
    """SAP sends several Set-Cookie headers, and an Expires attribute contains
    a comma of its own. Joining them and splitting on ',' cuts a cookie in half
    — this asserts the right session id still reaches the POST."""
    seen = {}

    def responder(request):
        if request.method == "GET":
            headers = [
                ("x-csrf-token", "TOKEN-ABC"),
                ("set-cookie", "MYSAPSSO2=ignored; path=/; Expires=Wed, 21 Oct 2025 07:28:00 GMT"),
                ("set-cookie", "SAP_SESSIONID_S4D_120=the-real-one; path=/; secure; HttpOnly"),
                ("set-cookie", "sap-usercontext=sap-client=120; path=/"),
            ]
            return httpx.Response(200, headers=headers, json={"d": {"results": []}})
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "success"
    assert "SAP_SESSIONID_S4D_120=the-real-one" in seen["cookie"]
    assert "sap-usercontext=sap-client=120" in seen["cookie"]


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_share_a_session(monkeypatch):
    """A module-level client would let one caller's SAP session be used for
    another caller's complaint."""
    counter = {"n": 0}
    pairs: list[tuple[str, str]] = []

    def responder(request):
        if request.method == "GET":
            counter["n"] += 1
            n = counter["n"]
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": f"TOKEN-{n}",
                    "set-cookie": f"SAP_SESSIONID_S4D_120=sess{n}; path=/",
                },
                json={"d": {"results": []}},
            )
        pairs.append(
            (request.headers.get("x-csrf-token", ""), request.headers.get("cookie", ""))
        )
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    await asyncio.gather(
        *(sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT)) for _ in range(20))
    )

    assert len(pairs) == 20
    for token, cookie in pairs:
        n = token.removeprefix("TOKEN-")
        assert f"SAP_SESSIONID_S4D_120=sess{n}" in cookie, (
            f"token {token} was paired with the wrong session: {cookie}"
        )


# ---------------------------------------------------------------------------
# Stale token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_token_is_refetched_and_the_post_retried_once(monkeypatch):
    calls = {"get": 0, "post": 0}

    def responder(request):
        if request.method == "GET":
            calls["get"] += 1
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": f"TOKEN-{calls['get']}",
                    "set-cookie": f"SAP_SESSIONID_S4D_120=sess{calls['get']}; path=/",
                },
                json={"d": {"results": []}},
            )
        calls["post"] += 1
        if calls["post"] == 1:
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, text="stale")
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "success"
    assert calls == {"get": 2, "post": 2}


@pytest.mark.asyncio
async def test_a_genuine_403_is_not_retried(monkeypatch):
    """Only 403 *with* x-csrf-token: Required means a stale token. Anything
    else is a real authorisation failure and must surface."""
    calls = {"post": 0}

    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-ABC",
                    "set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/",
                },
                json={"d": {"results": []}},
            )
        calls["post"] += 1
        return httpx.Response(403, text="not authorised")

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "error"
    assert calls["post"] == 1


# ---------------------------------------------------------------------------
# Refusing to proceed on a broken handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/"},  # no token
        {"x-csrf-token": "TOKEN-ABC"},  # no session cookie
    ],
    ids=["missing_token", "missing_session_cookie"],
)
async def test_an_incomplete_handshake_stops_before_the_post(monkeypatch, headers):
    calls = {"post": 0}

    def responder(request):
        if request.method == "GET":
            return httpx.Response(200, headers=headers, json={"d": {"results": []}})
        calls["post"] += 1
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "error"
    assert calls["post"] == 0, "must not attempt a write with half a handshake"


# ---------------------------------------------------------------------------
# Failure shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timeout_never_claims_the_complaint_failed(monkeypatch):
    """SAP may have created it anyway. Saying it failed invites a duplicate."""

    def responder(request):
        raise httpx.ReadTimeout("too slow", request=request)

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "error"
    assert "couldn't confirm" in result["speak"]
    assert "failed" not in result["speak"].lower()


@pytest.mark.asyncio
async def test_a_large_sap_error_body_does_not_reach_the_caller(monkeypatch):
    """Responses are injected into the model's context and persisted."""

    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-ABC",
                    "set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/",
                },
                json={"d": {"results": []}},
            )
        return httpx.Response(500, text="x" * 500_000)

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "error"
    assert len(json.dumps(result)) < 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ImPartner", "ImQcode", "ImNotes", "ImTicketcreationtag"])
async def test_a_missing_field_is_rejected_before_any_sap_call(monkeypatch, field):
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, responder)
    event = dict(VALID_EVENT)
    event[field] = "  "
    result = await sap.create_complaint_with_fresh_csrf(event)

    assert result["status"] == "error"
    assert field in result["message"]
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_a_create_with_no_ticket_id_is_an_error_not_a_success(monkeypatch):
    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-ABC",
                    "set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/",
                },
                json={"d": {"results": []}},
            )
        return httpx.Response(201, json={"d": {}})

    _install_transport(monkeypatch, responder)
    result = await sap.create_complaint_with_fresh_csrf(dict(VALID_EVENT))

    assert result["status"] == "error"
    assert "ticket id" in result["message"]


@pytest.mark.asyncio
async def test_notes_are_normalised_and_bounded(monkeypatch):
    seen = {}

    def responder(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "x-csrf-token": "TOKEN-ABC",
                    "set-cookie": "SAP_SESSIONID_S4D_120=sess1; path=/",
                },
                json={"d": {"results": []}},
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, content=_created_body(), headers={"content-type": "application/json"})

    _install_transport(monkeypatch, responder)
    event = dict(VALID_EVENT)
    event["ImNotes"] = "line one\n\n   line   two  " + ("z" * 500)
    await sap.create_complaint_with_fresh_csrf(event)

    notes = seen["body"]["ImNotes"]
    assert len(notes) <= sap.MAX_NOTES_CHARS
    assert "\n" not in notes
    assert "   " not in notes
