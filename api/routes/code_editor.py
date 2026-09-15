"""Code Editor routes.

  GET    /code-editor/files                 the draft tree
  GET    /code-editor/files/{path}          one file
  PUT    /code-editor/files/{path}          save (validated; invalid is refused)
  DELETE /code-editor/files/{path}
  GET    /code-editor/env                   keys + hints, never values
  PUT    /code-editor/env/{key}
  DELETE /code-editor/env/{key}
  POST   /code-editor/test-run              run the DRAFT against a payload
  POST   /code-editor/test-run-deployed     run whatever is currently DEPLOYED
  GET    /code-editor/versions
  POST   /code-editor/versions              snapshot the draft
  POST   /code-editor/versions/{n}/deploy   reconcile tools, stamp deployed
  POST   /code-editor/run/{function_name}   runtime entry point for deployed tools

Handlers stay thin — everything real is in services/code_editor/.
"""

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from api.db import db_client
from api.db.models import UserModel
from api.services.auth.depends import get_user, get_user_with_selected_organization
from api.services.code_editor import secrets, workspace
from api.services.code_editor.deploy import DeployError, deploy_version
from api.services.code_editor.workspace import WorkspaceError

router = APIRouter(prefix="/code-editor", tags=["code-editor"])

# The address the API reaches *itself* on, inside the compose network. The
# generated tools call back here, and the request originates from the api
# container, so this is an internal service name rather than the public host.
API_INTERNAL_URL = os.environ.get("API_INTERNAL_URL", "http://localhost:8000")


class FileResponse(BaseModel):
    path: str
    content: str


class SaveFileRequest(BaseModel):
    content: str


class SaveFileResponse(BaseModel):
    path: str
    warnings: list[str] = []


class EnvVarResponse(BaseModel):
    key: str
    hint: Optional[str] = None
    # None for a row saved before this was tracked — no plaintext left to
    # measure. The UI falls back to a fixed-width mask in that case.
    length: Optional[int] = None


class SetEnvVarRequest(BaseModel):
    value: str = Field(min_length=1)


class TestRunRequest(BaseModel):
    event: dict[str, Any]
    timeout_seconds: float = Field(default=10.0, ge=1, le=60)


class CreateVersionRequest(BaseModel):
    description: Optional[str] = None


class VersionResponse(BaseModel):
    version_number: int
    description: Optional[str]
    created_at: Any
    deployed_at: Any = None
    file_count: int


def _org(user: UserModel) -> int:
    if not user.selected_organization_id:
        raise HTTPException(status_code=400, detail="No organization selected.")
    return user.selected_organization_id


def _bad_request(exc: WorkspaceError) -> HTTPException:
    # Validation errors are the product here, not an internal failure — the
    # editor renders them inline against the file being saved.
    return HTTPException(
        status_code=400, detail={"message": exc.message, "errors": exc.errors}
    )


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------


