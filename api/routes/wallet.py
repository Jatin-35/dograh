from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.db.wallet_client import WALLET_PULSE_SECONDS_OPTIONS
from api.enums import WorkflowBillingMode
from api.services.auth.depends import get_superuser, get_user_with_selected_organization

router = APIRouter(prefix="/wallet", tags=["wallet"])


class WalletSettingsResponse(BaseModel):
    organization_id: int
    wallet_currency: str
    wallet_balance: Decimal
    credit_limit: Decimal
    wallet_enabled: bool


class UpdateWalletSettingsRequest(BaseModel):
    wallet_currency: Optional[str] = None
    credit_limit: Optional[Decimal] = None
    wallet_enabled: Optional[bool] = None


class WalletTopupRequest(BaseModel):
    amount: Decimal
    note: Optional[str] = None


class WalletAdjustRequest(BaseModel):
    amount: Decimal
    note: str


class WalletTransactionResponse(BaseModel):
    id: int
    organization_id: int
    amount: Decimal
    currency: str
    type: str
    balance_after: Optional[Decimal]
    workflow_run_id: Optional[int]
    workflow_id: Optional[int]
    campaign_id: Optional[int]
    created_by_user_id: Optional[int]
    note: Optional[str]
    created_at: datetime


class WalletTransactionsListResponse(BaseModel):
    transactions: List[WalletTransactionResponse]
    total_count: int
    page: int
    limit: int


class UpdateWorkflowBillingSettingsRequest(BaseModel):
    avg_call_duration_minutes: Optional[Decimal] = None
    price_per_minute: Optional[Decimal] = None
    # Which pricing mode is active for this agent — exactly one of
    # price_per_minute/price_per_call is read at billing time (see
    # WorkflowBillingMode). Switching modes doesn't clear the other mode's
    # rate field.
    billing_mode: Optional[str] = None
    price_per_call: Optional[Decimal] = None
    # Per-minute pulse size in seconds (0 = exact, pay-as-you-go). See
    # WALLET_PULSE_SECONDS_OPTIONS.
    pulse_seconds: Optional[int] = None


class WorkflowBillingSettingsResponse(BaseModel):
    workflow_id: int
    avg_call_duration_minutes: Optional[Decimal]
    price_per_minute: Optional[Decimal]
    billing_mode: str
    price_per_call: Optional[Decimal]
    pulse_seconds: int


def _to_settings_response(org) -> WalletSettingsResponse:
    return WalletSettingsResponse(
        organization_id=org.id,
        wallet_currency=org.wallet_currency,
        wallet_balance=org.wallet_balance,
        credit_limit=org.credit_limit,
        wallet_enabled=org.wallet_enabled,
    )


def _to_transaction_response(tx) -> WalletTransactionResponse:
    return WalletTransactionResponse(
        id=tx.id,
        organization_id=tx.organization_id,
        amount=tx.amount,
        currency=tx.currency,
        type=tx.type,
        balance_after=tx.balance_after,
        workflow_run_id=tx.workflow_run_id,
        workflow_id=tx.workflow_run.workflow_id if tx.workflow_run else None,
        campaign_id=tx.campaign_id,
        created_by_user_id=tx.created_by_user_id,
        note=tx.note,
        created_at=tx.created_at,
    )


