"""In-product AI assistant routes — SSE-streamed chat over WorkflowGenToolbox.

  POST /workflow-gen/sessions                          new standalone session
  POST /workflow-gen/workflows/{workflow_id}/session    get-or-create, per-workflow
  GET  /workflow-gen/sessions/{id}                      session snapshot (reload UI)
  POST /workflow-gen/sessions/{id}/messages             one chat turn (SSE stream)
  POST /workflow-gen/sessions/{id}/confirm              resolve a pending action (SSE stream)
"""

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from api.db.models import UserModel, WorkflowGenChatSessionModel
from api.db.workflow_gen_chat_session_client import (
    WorkflowGenChatSessionRevisionConflictError,
)
from api.services.auth.depends import get_user_with_selected_organization
from api.services.workflow_gen import session_service
from api.services.workflow_gen.config import is_workflow_gen_configured
from api.services.workflow_gen.session_service import PendingActionRequiredError

router = APIRouter(prefix="/workflow-gen", tags=["workflow-gen-chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
}


class AppendWorkflowGenMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int | None = None


class ConfirmWorkflowGenActionRequest(BaseModel):
    action_id: str
    approve: bool


class WorkflowGenChatSessionResponse(BaseModel):
    id: int
    session_uuid: str
    revision: int
    messages: list[dict[str, Any]]
    pending_action: dict[str, Any] | None = None
    status: str
    workflow_id: int | None = None


class WorkflowGenChatSessionSummary(BaseModel):
    id: int
    title: str
    updated_at: datetime


def _build_response(
    chat_session: WorkflowGenChatSessionModel,
) -> WorkflowGenChatSessionResponse:
    return WorkflowGenChatSessionResponse(
        id=chat_session.id,
        session_uuid=chat_session.session_uuid,
        revision=chat_session.revision,
        messages=chat_session.messages or [],
        pending_action=chat_session.pending_action,
        status=chat_session.status,
        workflow_id=chat_session.workflow_id,
    )


def _build_summary(
    chat_session: WorkflowGenChatSessionModel,
) -> WorkflowGenChatSessionSummary:
    return WorkflowGenChatSessionSummary(
        id=chat_session.id,
        title=chat_session.title or "New conversation",
        updated_at=chat_session.updated_at,
    )


def _require_configured() -> None:
    if not is_workflow_gen_configured():
        raise HTTPException(
            status_code=503,
            detail="The in-product AI assistant isn't configured on this deployment.",
        )


def _sse(event: dict[str, Any], revision: int) -> str:
    return f"data: {json.dumps({**event, 'revision': revision}, default=str)}\n\n"


@router.post("/sessions", response_model=WorkflowGenChatSessionResponse)
async def create_workflow_gen_session(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WorkflowGenChatSessionResponse:
    _require_configured()
    chat_session = await session_service.create_session(
        organization_id=user.selected_organization_id, user_id=user.id
    )
    return _build_response(chat_session)


@router.post(
    "/workflows/{workflow_id}/session", response_model=WorkflowGenChatSessionResponse
)
async def ensure_workflow_gen_session_for_workflow(
    workflow_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WorkflowGenChatSessionResponse:
    """Get-or-create the one canonical chat session for a workflow — the
    per-editor "AI Assistant" entry point, persisted across visits."""
    _require_configured()
    chat_session = await session_service.ensure_session_for_workflow(
        workflow_id,
        organization_id=user.selected_organization_id,
        user_id=user.id,
    )
    return _build_response(chat_session)


@router.get("/sessions", response_model=list[WorkflowGenChatSessionSummary])
async def list_workflow_gen_sessions(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[WorkflowGenChatSessionSummary]:
    """List the current user's standalone (non-per-workflow) sessions, most
    recently updated first — backs the thread-history sidebar."""
    sessions = await session_service.list_standalone_sessions(
        organization_id=user.selected_organization_id, user_id=user.id
    )
    return [_build_summary(s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=WorkflowGenChatSessionResponse)
async def get_workflow_gen_session(
    session_id: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WorkflowGenChatSessionResponse:
    chat_session = await session_service.get_session(
        session_id, organization_id=user.selected_organization_id
    )
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _build_response(chat_session)


@router.post("/sessions/{session_id}/messages")
async def append_workflow_gen_message(
    session_id: int,
    request: AppendWorkflowGenMessageRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> StreamingResponse:
    _require_configured()

    async def event_stream():
        try:
            async for step in session_service.append_user_turn_and_run(
                session_id,
                organization_id=user.selected_organization_id,
                user_id=user.id,
                text=request.text,
                expected_revision=request.expected_revision,
            ):
                yield _sse(step["event"], step["revision"])
        except ValueError as e:
            yield _sse({"type": "error", "data": {"code": "internal", "message": str(e)}}, -1)
        except PendingActionRequiredError as e:
            yield _sse({"type": "error", "data": {"code": "internal", "message": str(e)}}, -1)
        except WorkflowGenChatSessionRevisionConflictError as e:
            yield _sse(
                {
                    "type": "error",
                    "data": {
                        "code": "internal",
                        "message": f"Session changed since you last saw it (expected revision {e.expected_revision}, now {e.actual_revision}). Reload and retry.",
                    },
                },
                e.actual_revision,
            )
        except Exception:
            # Without this the stream would just stop mid-turn: no `error`
            # frame, no `done` frame, and the user's message appears to vanish
            # with no feedback at all.
            logger.exception(f"workflow_gen chat turn failed (session {session_id})")
            yield _sse(
                {
                    "type": "error",
                    "data": {
                        "code": "internal",
                        "message": "Something went wrong handling that message. Please try again.",
                    },
                },
                -1,
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/sessions/{session_id}/confirm")
async def confirm_workflow_gen_action(
    session_id: int,
    request: ConfirmWorkflowGenActionRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> StreamingResponse:
    _require_configured()

    async def event_stream():
        try:
            async for step in session_service.confirm_pending_action(
                session_id,
                organization_id=user.selected_organization_id,
                user_id=user.id,
                action_id=request.action_id,
                approve=request.approve,
            ):
                yield _sse(step["event"], step["revision"])
        except ValueError as e:
            yield _sse({"type": "error", "data": {"code": "internal", "message": str(e)}}, -1)
        except Exception:
            logger.exception(
                f"workflow_gen confirm failed (session {session_id}, action {request.action_id})"
            )
            yield _sse(
                {
                    "type": "error",
                    "data": {
                        "code": "internal",
                        "message": "Something went wrong completing that action. Please try again.",
                    },
                },
                -1,
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
