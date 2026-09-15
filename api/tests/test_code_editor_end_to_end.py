"""Code Editor, end to end.

Unit tests checked each piece in isolation and still let a broken feature
through: deploy generated a URL for an endpoint that was never implemented, so
every deployed tool would have 404'd on a live call while the shape assertions
passed. These exercise the seams instead — the API service really calling the
real sandbox over the real HTTP contract.

The handlers app runs in-process over an ASGI transport rather than as a
separate container. That keeps it a unit test in speed while still crossing the
boundary where the mismatches live: the URL, the payload shape, the auth header
and the response envelope.
"""

import json
from typing import Any

import httpx
import pytest

from handlers.main import app as handlers_app

ROUTER = "all_events_entry_point.py"

ROUTER_SOURCE = '''
def all_events_handler(event, context):
    function_name = event.get("function_name")

    if function_name == "get_order_status":
        order_id = event.get("order_id")
        print("looking up", order_id)
        return {
            "status": "shipped",
            "speak": f"Order {order_id} has shipped.",
            "caller": context.get("caller_number"),
            "org": context.get("organization_id"),
        }

    if function_name == "needs_secret":
        import os
        return {"key_seen": os.environ.get("ORDERS_API_KEY")}

    return {"error": f"Unknown function: {function_name}"}
'''

SCHEMA = json.dumps(
    {
        "name": "get_order_status",
        "description": "Gets the current status of a customer order",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "e.g. ORD-12345"}
            },
            "additionalProperties": False,
            "required": ["order_id"],
        },
    }
)


@pytest.fixture
def wired(monkeypatch):
    """Point the API's workspace service at the real handlers app, in-process."""
    from api.services.code_editor import workspace

    monkeypatch.setenv("HANDLERS_API_KEY", "test-key")
    monkeypatch.setattr(handlers_app.router, "lifespan_context", None, raising=False)

    transport = httpx.ASGITransport(app=handlers_app)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, base_url="http://handlers", timeout=30.0)

    monkeypatch.setattr(workspace.httpx, "AsyncClient", _client)
    return workspace


def _files() -> dict[str, str]:
    return {
        ROUTER: ROUTER_SOURCE,
        "function_definitions/get_order_status.json": SCHEMA,
    }


# ---------------------------------------------------------------------------
# API service → sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_api_can_actually_execute_a_workspace(wired, monkeypatch):
    """The seam that matters: the API's payload shape must match what the
    sandbox endpoint expects, including the auth header."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    outcome = await wired.run_test(
        organization_id=1,
        event={"function_name": "get_order_status", "order_id": "ORD-1"},
        files=_files(),
    )

    assert outcome["statusCode"] == 200
    assert outcome["result"]["status"] == "shipped"
    assert outcome["result"]["speak"] == "Order ORD-1 has shipped."
    # print() reaches the console separately from the return value.
    assert "looking up ORD-1" in outcome["logs"]


@pytest.mark.asyncio
async def test_the_call_context_reaches_the_handler(wired, monkeypatch):
    """`context` is how a handler tells which agent and which caller triggered
    it — the replacement for the reference platform's `business` object."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    payload_context = wired.build_context(
        organization_id=42, workflow_id=7, workflow_run_id=99, caller_number="+911234"
    )
    assert payload_context == {
        "organization_id": 42,
        "workflow_id": 7,
        "workflow_run_id": 99,
        "caller_number": "+911234",
    }

    outcome = await wired.run_test(
        organization_id=42,
        event={"function_name": "get_order_status", "order_id": "ORD-2"},
        files=_files(),
    )
    assert outcome["result"]["org"] == 42


@pytest.mark.asyncio
async def test_workflow_id_and_run_id_reach_the_handler(wired, monkeypatch):
    """The gap this closes: `build_context` always documented workflow_id and
    workflow_run_id, but nothing on the real call path ever supplied them —
    every live invocation saw them as null. This is the seam that now carries
    real values, so it is what has to prove they actually arrive."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    files = {
        ROUTER: (
            "def all_events_handler(event, context):\n"
            "    return {\n"
            "        'workflow_id': context.get('workflow_id'),\n"
            "        'workflow_run_id': context.get('workflow_run_id'),\n"
            "    }\n"
        )
    }

    outcome = await wired.run_test(
        organization_id=1,
        event={"function_name": "echo_context"},
        files=files,
        workflow_id=7,
        workflow_run_id=99,
    )

    assert outcome["result"] == {"workflow_id": 7, "workflow_run_id": 99}


@pytest.mark.asyncio
async def test_an_interactive_test_run_has_no_workflow_context(wired, monkeypatch):
    """Test Latest / Test Deployed have no real call behind them — the honest
    answer is null, not a stale or guessed value from some other run."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    files = {
        ROUTER: (
            "def all_events_handler(event, context):\n"
            "    return {\n"
            "        'workflow_id': context.get('workflow_id'),\n"
            "        'workflow_run_id': context.get('workflow_run_id'),\n"
            "    }\n"
        )
    }

    outcome = await wired.run_test(
        organization_id=1, event={"function_name": "echo_context"}, files=files
    )

    assert outcome["result"] == {"workflow_id": None, "workflow_run_id": None}


