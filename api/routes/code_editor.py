"""Code Editor routes.

  GET    /code-editor/files                 the draft tree
  GET    /code-editor/files/{path}          one file
  PUT    /code-editor/files/{path}          save (validated; invalid is refused)
  DELETE /code-editor/files/{path}
  GET    /code-editor/env                   keys + hints, never values
  PUT    /code-editor/env/{key}
  DELETE /code-editor/env/{key}
  POST   /code-editor/test-run              run the DRAFT against a payload
  GET    /code-editor/versions
  POST   /code-editor/versions              snapshot the draft
  POST   /code-editor/versions/{n}/deploy   reconcile tools, stamp deployed
  POST   /code-editor/run/{function_name}   runtime entry point for deployed tools

Handlers stay thin — everything real is in services/code_editor/.
"""

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
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
    return [EnvVarResponse(key=r["key"], hint=r["hint"]) for r in rows]


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
    return EnvVarResponse(key=key, hint=secrets.hint(request.value))


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

    event = {**payload, "function_name": function_name}
    try:
        outcome = await workspace.run_test(
            organization_id, event, files=version.files or {}
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
