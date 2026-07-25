import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel

from api.constants import PUBLIC_BASE_URL, UI_APP_URL
from api.db import db_client
from api.db.models import UserModel
from api.enums import OrganizationStatus
from api.services.auth.depends import get_superuser
from api.services.auth.stack_auth import (
    StackAuthSessionError,
    StackAuthTeamError,
    StackAuthUserSearchError,
    stackauth,
)

router = APIRouter(prefix="/superuser", tags=["superuser"])


class ImpersonateRequest(BaseModel):
    """Request payload for superadmin impersonation.

    ``provider_user_id``, ``user_id``, or ``email`` may be supplied. If more
    than one is provided, ``provider_user_id`` takes precedence, followed by
    ``user_id`` and then ``email``.

    ``target_organization_id``, when supplied, forces the target account's
    Stack-selected team to that organization before impersonating — without
    it, the impersonated session lands wherever that account's own selected
    team already was, which is only guaranteed correct if the account belongs
    to exactly one team. Needed whenever the caller relies on landing in a
    *specific* organization (e.g. deep-linking straight to one of its
    workflows) rather than just "however that user happens to be logged in".
    """

    provider_user_id: str | None = None
    user_id: int | None = None
    email: str | None = None
    target_organization_id: int | None = None


class ImpersonateResponse(BaseModel):
    refresh_token: str
    access_token: str


class SuperuserWorkflowRunResponse(BaseModel):
    id: int
    name: str
    workflow_id: int
    workflow_name: Optional[str]
    user_id: Optional[int]
    organization_id: Optional[int]
    organization_name: Optional[str]
    mode: str
    is_completed: bool
    recording_url: Optional[str]
    transcript_url: Optional[str]
    usage_info: Optional[dict]
    cost_info: Optional[dict]
    initial_context: Optional[dict]
    gathered_context: Optional[dict]
    created_at: datetime


class SuperuserWorkflowRunsListResponse(BaseModel):
    workflow_runs: List[SuperuserWorkflowRunResponse]
    total_count: int
    page: int
    limit: int
    total_pages: int


class SuperuserOrganizationResponse(BaseModel):
    id: int
    provider_id: str
    name: Optional[str]
    primary_contact_email: Optional[str]
    status: str
    created_at: datetime
    user_count: int


class SuperuserOrganizationsListResponse(BaseModel):
    organizations: List[SuperuserOrganizationResponse]
    total_count: int


class UpdateOrganizationStatusRequest(BaseModel):
    status: str


class CreateOrganizationRequest(BaseModel):
    name: str
    email: str


class CreateOrganizationResponse(BaseModel):
    organization: SuperuserOrganizationResponse
    invitation_sent: bool


class SuperuserWorkflowResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    total_runs: int
    folder_id: Optional[int]
    folder_name: Optional[str]
    organization_id: int
    organization_name: Optional[str]
    organization_provider_id: str
    organization_status: str
    organization_primary_contact_email: Optional[str]


class SuperuserWorkflowsListResponse(BaseModel):
    workflows: List[SuperuserWorkflowResponse]
    total_count: int


@router.post("/impersonate")
async def impersonate(
    request: ImpersonateRequest, user: UserModel = Depends(get_superuser)
) -> ImpersonateResponse:
    """Impersonate a user as a super-admin.
    Internally, Stack Auth requires the **provider user ID** (a UUID-ish string)
    to create an impersonation session.
    """

    provider_user_id = (
        request.provider_user_id.strip() if request.provider_user_id else None
    ) or None
    email = request.email.strip().lower() if request.email else None

    # ------------------------------------------------------------------
    # Fallback: resolve provider_user_id from internal ``user_id`` or email.
    # ------------------------------------------------------------------
    if provider_user_id is None:
        if request.user_id is not None:
            db_user = await db_client.get_user_by_id(request.user_id)

            if db_user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User with ID {request.user_id} not found.",
                )

            provider_user_id = db_user.provider_id
        elif email:
            db_user = await db_client.get_user_by_email(email)

            if db_user is not None:
                provider_user_id = db_user.provider_id
            else:
                try:
                    stack_users = await stackauth.find_users_by_email(email)
                except StackAuthUserSearchError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Failed to search Stack Auth users.",
                    ) from exc

                if len(stack_users) == 1 and isinstance(stack_users[0].get("id"), str):
                    provider_user_id = stack_users[0]["id"]
                elif len(stack_users) > 1:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Multiple Stack Auth users matched that email.",
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"User with email {email} not found.",
                    )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "One of 'provider_user_id', 'user_id', or 'email' must be provided."
                ),
            )

    # ------------------------------------------------------------------
    # If a target organization was specified, force the account's selected
    # team to it first — otherwise the impersonated session inherits
    # whatever that account's own selected team already was, which isn't
    # guaranteed to be the org we actually want. A hard failure here (rather
    # than impersonating anyway) is deliberate: better to fail loudly than
    # silently land in the wrong organization.
    # ------------------------------------------------------------------
    if request.target_organization_id is not None:
        target_org = await db_client.get_organization_by_id(
            request.target_organization_id
        )
        if target_org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization with ID {request.target_organization_id} not found.",
            )
        try:
            await stackauth.set_user_selected_team(
                provider_user_id, target_org.provider_id
            )
        except StackAuthTeamError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to set the target organization for impersonation.",
            ) from exc

    # ------------------------------------------------------------------
    # Call Stack Auth to create the impersonation session
    # ------------------------------------------------------------------
    try:
        session = await stackauth.impersonate(provider_user_id)
    except StackAuthSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create Stack Auth impersonation session.",
        ) from exc

    if (
        not isinstance(session, dict)
        or "refresh_token" not in session
        or "access_token" not in session
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create Stack Auth impersonation session.",
        )

    return ImpersonateResponse(
        refresh_token=session["refresh_token"],
        access_token=session["access_token"],
    )