# ---------------------------------------------------------------------------
# Superadmin endpoints — configure and fund an org's wallet.
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}")
async def get_wallet_settings(
    organization_id: int,
    user: UserModel = Depends(get_superuser),
) -> WalletSettingsResponse:
    org = await db_client.get_organization_by_id(organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _to_settings_response(org)


@router.patch("/organizations/{organization_id}")
async def update_wallet_settings(
    organization_id: int,
    request: UpdateWalletSettingsRequest,
    user: UserModel = Depends(get_superuser),
) -> WalletSettingsResponse:
    if request.credit_limit is not None and request.credit_limit < 0:
        raise HTTPException(status_code=400, detail="credit_limit cannot be negative")

    org = await db_client.update_wallet_settings(
        organization_id=organization_id,
        wallet_currency=request.wallet_currency,
        credit_limit=request.credit_limit,
        wallet_enabled=request.wallet_enabled,
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _to_settings_response(org)


@router.post("/organizations/{organization_id}/topup")
async def topup_wallet(
    organization_id: int,
    request: WalletTopupRequest,
    user: UserModel = Depends(get_superuser),
) -> WalletSettingsResponse:
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Topup amount must be positive")

    try:
        await db_client.wallet_topup(
            organization_id=organization_id,
            amount=request.amount,
            created_by_user_id=user.id,
            note=request.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    org = await db_client.get_organization_by_id(organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _to_settings_response(org)


@router.post("/organizations/{organization_id}/adjust")
async def adjust_wallet(
    organization_id: int,
    request: WalletAdjustRequest,
    user: UserModel = Depends(get_superuser),
) -> WalletSettingsResponse:
    if request.amount == 0:
        raise HTTPException(status_code=400, detail="Adjustment amount must be non-zero")
    if not request.note.strip():
        raise HTTPException(
            status_code=400, detail="A note is required for manual adjustments"
        )

    try:
        await db_client.wallet_adjust(
            organization_id=organization_id,
            amount=request.amount,
            created_by_user_id=user.id,
            note=request.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    org = await db_client.get_organization_by_id(organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _to_settings_response(org)


@router.get("/organizations/{organization_id}/transactions")
async def list_organization_wallet_transactions(
    organization_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: UserModel = Depends(get_superuser),
) -> WalletTransactionsListResponse:
    offset = (page - 1) * limit
    transactions, total_count = await db_client.list_wallet_transactions(
        organization_id=organization_id, limit=limit, offset=offset
    )
    return WalletTransactionsListResponse(
        transactions=[_to_transaction_response(tx) for tx in transactions],
        total_count=total_count,
        page=page,
        limit=limit,
    )


@router.patch("/workflows/{workflow_id}/billing-settings")
async def update_workflow_billing_settings(
    workflow_id: int,
    request: UpdateWorkflowBillingSettingsRequest,
    user: UserModel = Depends(get_superuser),
) -> WorkflowBillingSettingsResponse:
    if request.avg_call_duration_minutes is not None and request.avg_call_duration_minutes <= 0:
        raise HTTPException(
            status_code=400, detail="avg_call_duration_minutes must be positive"
        )
    if request.price_per_minute is not None and request.price_per_minute <= 0:
        raise HTTPException(status_code=400, detail="price_per_minute must be positive")
    if request.price_per_call is not None and request.price_per_call <= 0:
        raise HTTPException(status_code=400, detail="price_per_call must be positive")
    valid_modes = {m.value for m in WorkflowBillingMode}
    if request.billing_mode is not None and request.billing_mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"billing_mode must be one of {sorted(valid_modes)}",
        )
    if (
        request.pulse_seconds is not None
        and request.pulse_seconds not in WALLET_PULSE_SECONDS_OPTIONS
    ):
        raise HTTPException(
            status_code=400,
            detail=f"pulse_seconds must be one of {list(WALLET_PULSE_SECONDS_OPTIONS)}",
        )

    workflow = await db_client.update_workflow_billing_settings(
        workflow_id=workflow_id,
        avg_call_duration_minutes=request.avg_call_duration_minutes,
        price_per_minute=request.price_per_minute,
        billing_mode=request.billing_mode,
        price_per_call=request.price_per_call,
        pulse_seconds=request.pulse_seconds,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowBillingSettingsResponse(
        workflow_id=workflow.id,
        avg_call_duration_minutes=workflow.avg_call_duration_minutes,
        price_per_minute=workflow.price_per_minute,
        billing_mode=workflow.billing_mode,
        price_per_call=workflow.price_per_call,
        pulse_seconds=workflow.pulse_seconds,
    )


# ---------------------------------------------------------------------------
# Client (org-member) endpoints — a client's view of their own wallet.
# ---------------------------------------------------------------------------


@router.get("/me")
async def get_my_wallet(
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WalletSettingsResponse:
    org = await db_client.get_organization_by_id(user.selected_organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _to_settings_response(org)


@router.get("/me/transactions")
async def list_my_wallet_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: UserModel = Depends(get_user_with_selected_organization),
) -> WalletTransactionsListResponse:
    offset = (page - 1) * limit
    transactions, total_count = await db_client.list_wallet_transactions(
        organization_id=user.selected_organization_id, limit=limit, offset=offset
    )
    return WalletTransactionsListResponse(
        transactions=[_to_transaction_response(tx) for tx in transactions],
        total_count=total_count,
        page=page,
        limit=limit,
    )