@pytest.mark.asyncio
async def test_org_environment_variables_reach_user_code(wired, monkeypatch):
    monkeypatch.setattr(wired, "resolve_env", _async_return({"ORDERS_API_KEY": "sk-123"}))

    outcome = await wired.run_test(
        organization_id=1, event={"function_name": "needs_secret"}, files=_files()
    )

    assert outcome["statusCode"] == 200
    assert outcome["result"]["key_seen"] == "sk-123"


@pytest.mark.asyncio
async def test_an_unknown_function_comes_back_as_the_routers_own_answer(wired, monkeypatch):
    """The router's fallback branch, not a platform error — the model should
    see what the user's code actually said."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    outcome = await wired.run_test(
        organization_id=1, event={"function_name": "nope"}, files=_files()
    )

    assert outcome["statusCode"] == 200
    assert "Unknown function" in outcome["result"]["error"]


@pytest.mark.asyncio
async def test_a_crash_in_user_code_is_reported_not_raised(wired, monkeypatch):
    """A handler that raises must not propagate into the API process."""
    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    files = {ROUTER: "def all_events_handler(event, context):\n    raise RuntimeError('boom')\n"}
    outcome = await wired.run_test(
        organization_id=1, event={"function_name": "x"}, files=files
    )

    assert outcome["statusCode"] == 500
    assert "boom" in outcome["error"]


@pytest.mark.asyncio
async def test_a_missing_router_fails_before_execution(wired):
    from api.services.code_editor.workspace import WorkspaceError

    with pytest.raises(WorkspaceError) as exc:
        await wired.run_test(
            organization_id=1,
            event={"function_name": "x"},
            files={"helpers/util.py": "x = 1"},
        )
    assert ROUTER in str(exc.value)


@pytest.mark.asyncio
async def test_the_sandbox_rejects_a_wrong_key(monkeypatch):
    """The auth boundary itself. Without it any org could execute another
    org's code, since Dograh's HTTP tools have no SSRF guard."""
    monkeypatch.setenv("HANDLERS_API_KEY", "the-real-key")
    transport = httpx.ASGITransport(app=handlers_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://handlers") as client:
        refused = await client.post(
            "/execute", json={"files": _files()}, headers={"X-Handler-Key": "guessed"}
        )
        missing = await client.post("/execute", json={"files": _files()})

    assert refused.status_code == 401
    assert missing.status_code == 401


@pytest.mark.asyncio
async def test_a_rejected_key_is_reported_clearly(wired, monkeypatch):
    """Otherwise a key mismatch surfaces as an opaque 401 with nothing naming
    the cause — and the two containers are configured separately."""
    from api.services.code_editor.workspace import WorkspaceError

    monkeypatch.setattr(wired, "resolve_env", _async_return({}))

    class _Unauthorized:
        status_code = 401
        is_error = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return self

    monkeypatch.setattr(wired.httpx, "AsyncClient", lambda *a, **k: _Unauthorized())

    with pytest.raises(WorkspaceError) as exc:
        await wired.run_test(
            organization_id=1, event={"function_name": "x"}, files=_files()
        )
    assert "HANDLERS_API_KEY" in str(exc.value)


@pytest.mark.asyncio
async def test_the_sandbox_being_down_is_reported_clearly(monkeypatch):
    """A container that isn't running must say so, not 500."""
    from api.services.code_editor import workspace
    from api.services.code_editor.workspace import WorkspaceError

    monkeypatch.setenv("HANDLERS_API_KEY", "test-key")
    monkeypatch.setattr(workspace, "resolve_env", _async_return({}))

    def _dead(*args, **kwargs):
        class _Dead:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise httpx.ConnectError("connection refused")

        return _Dead()

    monkeypatch.setattr(workspace.httpx, "AsyncClient", _dead)

    with pytest.raises(WorkspaceError) as exc:
        await workspace.run_test(
            organization_id=1, event={"function_name": "x"}, files=_files()
        )
    assert "handlers container" in str(exc.value)


def _async_return(value: Any):
    async def _inner(*args, **kwargs):
        return value

    return _inner
