"""Wallet reservation/reconciliation hooks for campaigns.

A campaign's estimated cost is reserved from its org's wallet in one lump
sum once its row count is known (at launch for redials, after source sync
for normal campaigns) — see WalletClient.wallet_reserve_for_campaign. Each
completed call then records its actual cost against that reservation
(WalletClient.wallet_record_campaign_call_cost, wired in via
workflow_run_billing.py). When the campaign reaches any terminal state
(completed/failed/cancelled), the reservation is trued up against actual
spend and the difference refunded or debited.

Both hooks are no-ops when the org's wallet is off, or the workflow's
active billing_mode has no rate configured for it (price_per_minute +
avg_call_duration_minutes for PER_MINUTE, price_per_call for PER_CALL).
"""

from decimal import ROUND_HALF_UP, Decimal

from loguru import logger

from api.db import db_client
from api.db.models import CampaignModel, WorkflowModel
from api.db.wallet_client import InsufficientBalanceError, billable_seconds
from api.enums import WorkflowBillingMode


def _estimate_campaign_cost(workflow: WorkflowModel, total_rows: int) -> Decimal | None:
    """Estimated upfront reservation for total_rows calls on this workflow,
    or None if its active billing_mode isn't fully configured yet."""
    if workflow.billing_mode == WorkflowBillingMode.PER_CALL.value:
        if workflow.price_per_call is None:
            return None
        return (Decimal(total_rows) * workflow.price_per_call).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

    if workflow.avg_call_duration_minutes is None or workflow.price_per_minute is None:
        return None
    # Round the average call up to the agent's pulse, as billing will. Each
    # real call rounds up on its own, so on a pulsed agent actual spend still
    # tends to land a little above this; the terminal reconcile debits the
    # difference.
    billed_minutes = (
        billable_seconds(
            workflow.avg_call_duration_minutes * 60, workflow.pulse_seconds or 0
        )
        / 60
    )
    return (
        Decimal(total_rows) * billed_minutes * workflow.price_per_minute
    ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


async def reserve_campaign_wallet(campaign: CampaignModel, *, total_rows: int) -> None:
    """Reserve this campaign's estimated cost from its org's wallet.

    Raises ValueError if the org can't cover the estimate — callers should
    fail the campaign launch/sync on this, matching the ValueError
    convention the rest of the campaign lifecycle already uses. No-ops if
    wallet billing isn't configured or there's nothing to bill.
    """
    if total_rows <= 0:
        return

    org = await db_client.get_organization_by_id(campaign.organization_id)
    if org is None or not org.wallet_enabled:
        return

    workflow = await db_client.get_workflow_by_id(campaign.workflow_id)
    if workflow is None:
        return

    estimated_cost = _estimate_campaign_cost(workflow, total_rows)
    if estimated_cost is None:
        logger.warning(
            "Campaign {} launching without a wallet reservation: workflow {} has no "
            "rate configured for its billing_mode ({})",
            campaign.id,
            campaign.workflow_id,
            workflow.billing_mode,
        )
        return

    try:
        await db_client.wallet_reserve_for_campaign(
            organization_id=campaign.organization_id,
            campaign_id=campaign.id,
            estimated_cost=estimated_cost,
        )
    except InsufficientBalanceError as e:
        raise ValueError(
            f"Insufficient wallet balance to launch campaign: needs {e.required}, "
            f"available {e.available}"
        ) from e


async def reconcile_campaign_wallet(campaign_id: int) -> None:
    """Best-effort true-up of a campaign's wallet reservation once it
    reaches a terminal state. Never raises — a failure here shouldn't
    block or undo the campaign's own state transition; it's logged for
    manual follow-up instead. No-ops if the campaign was never reserved.
    """
    try:
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if campaign is None:
            return
        await db_client.wallet_reconcile_campaign(
            organization_id=campaign.organization_id,
            campaign_id=campaign_id,
        )
    except Exception as e:
        logger.error("Failed to reconcile wallet for campaign {}: {}", campaign_id, e)