@router.get("/files", response_model=list[FileResponse])
async def list_files(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[FileResponse]:
    files = await workspace.ensure_workspace(_org(user))
    return [FileResponse(path=p, content=c) for p, c in sorted(files.items())]


@router.get("/files/{path:path}", response_model=FileResponse)
async def get_file(
    path: str, user: UserModel = Depends(get_user_with_selected_organization)
) -> FileResponse:
    file = await db_client.get_code_editor_file(_org(user), path)
    if file is None:
        raise HTTPException(status_code=404, detail=f"{path} not found.")
    return FileResponse(path=file.path, content=file.content)


@router.put("/files/{path:path}", response_model=SaveFileResponse)
async def save_file(
    path: str,
    request: SaveFileRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> SaveFileResponse:
    try:
        result = await workspace.save_file(_org(user), path, request.content, user.id)
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc
    return SaveFileResponse(path=path, warnings=result.warnings)


@router.delete("/files/{path:path}")
async def delete_file(
    path: str, user: UserModel = Depends(get_user_with_selected_organization)
) -> dict[str, bool]:
    try:
        deleted = await workspace.delete_file(_org(user), path)
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"{path} not found.")
    return {"deleted": True}


# ----------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------


@router.get("/env", response_model=list[EnvVarResponse])
async def list_env(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[EnvVarResponse]:
    rows = await workspace.list_env_vars(_org(user))
    return [
        EnvVarResponse(key=r["key"], hint=r["hint"], length=r["length"]) for r in rows
    ]


@router.put("/env/{key}", response_model=EnvVarResponse)
async def set_env(
    key: str,
    request: SetEnvVarRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> EnvVarResponse:
    if not secrets.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "CODE_EDITOR_ENCRYPTION_KEY is not configured on this deployment, "
                "so secrets cannot be stored."
            ),
        )
    try:
        await workspace.set_env_var(_org(user), key, request.value)
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc
    return EnvVarResponse(
        key=key, hint=secrets.hint(request.value), length=len(request.value)
    )


@router.delete("/env/{key}")
async def delete_env(
    key: str, user: UserModel = Depends(get_user_with_selected_organization)
) -> dict[str, bool]:
    deleted = await db_client.delete_code_editor_env_var(_org(user), key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"{key} not found.")
    return {"deleted": True}


# ----------------------------------------------------------------------
# Test run
# ----------------------------------------------------------------------


@router.post("/test-run")
async def test_run(
    request: TestRunRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> dict[str, Any]:
    """Run the draft. Never the deployed version — see workspace.run_test."""
    try:
        return await workspace.run_test(
            _org(user), request.event, timeout_seconds=request.timeout_seconds
        )
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc


@router.post("/test-run-deployed")
async def test_run_deployed(
    request: TestRunRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> dict[str, Any]:
    """Run whatever is currently LIVE, untouched by unsaved draft edits.

    A sibling of `/test-run`, not a variant of `/run/{function_name}`: this is
    a developer checking their own deployed code from the editor, so it is
    session-authenticated like every other editor route and returns the full
    execution envelope (statusCode/result/logs/error/traceback) — unlike the
    runtime route, which collapses failures into a speakable message for the
    model and is never what a human debugging a run wants to see.

    No workflow_id/workflow_run_id/caller_number: there is no real call behind
    an interactive test, so the context an org's code sees here is honestly
    org-only, same as `/test-run`.
    """
    organization_id = _org(user)
    version = await db_client.get_deployed_code_editor_version(organization_id)
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="Nothing has been deployed yet for this organization.",
        )
    try:
        return await workspace.run_test(
            organization_id,
            request.event,
            files=version.files or {},
            timeout_seconds=request.timeout_seconds,
        )
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc


# ----------------------------------------------------------------------
# Versions and deploy
# ----------------------------------------------------------------------


@router.get("/versions", response_model=list[VersionResponse])
async def list_versions(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> list[VersionResponse]:
    versions = await db_client.list_code_editor_versions(_org(user))
    return [
        VersionResponse(
            version_number=v.version_number,
            description=v.description,
            created_at=v.created_at,
            deployed_at=v.deployed_at,
            file_count=len(v.files or {}),
        )
        for v in versions
    ]


@router.post("/versions", response_model=VersionResponse)
async def create_version(
    request: CreateVersionRequest,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> VersionResponse:
    try:
        version = await workspace.create_version(
            _org(user), request.description, user.id
        )
    except WorkspaceError as exc:
        raise _bad_request(exc) from exc
    return VersionResponse(
        version_number=version.version_number,
        description=version.description,
        created_at=version.created_at,
        deployed_at=version.deployed_at,
        file_count=len(version.files or {}),
    )


@router.post("/versions/{version_number}/deploy")
async def deploy(
    version_number: int,
    user: UserModel = Depends(get_user_with_selected_organization),
) -> dict[str, Any]:
    try:
        report = await deploy_version(
            _org(user), version_number, api_url=API_INTERNAL_URL, user_id=user.id
        )
    except DeployError as exc:
        raise HTTPException(
            status_code=400, detail={"message": exc.message, "errors": exc.errors}
        ) from exc
    return report.as_dict()


# ----------------------------------------------------------------------
# Runtime
# ----------------------------------------------------------------------


@router.post("/run/{function_name}")
async def run_deployed_function(
    function_name: str,
    payload: dict[str, Any],
    user: UserModel = Depends(get_user),
) -> dict[str, Any]:
    """Execute a deployed function. This is what a generated tool calls.

    The organization comes from the authenticated caller, never from the
    payload. A tool config is editable by any org admin, so trusting an org id
    in the body would let one organization run another's code.

    Runs the **deployed** snapshot, not the draft: an agent on a live call must
    not be affected by whatever someone happens to be editing.
    """
    organization_id = _org(user)
    version = await db_client.get_deployed_code_editor_version(organization_id)
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="No Code Editor version has been deployed for this organization.",
        )

    # These two travel in the request body because that's the only channel a
    # live call's tool invocation actually crosses — see
    # `_inject_code_editor_context` in `services/workflow/tools/custom_tool.py`.
    # Popped out before the rest becomes `event`, so they land in `context`
    # (as the router's own docstring promises) instead of being mistaken for
    # one of the model's declared parameters.
    workflow_id = payload.pop("_dograh_workflow_id", None)
    workflow_run_id = payload.pop("_dograh_workflow_run_id", None)

    # The platform injects these, but this route is reachable by anyone holding
    # the organization's runtime key, so the body is not a trusted channel. The
    # generated router's docstring invites customers to branch on
    # `context["workflow_id"]`, which makes an unvalidated value here a way to
    # drive someone's own code down a branch reserved for a different agent —
    # or to stamp a record with a workflow_run_id belonging to another
    # organization's call. Anything that doesn't belong to the caller is
    # dropped rather than rejected: a 4xx here would fail a live call over a
    # field the customer's code is free to ignore.
    if workflow_id is not None:
        owner = await db_client.get_workflow_organization_id(workflow_id)
        if owner != organization_id:
            logger.warning(
                f"Dropping _dograh_workflow_id={workflow_id} on a run for "
                f"organization {organization_id}: it belongs to {owner}."
            )
            workflow_id = None
            workflow_run_id = None
    elif workflow_run_id is not None:
        # A run id without the workflow it belongs to cannot be checked, and an
        # unverifiable identity is worth less than no identity.
        workflow_run_id = None

    if workflow_run_id is not None:
        # Owning the workflow does not imply owning the run — this is the
        # second half of the same check, not a repeat of it.
        run = await db_client.get_workflow_run(
            workflow_run_id, organization_id=organization_id
        )
        if run is None or run.workflow_id != workflow_id:
            logger.warning(
                f"Dropping _dograh_workflow_run_id={workflow_run_id} on a run "
                f"for organization {organization_id}: it is not a run of "
                f"workflow {workflow_id}."
            )
            workflow_run_id = None

    event = {**payload, "function_name": function_name}
    try:
        outcome = await workspace.run_test(
            organization_id,
            event,
            files=version.files or {},
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
        )
    except WorkspaceError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    # The tool result reaches the model and the transcript, so hand back the
    # handler's own return value rather than the execution envelope.
    if outcome.get("statusCode") == 200:
        return outcome.get("result") or {}
    return {
        "status": "error",
        "speak": "I wasn't able to complete that just now.",
        "message": outcome.get("error") or "The function failed.",
    }
