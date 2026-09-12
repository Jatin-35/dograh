"""Service layer for reusable tool management.

Routes and MCP tools both use this module so validation, credential
scoping, MCP discovery, and analytics stay consistent.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from loguru import logger

from api.db import db_client
from api.db.models import UserModel
from api.enums import PostHogEvent, ToolCategory
from api.schemas.tool import (
    CreatedByResponse,
    CreateToolRequest,
    McpRefreshResponse,
    ToolResponse,
    ToolTestResponse,
)
from api.services.posthog_client import capture_event
from api.services.workflow.mcp_tool_session import discover_mcp_tools
from api.services.workflow.tools.custom_tool import (
    execute_http_tool,
    serialize_query_params,
)
from api.services.workflow.tools.mcp_tool import (
    McpDefinitionError,
    validate_mcp_definition,
)


class ToolManagementError(ValueError):
    """Recoverable tool-management error with an MCP/HTTP friendly code."""

    def __init__(self, error_code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code


def build_tool_response(tool: Any, include_created_by: bool = False) -> ToolResponse:
    """Build a public response from a ToolModel-like object."""
    created_by = None
    if include_created_by and tool.created_by_user:
        created_by = CreatedByResponse(
            id=tool.created_by_user.id,
            provider_id=tool.created_by_user.provider_id,
        )

    return ToolResponse(
        id=tool.id,
        tool_uuid=tool.tool_uuid,
        name=tool.name,
        description=tool.description,
        category=tool.category,
        icon=tool.icon,
        icon_color=tool.icon_color,
        status=tool.status,
        definition=tool.definition,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
        created_by=created_by,
    )


def _credential_uuid_from_definition(definition: dict[str, Any]) -> Optional[str]:
    config = definition.get("config")
    if not isinstance(config, dict):
        return None
    credential_uuid = config.get("credential_uuid")
    return credential_uuid if isinstance(credential_uuid, str) else None


def _credential_uuids_from_definition(definition: dict[str, Any]) -> list[str]:
    credential_uuids: list[str] = []
    top_level = _credential_uuid_from_definition(definition)
    if top_level:
        credential_uuids.append(top_level)

    config = definition.get("config")
    if isinstance(config, dict):
        resolver = config.get("resolver")
        if isinstance(resolver, dict):
            resolver_credential_uuid = resolver.get("credential_uuid")
            if isinstance(resolver_credential_uuid, str) and resolver_credential_uuid:
                credential_uuids.append(resolver_credential_uuid)

    return list(dict.fromkeys(credential_uuids))


async def fetch_credential(credential_uuid: Optional[str], organization_id: int):
    """Best-effort credential lookup for MCP auth/discovery."""
    if not credential_uuid:
        return None
    try:
        return await db_client.get_credential_by_uuid(credential_uuid, organization_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Tool credential fetch failed: {e}")
        return None


async def validate_tool_credential_references(
    definition: dict[str, Any], *, organization_id: int
) -> None:
    """Ensure credential UUID references belong to the caller's organization."""
    for credential_uuid in _credential_uuids_from_definition(definition):
        credential = await db_client.get_credential_by_uuid(
            credential_uuid, organization_id
        )
        if not credential:
            raise ToolManagementError(
                "credential_not_found",
                (
                    f"Credential '{credential_uuid}' was not found in this "
                    "organization. Create it in the UI first, then retry with its "
                    "credential_uuid."
                ),
                status_code=404,
            )


async def populate_discovered_tools(
    definition: dict[str, Any], *, organization_id: int
) -> dict[str, Any]:
    """Best-effort MCP discovery before saving a tool definition.

    Non-MCP definitions pass through untouched. For MCP definitions, a dead
    server yields ``discovered_tools: []`` and does not block creation.
    """
    if not isinstance(definition, dict) or definition.get("type") != "mcp":
        return definition
    try:
        cfg = validate_mcp_definition(definition)
    except McpDefinitionError:
        return definition

    credential = await fetch_credential(cfg.get("credential_uuid"), organization_id)

    async def _run() -> list:
        try:
            return await discover_mcp_tools(
                url=cfg["url"],
                credential=credential,
                timeout_secs=cfg["timeout_secs"],
                sse_read_timeout_secs=cfg["sse_read_timeout_secs"],
            )
        except BaseException as e:  # noqa: BLE001
            logger.warning(f"MCP discovery failed; caching empty list: {e}")
            return []

    discovered = await asyncio.ensure_future(_run())
    definition["config"]["discovered_tools"] = discovered
    return definition


