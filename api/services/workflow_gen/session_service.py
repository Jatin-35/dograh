"""Service layer between the SSE route and agent_loop/db_client.

Both public functions are async generators yielding
`{"event": <SSE-shaped dict>, "revision": int}` — the route writes each as
an SSE frame. Persistence happens at *every* yielded step, not just at the
end: this is what makes `pending_action` durable the instant it's proposed
(before the approval frame even reaches the client) rather than living only
in the agent loop's stack frame.
"""

from typing import Any, AsyncIterator

from loguru import logger

from api.db import db_client
from api.db.models import WorkflowGenChatSessionModel
from api.db.workflow_gen_chat_session_client import (
    WorkflowGenChatSessionRevisionConflictError,
)
from api.services.workflow_gen import agent_loop

__all__ = ["WorkflowGenChatSessionRevisionConflictError", "PendingActionRequiredError"]


class PendingActionRequiredError(Exception):
    """Raised when a message is sent while a prior action is still awaiting confirmation."""


def _status_for(event_type: str, *, has_pending: bool) -> str:
    if has_pending:
        return "awaiting_confirmation"
    if event_type in ("done", "error"):
        return "idle"
    return "running"


async def _reset_status_to_idle(session_id: int, organization_id: int) -> None:
    """Release a session left mid-flight by a turn that died.

    Only `status` is touched, and only when it's still "running" — an
    `awaiting_confirmation` row has an approval the user still needs to
    resolve, and `messages`/`pending_action` must keep whatever the last
    successful step persisted. Never raises: this runs on a failure path and
    must not mask the original exception.
    """
    try:
        chat_session = await db_client.get_workflow_gen_chat_session(
            session_id, organization_id=organization_id
        )
        if chat_session and chat_session.status == "running":
            await db_client.update_workflow_gen_chat_session(
                session_id, organization_id=organization_id, status="idle"
            )
    except Exception:
        logger.exception(f"Failed to reset workflow_gen session {session_id} status")


_TITLE_MAX_LEN = 60


def _title_from_message(text: str) -> str:
    """Derive a short thread-history label from the first user message —
    truncated to ~60 chars with an ellipsis, never overwritten later
    (see WorkflowGenChatSessionClient.update_workflow_gen_chat_session)."""
    trimmed = text.strip()
    if len(trimmed) <= _TITLE_MAX_LEN:
        return trimmed
    return trimmed[:_TITLE_MAX_LEN].rstrip() + "…"


async def create_session(
    organization_id: int, user_id: int | None
) -> WorkflowGenChatSessionModel:
    return await db_client.create_workflow_gen_chat_session(
        organization_id=organization_id, created_by=user_id
    )


async def ensure_session_for_workflow(
    workflow_id: int, organization_id: int, user_id: int | None
) -> WorkflowGenChatSessionModel:
    return await db_client.ensure_workflow_gen_chat_session(
        workflow_id, organization_id=organization_id, created_by=user_id
    )


async def get_session(
    session_id: int, organization_id: int
) -> WorkflowGenChatSessionModel | None:
    return await db_client.get_workflow_gen_chat_session(
        session_id, organization_id=organization_id
    )


async def list_standalone_sessions(
    organization_id: int, user_id: int | None
) -> list[WorkflowGenChatSessionModel]:
    """Standalone (non-per-workflow) sessions for the current user, most
    recently updated first — backs the thread-history sidebar."""
    return await db_client.list_workflow_gen_chat_sessions(
        organization_id=organization_id, created_by=user_id
    )


