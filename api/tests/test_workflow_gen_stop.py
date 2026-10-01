"""Stopping a Scout turn.

The Stop button drops the stream; the server cancels the running turn. A
cancellation is not an Exception, so before this the session stayed "running"
after every Stop.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.workflow_gen import session_service


def _session(status="running"):
    return SimpleNamespace(
        id=7,
        pending_action=None,
        revision=3,
        is_workflow_scoped=True,
        title="t",
        messages=[],
        workflow_id=11,
        surface="workflow",
        status=status,
    )


def _db():
    db = MagicMock()
    db.get_workflow_gen_chat_session = AsyncMock(return_value=_session())
    db.update_workflow_gen_chat_session = AsyncMock(return_value=SimpleNamespace(revision=4))
    return db


def _step():
    return SimpleNamespace(
        event={"type": "status", "data": {"message": "Reading the workflow"}},
        messages=[],
        pending_action=None,
        workflow_id=11,
    )


async def _never_finishing_turn(**kwargs):
    yield _step()
    await asyncio.Event().wait()  # the model / a tool still working


async def _failing_turn(**kwargs):
    yield _step()
    raise RuntimeError("model error")


def _statuses(db):
    return [
        c.kwargs.get("status")
        for c in db.update_workflow_gen_chat_session.await_args_list
        if "status" in c.kwargs
    ]


async def _run_and_stop(gen):
    async def consume():
        async for _ in gen:
            seen.set()

    seen = asyncio.Event()
    task = asyncio.create_task(consume())
    await asyncio.wait_for(seen.wait(), timeout=2)
    task.cancel()  # what a dropped stream does to the response task
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stopping_a_message_turn_leaves_the_session_idle():
    db = _db()
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", _never_finishing_turn
    ):
        gen = session_service.append_user_turn_and_run(
            7, organization_id=1, user_id=2, text="add a transfer tool", expected_revision=3
        )
        await _run_and_stop(gen)

    assert _statuses(db)[-1] == "idle"


@pytest.mark.asyncio
async def test_stopping_an_approved_action_leaves_the_session_idle():
    db = _db()
    pending = {"action_id": "a1", "tool_call_id": "c1", "name": "save_workflow", "arguments": {}}
    db.get_workflow_gen_chat_session = AsyncMock(
        return_value=SimpleNamespace(**{**vars(_session()), "pending_action": pending})
    )
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "execute_confirmed_action", _never_finishing_turn
    ):
        gen = session_service.confirm_pending_action(
            7, organization_id=1, user_id=2, action_id="a1", approve=True
        )
        await _run_and_stop(gen)

    assert _statuses(db)[-1] == "idle"


@pytest.mark.asyncio
async def test_an_error_still_resets_the_session():
    db = _db()
    with patch.object(session_service, "db_client", db), patch.object(
        session_service.agent_loop, "run_turn", _failing_turn
    ):
        gen = session_service.append_user_turn_and_run(
            7, organization_id=1, user_id=2, text="hi", expected_revision=3
        )
        with pytest.raises(RuntimeError):
            async for _ in gen:
                pass

    assert _statuses(db)[-1] == "idle"