async def create_tool_for_user(
    request: CreateToolRequest,
    user: UserModel,
    *,
    source: str = "api",
) -> ToolResponse:
    """Create a reusable tool for the authenticated user's selected org."""
    if not user.selected_organization_id:
        raise ToolManagementError(
            "organization_required",
            "No organization selected for the user",
            status_code=400,
        )

    definition = request.definition.model_dump()
    await validate_tool_credential_references(
        definition, organization_id=user.selected_organization_id
    )
    definition = await populate_discovered_tools(
        definition,
        organization_id=user.selected_organization_id,
    )

    tool = await db_client.create_tool(
        organization_id=user.selected_organization_id,
        user_id=user.id,
        name=request.name,
        definition=definition,
        category=request.category,
        description=request.description,
        icon=request.icon,
        icon_color=request.icon_color,
    )

    capture_event(
        distinct_id=str(user.provider_id),
        event=PostHogEvent.TOOL_CREATED,
        properties={
            "tool_name": request.name,
            "tool_category": request.category,
            "source": source,
            "organization_id": user.selected_organization_id,
        },
    )

    return build_tool_response(tool)


def hint_for_status_code(
    status_code: Optional[int], configured_method: str
) -> Optional[str]:
    """Human-readable explanation for a status code a misconfigured tool
    is likely to hit. Returns None for 2xx and any code not covered."""
    if status_code == 400:
        return (
            "HTTP 400 Bad Request — the server rejected the request payload. "
            "Verify the arguments/body match what this endpoint expects."
        )
    if status_code == 401:
        return (
            "HTTP 401 Unauthorized — the request wasn't authenticated. Check "
            "the credential configured on the Authentication tab is present "
            "and valid."
        )
    if status_code == 403:
        return (
            "HTTP 403 Forbidden — authenticated, but the configured "
            "credential doesn't have permission for this endpoint/action."
        )
    if status_code == 404:
        return (
            f"HTTP 404 Not Found — verify the endpoint URL is correct and "
            f"that {configured_method} is a valid method for it."
        )
    if status_code == 405:
        return (
            f"HTTP 405 Method Not Allowed — the endpoint rejected the "
            f"configured method ({configured_method}). Verify the API expects "
            f"{configured_method} for this URL."
        )
    if status_code == 408:
        return (
            "HTTP 408 Request Timeout — the endpoint didn't respond in time. "
            "Check the endpoint is reachable, or increase Timeout (ms) if it's "
            "just slow."
        )
    if status_code == 409:
        return (
            "HTTP 409 Conflict — the endpoint rejected the request due to a "
            "conflicting resource state (e.g. duplicate create). Not "
            "necessarily a configuration problem."
        )
    if status_code == 415:
        return (
            "HTTP 415 Unsupported Media Type — check the Content-Type header "
            "matches the format this endpoint expects for the body."
        )
    if status_code == 422:
        return (
            "HTTP 422 Unprocessable Entity — the request was well-formed but "
            "the payload's structure or field types don't match what this "
            "endpoint expects. Compare your arguments against the API's "
            "documented schema."
        )
    if status_code == 429:
        return (
            "HTTP 429 Too Many Requests — the endpoint is rate-limiting. Wait "
            "and retry; not a configuration problem."
        )
    if status_code is not None and 500 <= status_code < 600:
        return (
            f"HTTP {status_code} — the endpoint itself errored. This is "
            "likely an issue on the API's side, not your tool configuration."
        )
    return None


