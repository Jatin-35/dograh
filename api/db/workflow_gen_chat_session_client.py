from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import WorkflowGenChatSessionModel


class WorkflowGenChatSessionRevisionConflictError(Exception):
    def __init__(self, expected_revision: int, actual_revision: int):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "Workflow gen chat session revision conflict: "
            f"expected {expected_revision}, found {actual_revision}"
        )


class WorkflowGenChatSessionClient(BaseDBClient):
    async def create_workflow_gen_chat_session(
        self,
        organization_id: int,
        created_by: int | None = None,
    ) -> WorkflowGenChatSessionModel:
        async with self.async_session() as session:
            chat_session = WorkflowGenChatSessionModel(
                organization_id=organization_id,
                created_by=created_by,
                messages=[],
                status="idle",
            )
            session.add(chat_session)
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(chat_session)
        return chat_session

    async def ensure_workflow_gen_chat_session(
        self,
        workflow_id: int,
        *,
        organization_id: int,
        created_by: int | None = None,
    ) -> WorkflowGenChatSessionModel:
        """Get-or-create the one canonical chat session for a workflow —
        find-or-create-under-lock, mirroring
        WorkflowRunTextSessionClient.ensure_workflow_run_text_session.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowGenChatSessionModel)
                .where(
                    WorkflowGenChatSessionModel.workflow_id == workflow_id,
                    WorkflowGenChatSessionModel.organization_id == organization_id,
                )
                .with_for_update()
            )
            chat_session = result.scalars().first()
            if chat_session:
                return chat_session

            chat_session = WorkflowGenChatSessionModel(
                organization_id=organization_id,
                created_by=created_by,
                workflow_id=workflow_id,
                is_workflow_scoped=True,
                messages=[],
                status="idle",
            )
            session.add(chat_session)
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(chat_session)
        return chat_session

    async def get_workflow_gen_chat_session(
        self,
        session_id: int,
        *,
        organization_id: int,
    ) -> WorkflowGenChatSessionModel | None:
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowGenChatSessionModel).where(
                    WorkflowGenChatSessionModel.id == session_id,
                    WorkflowGenChatSessionModel.organization_id == organization_id,
                )
            )
            return result.scalars().first()

    async def update_workflow_gen_chat_session(
        self,
        session_id: int,
        *,
        organization_id: int,
        messages: list | None = None,
        pending_action: dict | None = "__unset__",
        status: str | None = None,
        workflow_id: int | None = None,
        expected_revision: int | None = None,
        title: str | None = None,
        agent_name: str | None = None,
    ) -> WorkflowGenChatSessionModel:
        """Update a session, bumping its revision.

        `pending_action` uses the sentinel default "__unset__" so callers can
        explicitly pass `None` to clear a resolved/cancelled pending action,
        distinct from "leave it as-is" (the default).

        `title` is only ever applied if the session doesn't already have one
        — callers may pass it on every call for a turn without needing to
        track whether it was already set (idempotent, first-write-wins). It's
        a placeholder derived from the first user message, used before a
        workflow has been named.

        `agent_name` always overwrites the title, regardless of what's
        currently set — pass this once the thread's workflow gets a real
        name (a `workflow_ready` event), so the thread is labeled with the
        agent's actual name rather than the initial message snippet.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowGenChatSessionModel)
                .where(
                    WorkflowGenChatSessionModel.id == session_id,
                    WorkflowGenChatSessionModel.organization_id == organization_id,
                )
                .with_for_update()
            )
            chat_session = result.scalars().first()
            if not chat_session:
                raise ValueError(f"Workflow gen chat session {session_id} not found")

            if (
                expected_revision is not None
                and chat_session.revision != expected_revision
            ):
                raise WorkflowGenChatSessionRevisionConflictError(
                    expected_revision=expected_revision,
                    actual_revision=chat_session.revision,
                )

            if messages is not None:
                chat_session.messages = messages
            if pending_action != "__unset__":
                chat_session.pending_action = pending_action
            if status is not None:
                chat_session.status = status
            if workflow_id is not None:
                chat_session.workflow_id = workflow_id
            if title is not None and chat_session.title is None:
                chat_session.title = title
            if agent_name is not None:
                chat_session.title = agent_name
            chat_session.revision += 1

            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(chat_session)
        return chat_session

    async def list_workflow_gen_chat_sessions(
        self,
        *,
        organization_id: int,
        created_by: int | None,
        limit: int = 100,
    ) -> list[WorkflowGenChatSessionModel]:
        """Sessions that originated from the standalone entry point (not
        `is_workflow_scoped`), for the thread-history sidebar — scoped to
        both the org and the requesting user, most recently updated first.
        A standalone session that has since built a workflow still counts:
        `is_workflow_scoped` is set once at creation and never changes, so a
        successful build doesn't make the thread disappear from its own
        history (checking `workflow_id IS NULL` here would be wrong for
        exactly that reason — a completed thread's `workflow_id` is no
        longer null, but it never stops being the thread it always was)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowGenChatSessionModel)
                .where(
                    WorkflowGenChatSessionModel.organization_id == organization_id,
                    WorkflowGenChatSessionModel.created_by == created_by,
                    WorkflowGenChatSessionModel.is_workflow_scoped.is_(False),
                )
                .order_by(WorkflowGenChatSessionModel.updated_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())
