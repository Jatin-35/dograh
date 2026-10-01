"""Scout Stop, end to end over a real HTTP server.

A real uvicorn server runs the real Scout route; an HTTP client drops the
stream mid-turn, which is exactly what the Stop button does (it aborts the
fetch). The agent behind the route must actually be cancelled — not left
running in the background — and the session must end idle.

uvicorn 0.35 reports ASGI spec 2.3, so Starlette's StreamingResponse watches
for ``http.disconnect`` and cancels the response task when it arrives; the
same path in the pinned Starlette 1.6.0.
"""

import asyncio
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from api.routes import workflow_gen_chat
from api.services.auth.depends import get_user_with_selected_organization
from api.services.workflow_gen import session_service


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _session(status="running", pending=None):
    return SimpleNamespace(
        id=7, pending_action=pending, revision=3, is_workflow_scoped=True, title="t",
        messages=[], workflow_id=11, surface="workflow", status=status,
    )


def _step(kind="status"):
    data = {"message": "Reading the workflow"}
    return SimpleNamespace(event={"type": kind, "data": data}, messages=[],
                           pending_action=None, workflow_id=11)


class _Agent:
    """A turn that emits one step (or none) and then keeps working."""

    def __init__(self, emit_first=True):
        self.emit_first = emit_first
        self.cancelled = threading.Event()
        self.finished = threading.Event()

    async def run(self, **kwargs):
        if self.emit_first:
            yield _step()
        try:
            await asyncio.sleep(30)  # the model / a tool still working
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.finished.set()
        yield _step()


@pytest.fixture
def server():
    """Run the Scout router on a real uvicorn server in a background thread."""
    app = FastAPI()
    app.include_router(workflow_gen_chat.router, prefix="/api/v1")
    app.dependency_overrides[get_user_with_selected_organization] = lambda: SimpleNamespace(
        id=2, selected_organization_id=1
    )
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    with patch.object(workflow_gen_chat, "_require_scout_enabled", AsyncMock()):
        thread.start()
        deadline = time.time() + 10
        while not srv.started and time.time() < deadline:
            time.sleep(0.05)
        assert srv.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}"
        srv.should_exit = True
        thread.join(timeout=5)


def _db(reset_status="running"):
    db = MagicMock()
    # First read: the turn starts. Later reads: the idle reset checks status.
    db.get_workflow_gen_chat_session = AsyncMock(
        side_effect=[_session()] + [_session(status=reset_status)] * 5
    )
    db.update_workflow_gen_chat_session = AsyncMock(return_value=SimpleNamespace(revision=4))
    return db


def _statuses(db):
    return [c.kwargs["status"] for c in db.update_workflow_gen_chat_session.await_args_list
            if "status" in c.kwargs]


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _post_and_drop(url, read_first_event=True):
    """Start a turn and drop the stream, the way the Stop button aborts the fetch."""
    with httpx.Client(timeout=10) as client:
        with client.stream(
            "POST", f"{url}/api/v1/workflow-gen/sessions/7/messages",
            json={"text": "add a transfer tool", "expected_revision": 3},
        ) as response:
            assert response.status_code == 200
            if read_first_event:
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        return line
            else:
                time.sleep(0.3)
    return None


def test_stop_mid_turn_cancels_the_agent_and_leaves_the_session_idle(server):
    agent, db = _Agent(), _db()
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", agent.run
    ):
        first = _post_and_drop(server)
        assert first is not None and "Reading the workflow" in first

        assert _wait(agent.cancelled.is_set), "the agent kept running after Stop"
        assert _wait(lambda: "idle" in _statuses(db)), f"statuses: {_statuses(db)}"
    assert not agent.finished.is_set()
    assert _statuses(db)[-1] == "idle"


def test_stop_before_the_first_reply_cancels_cleanly(server):
    agent, db = _Agent(emit_first=False), _db()
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", agent.run
    ):
        _post_and_drop(server, read_first_event=False)
        assert _wait(agent.cancelled.is_set), "the agent kept running after Stop"
    assert not agent.finished.is_set()
    # No step was persisted, so nothing was ever set to "running" by this turn.
    assert "running" not in _statuses(db)


def test_stop_does_not_clear_an_approval_that_is_waiting(server):
    """If the last persisted step was an approval card, Stop must not wipe it:
    the reset only touches a session still marked "running"."""
    agent, db = _Agent(), _db(reset_status="awaiting_confirmation")
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", agent.run
    ):
        _post_and_drop(server)
        assert _wait(agent.cancelled.is_set)
        time.sleep(0.3)  # let the reset run
    assert "idle" not in _statuses(db)


def test_a_turn_left_alone_still_completes(server):
    """Not stopping must keep working exactly as before."""
    db = _db()

    async def quick_turn(**kwargs):
        yield _step()
        yield SimpleNamespace(event={"type": "done", "data": {}}, messages=[],
                              pending_action=None, workflow_id=11)

    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", quick_turn
    ):
        with httpx.Client(timeout=10) as client:
            body = client.post(
                f"{server}/api/v1/workflow-gen/sessions/7/messages",
                json={"text": "hi", "expected_revision": 3},
            ).text
    assert body.count("data:") == 2 and '"done"' in body
    assert _statuses(db)[-1] == "idle"
