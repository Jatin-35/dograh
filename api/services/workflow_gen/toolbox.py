"""The agent's hands — org-scoped operations on Dograh's platform.

`WorkflowGenToolbox` wraps every DB/platform operation the in-product chat
assistant may perform, always scoped to the session's organization (tenant
isolation).

Workflow authoring runs through the *same* implementations the MCP tool
surface uses (`create_workflow_for_user` / `save_workflow_for_user` in
`api/mcp_server/tools/`): one parse → validate → persist pipeline, one error
vocabulary, shared with external MCP clients rather than reimplemented. The
tools are called in-process with an already-authenticated user — no MCP
session handshake, no API key, no HTTP loopback — but the pipeline they run
is identical, so `create_workflow`/`save_workflow` take **SDK TypeScript
source**, not a JSON definition, and return the MCP result shape verbatim.

Read-only catalog methods still go straight to `db_client`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError

from api.db import db_client
from api.db.models import UserModel
from api.enums import WebhookCredentialType
from api.mcp_server.tools._workflow_projection import project_workflow_to_sdk_view
from api.mcp_server.tools.create_workflow import create_workflow_for_user
from api.mcp_server.tools.docs_search import list_docs as _list_docs
from api.mcp_server.tools.docs_search import read_doc as _read_doc
from api.mcp_server.tools.docs_search import search_docs as _search_docs
from api.mcp_server.tools.save_workflow import save_workflow_for_user
from api.mcp_server.ts_bridge import TsBridgeError
from api.schemas.tool import CreateToolRequest
from api.services.credential_management import (
    CredentialManagementError,
    create_credential_for_user,
    credential_summary,
)
from api.services.tool_management import (
    ToolManagementError,
    create_tool_for_user,
    test_http_tool_for_user,
    update_tool_for_user,
)
from api.services.voice_prompting_guide import Stage, build_briefing, get_topic, list_topic_index
from api.services.workflow.node_specs import SPEC_VERSION, all_specs, get_spec


class WorkflowGenToolboxError(Exception):
    """A tool call failed in a way the agent loop should feed back to the LLM.

    `errors` carries the failure broken into individual, model-readable items
    (one per bad field, say) for the repair loop to hand back; `str(self)`
    stays short enough to show a human. Defaults to `[str(self)]` so callers
    that don't split anything out still work with the repair path.
    """

    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


def _format_validation_errors(exc: PydanticValidationError) -> list[str]:
    """One `field.path: message` line per invalid field, for the repair loop.

    Pydantic's own `str(exc)` is a multi-line block with URLs and input echoes
    — too noisy to feed back as-is, and too noisy to show a human."""
    lines: list[str] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ())) or "(root)"
        lines.append(f"{location}: {err.get('msg', 'invalid value')}")
    return lines or [str(exc)]


class WorkflowGenToolbox:
    """All platform operations available to the in-product chat assistant,
    org-scoped at construction."""

    def __init__(self, organization_id: int, user_id: int | None):
        self.organization_id = organization_id
        self.user_id = user_id

    async def _scoped_user(self) -> UserModel:
        """The acting user, carrying this session's organization scope.

        The shared MCP cores read tenant scope off `user.selected_organization_id`,
        and the user's profile row may have a different org selected than the
        one this chat session belongs to — same reason API-key-scoped requests
        override it (`api/services/auth/depends.py`)."""
        user = await db_client.get_user_by_id(self.user_id)
        if user is None:
            raise WorkflowGenToolboxError("Acting user not found")
        user.selected_organization_id = self.organization_id
        return user

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    async def get_workflow(self, workflow_id: int) -> dict[str, Any]:
        workflow = await db_client.get_workflow(
            workflow_id, organization_id=self.organization_id
        )
        if workflow is None:
            raise WorkflowGenToolboxError(
                f"Workflow {workflow_id} not found in this organization"
            )
        definition = (
            workflow.current_definition.workflow_json
            if workflow.current_definition
            else {}
        )
        return {
            "id": workflow.id,
            "name": workflow.name,
            "status": workflow.status,
            "definition": definition,
        }

    async def list_workflows(self, status: str | None = "active") -> list[dict[str, Any]]:
        workflows = await db_client.get_all_workflows_for_listing(
            organization_id=self.organization_id, status=status
        )
        return [
            {
                "id": w.id,
                "name": w.name,
                "status": w.status,
                "created_at": w.created_at.isoformat() if w.created_at else None,
            }
            for w in workflows
        ]

    async def get_workflow_code(self, workflow_id: int) -> dict[str, Any]:
        """The workflow as editable SDK TypeScript — the read half of the
        edit round-trip (`get_workflow_code` → edit → `save_workflow`)."""
        workflow = await db_client.get_workflow(
            workflow_id, organization_id=self.organization_id
        )
        if workflow is None:
            raise WorkflowGenToolboxError(
                f"Workflow {workflow_id} not found in this organization"
            )
        try:
            view = await project_workflow_to_sdk_view(workflow)
        except TsBridgeError as e:
            raise WorkflowGenToolboxError(f"Couldn't render this workflow as code: {e}") from e
        return {
            "workflow_id": workflow_id,
            "name": view["name"],
            "version": view["version"],
            "code": view["code"],
        }

    async def create_workflow(self, code: str) -> dict[str, Any]:
        """Create a workflow from SDK TypeScript source.

        Returns the MCP result shape verbatim — `{"created": bool, ...}` on
        success, `{"created": False, "error_code", "error"}` on failure. The
        agent loop reads that shape directly; failures are repairable, not
        exceptions."""
        user = await self._scoped_user()
        return await create_workflow_for_user(code, user, source="workflow_gen")

    async def save_workflow(self, workflow_id: int, code: str) -> dict[str, Any]:
        """Save SDK TypeScript over an existing workflow, as a draft.

        Same result-shape contract as `create_workflow`, keyed `saved`."""
        user = await self._scoped_user()
        try:
            return await save_workflow_for_user(workflow_id, code, user)
        except HTTPException as e:
            # The shared core signals "not in your org" the HTTP way; this
            # caller isn't an HTTP surface, so translate it.
            raise WorkflowGenToolboxError(str(e.detail)) from e

    # ------------------------------------------------------------------
    # Node type catalog (global, not org-scoped)
    # ------------------------------------------------------------------

    async def list_node_types(self) -> dict[str, Any]:
        return {
            "spec_version": SPEC_VERSION,
            "node_types": [
                {
                    "name": spec.name,
                    "display_name": spec.display_name,
                    "description": spec.description,
                    "category": spec.category.value,
                }
                for spec in all_specs()
            ],
        }

    async def get_node_type(self, name: str) -> dict[str, Any]:
        spec = get_spec(name)
        if spec is None:
            raise WorkflowGenToolboxError(f"Unknown node type: {name!r}")
        return spec.to_mcp_dict()

    # ------------------------------------------------------------------
    # Reference catalogs
    # ------------------------------------------------------------------

    async def list_tools(self, status: str | None = "active") -> list[dict[str, Any]]:
        tools = await db_client.get_tools_for_organization(
            organization_id=self.organization_id, status=status
        )
        return [
            {
                "tool_uuid": t.tool_uuid,
                "name": t.name,
                "description": t.description or "",
                "category": t.category,
            }
            for t in tools
        ]

    async def get_tool(self, tool_uuid: str) -> dict[str, Any]:
        """A tool's full definition — the read half of editing one in place."""
        tool = await db_client.get_tool_by_uuid(tool_uuid, self.organization_id)
        if tool is None:
            raise WorkflowGenToolboxError(
                f"Tool {tool_uuid} not found in this organization"
            )
        return {
            "tool_uuid": tool.tool_uuid,
            "name": tool.name,
            "description": tool.description or "",
            "category": tool.category,
            "status": tool.status,
            "definition": tool.definition,
        }

    async def test_tool(
        self,
        tool_uuid: str,
        llm_params: dict[str, Any] | None = None,
        preset_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fire a real request through an HTTP tool and report what came back.

        Lets the assistant verify a tool it just built actually works —
        whether the URL resolves and the credential authenticates — instead
        of leaving that to be discovered on a live call. This really does
        call the configured endpoint, which is why it sits behind the same
        confirmation gate as the mutating tools.
        """
        user = await self._scoped_user()
        try:
            result = await test_http_tool_for_user(
                tool_uuid,
                user,
                llm_params=llm_params or {},
                preset_params=preset_params or {},
            )
        except ToolManagementError as e:
            raise WorkflowGenToolboxError(e.message, errors=[e.message]) from e

        # Deliberately omits `request_headers`: they carry the resolved
        # credential, and this result is persisted into the chat transcript.
        return {
            "status": result.status,
            "status_code": result.status_code,
            "duration_ms": result.duration_ms,
            "data": result.data,
            "error": result.error,
            "hint": result.hint,
            "request_method": result.request_method,
            "request_url": result.request_url,
        }

    async def update_tool(
        self,
        tool_uuid: str,
        tool_definition: dict[str, Any] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Change an existing tool in place.

        Without this the assistant's only way to "add auth to that tool" is
        to build a second near-identical one, leaving the original behind as
        a confusing duplicate. `tool_definition`, when given, replaces the
        definition wholesale — fetch it with `get_tool` and edit it, don't
        send a fragment."""
        user = await self._scoped_user()
        try:
            tool = await update_tool_for_user(
                tool_uuid,
                user,
                name=name,
                description=description,
                definition=tool_definition,
            )
        except PydanticValidationError as e:
            raise WorkflowGenToolboxError(
                "The tool definition didn't match the expected shape.",
                errors=_format_validation_errors(e),
            ) from e
        except ToolManagementError as e:
            raise WorkflowGenToolboxError(e.message, errors=[e.message]) from e
        return {"tool_uuid": tool.tool_uuid, "name": tool.name, "status": tool.status}

    async def list_documents(self) -> list[dict[str, Any]]:
        documents = await db_client.get_documents_for_organization(
            organization_id=self.organization_id
        )
        return [
            {
                "document_uuid": d.document_uuid,
                "filename": d.filename,
                "processing_status": d.processing_status,
                "total_chunks": d.total_chunks,
            }
            for d in documents
        ]

    async def list_credentials(self) -> list[dict[str, Any]]:
        credentials = await db_client.get_credentials_for_organization(
            organization_id=self.organization_id
        )
        return [
            {
                "credential_uuid": c.credential_uuid,
                "name": c.name,
                "description": c.description or "",
                "credential_type": c.credential_type,
            }
            for c in credentials
        ]

    async def list_recordings(self, workflow_id: int | None = None) -> list[dict[str, Any]]:
        recordings = await db_client.get_recordings(
            organization_id=self.organization_id, workflow_id=workflow_id
        )
        return [
            {
                "recording_id": r.recording_id,
                "workflow_id": r.workflow_id,
                "transcript": r.transcript,
            }
            for r in recordings
        ]

    async def create_credential(
        self,
        name: str,
        credential_type: str,
        credential_data: dict[str, Any],
        description: str | None = None,
    ) -> dict[str, Any]:
        """Store a secret as a reusable credential and return only its uuid.

        Lets the assistant handle an API key end to end — store it, then
        reference it from an HTTP tool — instead of stalling to ask the user
        to go create one by hand. The secret itself is never echoed back:
        the result carries identifying fields only, so it doesn't get
        re-persisted into the chat transcript on every later turn.
        """
        try:
            parsed_type = WebhookCredentialType(credential_type)
        except ValueError:
            allowed = ", ".join(t.value for t in WebhookCredentialType)
            raise WorkflowGenToolboxError(
                f"Unknown credential_type {credential_type!r}.",
                errors=[f"credential_type must be one of: {allowed}"],
            ) from None

        user = await self._scoped_user()
        try:
            credential = await create_credential_for_user(
                user=user,
                name=name,
                credential_type=parsed_type,
                credential_data=credential_data,
                description=description,
            )
        except CredentialManagementError as e:
            raise WorkflowGenToolboxError(e.message, errors=[e.message]) from e
        return credential_summary(credential)

    async def create_tool(self, tool_definition: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed_request = CreateToolRequest.model_validate(tool_definition)
        except PydanticValidationError as e:
            raise WorkflowGenToolboxError(
                "The tool definition didn't match the expected shape.",
                errors=_format_validation_errors(e),
            ) from e

        user = await self._scoped_user()

        try:
            tool = await create_tool_for_user(parsed_request, user, source="workflow_gen")
        except ToolManagementError as e:
            raise WorkflowGenToolboxError(e.message) from e

        return {"tool_uuid": tool.tool_uuid, "name": tool.name, "status": tool.status}

    # ------------------------------------------------------------------
    # Voice-prompting guide (global, not org-scoped)
    # ------------------------------------------------------------------

    async def get_voice_prompting_guide(
        self,
        stage: str | None = None,
        topic: str | None = None,
        node_type: str | None = None,
    ) -> dict[str, Any]:
        if topic is not None:
            atom = get_topic(topic)
            if atom is None:
                raise WorkflowGenToolboxError(f"Unknown voice-prompting topic: {topic!r}")
            return atom.to_deep_dict()
        if stage is not None:
            try:
                stage_enum = Stage(stage)
            except ValueError:
                raise WorkflowGenToolboxError(f"Unknown stage: {stage!r}") from None
            return build_briefing(stage_enum, node_type=node_type)
        return {"topics": list_topic_index()}

    # ------------------------------------------------------------------
    # Docs (global, not org-scoped)
    # ------------------------------------------------------------------

    async def search_docs(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return await _search_docs(query, limit)

    async def read_doc(self, path: str, section: str | None = None) -> dict[str, Any]:
        return await _read_doc(path, section)

    async def list_docs(self, path: str | None = None, depth: int = 1) -> list[dict[str, Any]]:
        return await _list_docs(path, depth)