async def test_http_tool_for_user(
    tool_uuid: str,
    user: UserModel,
    *,
    llm_params: dict[str, Any],
    preset_params: dict[str, Any],
) -> ToolTestResponse:
    """Fire a real request through an HTTP API tool and describe the result.

    Shared by the REST test endpoint and the in-product assistant, so both
    report identical status hints and request previews. This performs a live
    call to the configured endpoint — callers must treat it as a side effect,
    not a read.
    """
    if not user.selected_organization_id:
        raise ToolManagementError(
            "organization_required",
            "No organization selected for the user",
            status_code=400,
        )

    tool = await db_client.get_tool_by_uuid(
        tool_uuid, user.selected_organization_id, include_archived=True
    )
    if not tool:
        raise ToolManagementError(
            "tool_not_found", f"Tool {tool_uuid} not found", status_code=404
        )
    if tool.category != ToolCategory.HTTP_API.value:
        raise ToolManagementError(
            "not_testable", "Only HTTP API tools can be tested", status_code=400
        )

    tool_config = tool.definition.get("config", {}) if isinstance(tool.definition, dict) else {}
    configured_method = tool_config.get("method", "?")
    configured_url = tool_config.get("url", "?")

    started_at = time.perf_counter()
    result = await execute_http_tool(
        tool,
        llm_params,
        preset_params=preset_params,
        organization_id=user.selected_organization_id,
        include_request_headers=True,
    )
    duration_ms = max(0, round((time.perf_counter() - started_at) * 1000))

    status = result.get("status", "error")
    status_code = result.get("status_code")
    if status_code is not None and status_code >= 400:
        status = "error"

    # Preset values take precedence over model-supplied values, matching live
    # execution after configured preset templates have been resolved.
    resolved_arguments = {**llm_params, **preset_params}

    # Mirror execute_http_tool's own branch: POST/PUT/PATCH send the resolved
    # arguments as a JSON body; GET/DELETE send them as query params. Never both.
    request_body = None
    request_params = None
    if configured_method in ("POST", "PUT", "PATCH"):
        request_body = resolved_arguments  # keep {} so preview matches wire request
    elif resolved_arguments:
        request_params = serialize_query_params(resolved_arguments)

    return ToolTestResponse(
        status=status,
        status_code=status_code,
        data=result.get("data"),
        error=result.get("error"),
        duration_ms=duration_ms,
        hint=hint_for_status_code(status_code, configured_method),
        request_method=configured_method,
        request_url=configured_url,
        request_headers=result.get("request_headers", {}),
        request_body=request_body,
        request_params=request_params,
    )


async def update_tool_for_user(
    tool_uuid: str,
    user: UserModel,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    definition: Optional[dict[str, Any]] = None,
    icon: Optional[str] = None,
    icon_color: Optional[str] = None,
    status: Optional[str] = None,
) -> ToolResponse:
    """Update an existing tool in the user's selected org.

    Only the fields passed are changed; everything else is left as-is. A
    supplied `definition` replaces the old one wholesale, so callers must
    send the complete definition rather than a partial patch.
    """
    if not user.selected_organization_id:
        raise ToolManagementError(
            "organization_required",
            "No organization selected for the user",
            status_code=400,
        )

    if definition is not None:
        await validate_tool_credential_references(
            definition, organization_id=user.selected_organization_id
        )
        definition = await populate_discovered_tools(
            definition,
            organization_id=user.selected_organization_id,
        )

    tool = await db_client.update_tool(
        tool_uuid=tool_uuid,
        organization_id=user.selected_organization_id,
        name=name,
        description=description,
        definition=definition,
        icon=icon,
        icon_color=icon_color,
        status=status,
    )
    if not tool:
        raise ToolManagementError(
            "tool_not_found",
            f"Tool {tool_uuid} not found in this organization",
            status_code=404,
        )
    return build_tool_response(tool)


async def refresh_mcp_tool_for_user(
    tool_uuid: str,
    user: UserModel,
) -> McpRefreshResponse:
    """Refresh cached MCP catalog for a tool owned by the user's org."""
    if not user.selected_organization_id:
        raise ToolManagementError(
            "organization_required",
            "No organization selected for the user",
            status_code=400,
        )

    tool = await db_client.get_tool_by_uuid(
        tool_uuid, user.selected_organization_id, include_archived=True
    )
    if not tool:
        raise ToolManagementError("tool_not_found", "Tool not found", status_code=404)
    if tool.category != ToolCategory.MCP.value:
        raise ToolManagementError(
            "not_mcp_tool", "Tool is not an MCP tool", status_code=400
        )

    try:
        cfg = validate_mcp_definition(tool.definition)
    except McpDefinitionError as e:
        raise ToolManagementError(
            "invalid_mcp_definition",
            f"Invalid MCP definition: {e}",
            status_code=400,
        ) from e

    credential = await fetch_credential(
        cfg.get("credential_uuid"), user.selected_organization_id
    )

    try:
        discovered = await discover_mcp_tools(
            url=cfg["url"],
            credential=credential,
            timeout_secs=cfg["timeout_secs"],
            sse_read_timeout_secs=cfg["sse_read_timeout_secs"],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MCP refresh discovery failed: {e}")
        discovered = []

    if not discovered:
        error = (
            f"Could not reach the MCP server at {cfg['url']} "
            f"(or it exposes no tools). Previously cached list retained."
        )
        return McpRefreshResponse(tool_uuid=tool_uuid, discovered_tools=[], error=error)

    new_def = dict(tool.definition or {})
    new_def["config"] = {**new_def.get("config", {}), "discovered_tools": discovered}
    await db_client.update_tool(
        tool_uuid=tool_uuid,
        organization_id=user.selected_organization_id,
        definition=new_def,
    )
    return McpRefreshResponse(
        tool_uuid=tool_uuid, discovered_tools=discovered, error=None
    )