async def append_user_turn_and_run(
    session_id: int,
    *,
    organization_id: int,
    user_id: int | None,
    text: str,
    expected_revision: int | None,
) -> AsyncIterator[dict[str, Any]]:
    chat_session = await db_client.get_workflow_gen_chat_session(
        session_id, organization_id=organization_id
    )
    if not chat_session:
        raise ValueError(f"Workflow gen chat session {session_id} not found")
    if chat_session.pending_action:
        raise PendingActionRequiredError(
            "This session has a proposed action awaiting your confirmation — "
            "confirm or cancel it before sending a new message."
        )
    if expected_revision is not None and chat_session.revision != expected_revision:
        raise WorkflowGenChatSessionRevisionConflictError(
            expected_revision=expected_revision, actual_revision=chat_session.revision
        )

    # Standalone sessions only get an auto-generated title from their first
    # user message, for the thread-history sidebar.
    # `update_workflow_gen_chat_session` only applies it if not already set,
    # so it's safe to pass on every step below without extra bookkeeping.
    # The `len(messages) == 0` check (not just `title is None`) matters for
    # sessions created before this feature existed: they have real prior
    # history but a null title, and without this guard the *next* message
    # sent — whatever it happens to be, e.g. "continue" — would wrongly
    # become the permanent title instead of being recognized as mid-thread.
    title = (
        _title_from_message(text)
        if not chat_session.is_workflow_scoped
        and chat_session.title is None
        and not chat_session.messages
        else None
    )

    try:
        async for step in agent_loop.run_turn(
            organization_id=organization_id,
            user_id=user_id,
            prior_messages=chat_session.messages,
            user_message=text,
        ):
            has_pending = step.event["type"] == "approval"
            # Once the workflow gets a real name (i.e. it's been built), the
            # thread should be labeled with that instead of the placeholder
            # first-message snippet — this always overwrites, unlike `title`.
            agent_name = (
                step.event["data"]["name"] if step.event["type"] == "workflow_ready" else None
            )
            updated = await db_client.update_workflow_gen_chat_session(
                session_id,
                organization_id=organization_id,
                messages=step.messages,
                pending_action=step.pending_action if has_pending else None,
                status=_status_for(step.event["type"], has_pending=has_pending),
                workflow_id=step.workflow_id,
                title=title,
                agent_name=agent_name,
            )
            yield {"event": step.event, "revision": updated.revision}
    except Exception:
        await _reset_status_to_idle(session_id, organization_id)
        raise


async def confirm_pending_action(
    session_id: int,
    *,
    organization_id: int,
    user_id: int | None,
    action_id: str,
    approve: bool,
) -> AsyncIterator[dict[str, Any]]:
    chat_session = await db_client.get_workflow_gen_chat_session(
        session_id, organization_id=organization_id
    )
    if not chat_session:
        raise ValueError(f"Workflow gen chat session {session_id} not found")

    current_pending = chat_session.pending_action
    if not current_pending or current_pending.get("action_id") != action_id:
        # Already resolved by a prior request (double-click, browser retry,
        # network retry) — idempotent no-op, return current state unchanged.
        yield {
            "event": {"type": "done", "data": {}},
            "revision": chat_session.revision,
            "already_resolved": True,
        }
        return

    try:
        async for step in agent_loop.execute_confirmed_action(
            organization_id=organization_id,
            user_id=user_id,
            prior_messages=chat_session.messages,
            pending_action=current_pending,
            approve=approve,
        ):
            # `execute_confirmed_action` resolves `current_pending` and then
            # resumes the agent loop for any post-build follow-up — which can
            # itself immediately propose a NEW mutating call (a fresh approval).
            # Unconditionally clearing `pending_action` here would silently drop
            # that new approval from persisted state, leaving its tool_call_id
            # unanswered in `messages` while the DB claims nothing is pending —
            # letting the next plain message sail past the
            # `append_user_turn_and_run` guard and violate the LLM's tool-call
            # contract on the very next completion call. Mirror
            # `append_user_turn_and_run`'s has_pending handling instead.
            has_pending = step.event["type"] == "approval"
            agent_name = (
                step.event["data"]["name"] if step.event["type"] == "workflow_ready" else None
            )
            updated = await db_client.update_workflow_gen_chat_session(
                session_id,
                organization_id=organization_id,
                messages=step.messages,
                pending_action=step.pending_action if has_pending else None,
                status=_status_for(step.event["type"], has_pending=has_pending),
                workflow_id=step.workflow_id,
                agent_name=agent_name,
            )
            yield {"event": step.event, "revision": updated.revision}
    except Exception:
        await _reset_status_to_idle(session_id, organization_id)
        raise