@router.get("/workflow-runs")
async def get_workflow_runs(
    page: int = Query(1, ge=1, description="Page number (starts from 1)"),
    limit: int = Query(50, ge=1, le=100, description="Number of items per page"),
    filters: Optional[str] = Query(None, description="JSON-encoded filter criteria"),
    sort_by: Optional[str] = Query(
        None, description="Field to sort by (e.g., 'duration', 'created_at')"
    ),
    sort_order: Optional[str] = Query(
        "desc", description="Sort order ('asc' or 'desc')"
    ),
    user: UserModel = Depends(get_superuser),
) -> SuperuserWorkflowRunsListResponse:
    """
    Get paginated list of all workflow runs with organization information.
    Requires superuser privileges.

    Filters should be provided as a JSON-encoded array of filter criteria.
    Example: [{"field": "id", "type": "number", "value": {"value": 680}}]
    """
    offset = (page - 1) * limit

    # Parse filters if provided
    filter_criteria = None
    if filters:
        try:
            filter_criteria = json.loads(filters)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid filter format")

    # Validate sort_order
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    workflow_runs, total_count = await db_client.get_workflow_runs_for_superadmin(
        limit=limit,
        offset=offset,
        filters=filter_criteria,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = (total_count + limit - 1) // limit  # Ceiling division

    return SuperuserWorkflowRunsListResponse(
        workflow_runs=[SuperuserWorkflowRunResponse(**run) for run in workflow_runs],
        total_count=total_count,
        page=page,
        limit=limit,
        total_pages=total_pages,
    )


@router.get("/organizations")
async def list_organizations(
    user: UserModel = Depends(get_superuser),
) -> SuperuserOrganizationsListResponse:
    """List all organizations with member counts. Requires superuser privileges."""
    organizations = await db_client.list_organizations_for_superadmin()
    return SuperuserOrganizationsListResponse(
        organizations=[
            SuperuserOrganizationResponse(**org) for org in organizations
        ],
        total_count=len(organizations),
    )


@router.post("/organizations")
async def create_organization(
    request: CreateOrganizationRequest,
    user: UserModel = Depends(get_superuser),
) -> CreateOrganizationResponse:
    """Provision a new client organization and email the client an invite.

    Flow (1B): create a Stack team (with the superadmin added as a member so
    they can build workflows before the client joins) -> persist a local
    organization row as pending_setup -> email the client a team invitation.
    Requires superuser privileges.
    """
    name = request.name.strip()
    email = request.email.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name is required.")
    if not email:
        raise HTTPException(status_code=400, detail="Client email is required.")

    # 1. Create the Stack team. Add the superadmin as a member via
    #    creator_user_id so they can select the team and build flows immediately.
    try:
        team = await stackauth.create_team(
            display_name=name,
            creator_user_id=user.provider_id,
        )
    except StackAuthTeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create the organization in the auth provider.",
        ) from exc

    team_id = team["id"]

    # 2. Persist the local organization row (pending_setup) with the superadmin
    #    as a member and a default API key.
    try:
        organization = await db_client.create_client_organization(
            provider_id=team_id,
            name=name,
            primary_contact_email=email,
            superadmin_user_id=user.id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Auth team created but local organization insert failed: {exc}",
        ) from exc

    # 3. Email the client an invitation. A failure here is non-fatal — the org
    #    exists and the client can be re-invited later; surface it to the UI.
    callback_base = PUBLIC_BASE_URL or UI_APP_URL
    invitation_sent = True
    try:
        await stackauth.send_team_invitation(
            team_id=team_id,
            email=email,
            callback_url=f"{callback_base.rstrip('/')}/handler/team-invitation",
        )
    except StackAuthTeamError as exc:
        invitation_sent = False
        logger.warning(
            "Organization {} created but invite to {} failed: {}",
            team_id,
            email,
            exc,
        )

    return CreateOrganizationResponse(
        organization=SuperuserOrganizationResponse(
            id=organization.id,
            provider_id=organization.provider_id,
            name=organization.name,
            primary_contact_email=organization.primary_contact_email,
            status=organization.status,
            created_at=organization.created_at,
            user_count=1,
        ),
        invitation_sent=invitation_sent,
    )


@router.patch("/organizations/{organization_id}/status")
async def update_organization_status(
    organization_id: int,
    request: UpdateOrganizationStatusRequest,
    user: UserModel = Depends(get_superuser),
) -> SuperuserOrganizationResponse:
    """Set an organization's lifecycle status. Requires superuser privileges.

    Suspending an org blocks its members at login (see get_user in
    services/auth/depends.py).
    """
    valid_statuses = {status.value for status in OrganizationStatus}
    if request.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}",
        )

    organization = await db_client.update_organization_status(
        organization_id, request.status
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization with ID {organization_id} not found.",
        )

    user_count = len(await db_client.get_organization_users(organization_id))
    return SuperuserOrganizationResponse(
        id=organization.id,
        provider_id=organization.provider_id,
        name=organization.name,
        primary_contact_email=organization.primary_contact_email,
        status=organization.status,
        created_at=organization.created_at,
        user_count=user_count,
    )


@router.get("/workflows")
async def list_workflows(
    user: UserModel = Depends(get_superuser),
) -> SuperuserWorkflowsListResponse:
    """List every active workflow across every organization, for the
    superadmin agent browser. Requires superuser privileges.
    """
    workflows = await db_client.list_workflows_for_superadmin()
    return SuperuserWorkflowsListResponse(
        workflows=[SuperuserWorkflowResponse(**wf) for wf in workflows],
        total_count=len(workflows),
    )
