"""Database access for the self-hosted prepaid wallet.

Every balance mutation goes through the same pattern: SELECT ... FOR UPDATE
the organization row, mutate the cached balance, insert one ledger row — all
within one transaction — so concurrent operations (two calls debiting at
once, two campaigns launching at once) can never lose an update.

Idempotency for retried operations is enforced by the partial unique
indexes on wallet_transactions (see the migration), backstopped here by
catching the resulting IntegrityError rather than relying only on a
pre-check (a pre-check alone is a check-then-act race under concurrency).
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationModel, WalletTransactionModel, WorkflowModel
from api.enums import WalletTransactionType, WorkflowBillingMode


class InsufficientBalanceError(Exception):
    """Raised when an operation would take a wallet below its credit_limit."""

    def __init__(self, organization_id: int, available: Decimal, required: Decimal):
        self.organization_id = organization_id
        self.available = available
        self.required = required
        super().__init__(
            f"Organization {organization_id} has insufficient balance: "
            f"available={available}, required={required}"
        )


# Pulse sizes a per-minute agent can bill in. 0 is pay-as-you-go: exact
# per-second billing, no rounding.
WALLET_PULSE_SECONDS_OPTIONS = (0, 15, 30, 45, 60)


def billable_seconds(duration_seconds: float | Decimal, pulse_seconds: int = 0) -> Decimal:
    """Talk time rounded up to a whole number of pulses — any part of a
    pulse is a full pulse, and there is no grace period, so a 2-second call
    on a 60-second pulse bills 60 seconds. pulse_seconds=0 returns the
    duration unchanged."""
    seconds = Decimal(str(duration_seconds))
    if not pulse_seconds:
        return seconds
    pulse = Decimal(pulse_seconds)
    pulses = (seconds / pulse).to_integral_value(rounding=ROUND_CEILING)
    return pulses * pulse


def compute_call_cost(
    duration_seconds: float, price_per_minute: Decimal, pulse_seconds: int = 0
) -> Decimal:
    """Per-minute billing: (billable seconds / 60) * rate, rounded to 4
    decimal places (matches the ledger's Numeric(14,4)). Billable seconds are
    the exact duration at pulse_seconds=0, or rounded up to the pulse."""
    minutes = billable_seconds(duration_seconds, pulse_seconds) / Decimal(60)
    cost = minutes * price_per_minute
    return cost.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def resolve_call_cost(
    *,
    billing_mode: str,
    duration_seconds: float,
    price_per_minute: Optional[Decimal],
    price_per_call: Optional[Decimal],
    pulse_seconds: int = 0,
) -> Decimal:
    """Cost for one call under whichever billing mode is active — exactly
    one of price_per_minute/price_per_call applies (see
    WorkflowBillingMode). pulse_seconds only affects per-minute billing.
    Callers must ensure the active mode's rate is actually set before
    calling this."""
    if billing_mode == WorkflowBillingMode.PER_CALL.value:
        assert price_per_call is not None
        return price_per_call.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    assert price_per_minute is not None
    return compute_call_cost(duration_seconds, price_per_minute, pulse_seconds)


def _call_charge_metadata(
    *,
    duration_seconds: float,
    billing_mode: str,
    price_per_minute: Optional[Decimal],
    price_per_call: Optional[Decimal],
    pulse_seconds: int,
) -> dict:
    """Ledger context for a call charge, so a disputed bill can be explained
    ("130s on a 60s pulse, billed as 180s") without recomputing it."""
    metadata = {
        "duration_seconds": duration_seconds,
        "billing_mode": billing_mode,
        "rate_per_minute": (
            str(price_per_minute) if price_per_minute is not None else None
        ),
        "rate_per_call": str(price_per_call) if price_per_call is not None else None,
    }
    if billing_mode != WorkflowBillingMode.PER_CALL.value:
        metadata["pulse_seconds"] = pulse_seconds
        metadata["billed_seconds"] = str(billable_seconds(duration_seconds, pulse_seconds))
    return metadata


class WalletClient(BaseDBClient):
    async def _lock_organization(self, session, organization_id: int) -> OrganizationModel:
        result = await session.execute(
            select(OrganizationModel)
            .where(OrganizationModel.id == organization_id)
            .with_for_update()
        )
        org = result.scalar_one_or_none()
        if org is None:
            raise ValueError(f"Organization {organization_id} not found")
        return org

    async def wallet_topup(
        self,
        *,
        organization_id: int,
        amount: Decimal,
        created_by_user_id: int,
        note: Optional[str] = None,
    ) -> WalletTransactionModel:
        """Superadmin adds funds. amount must be positive."""
        if amount <= 0:
            raise ValueError("Topup amount must be positive")

        async with self.async_session() as session:
            org = await self._lock_organization(session, organization_id)
            org.wallet_balance = org.wallet_balance + amount

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=amount,
                currency=org.wallet_currency,
                type=WalletTransactionType.TOPUP.value,
                balance_after=org.wallet_balance,
                created_by_user_id=created_by_user_id,
                note=note,
            )
            session.add(tx)
            await session.commit()
            await session.refresh(tx)
            return tx

    async def wallet_adjust(
        self,
        *,
        organization_id: int,
        amount: Decimal,
        created_by_user_id: int,
        note: str,
    ) -> WalletTransactionModel:
        """Superadmin manual correction. amount is signed (+/-)."""
        if amount == 0:
            raise ValueError("Adjustment amount must be non-zero")

        async with self.async_session() as session:
            org = await self._lock_organization(session, organization_id)
            org.wallet_balance = org.wallet_balance + amount

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=amount,
                currency=org.wallet_currency,
                type=WalletTransactionType.ADJUSTMENT.value,
                balance_after=org.wallet_balance,
                created_by_user_id=created_by_user_id,
                note=note,
            )
            session.add(tx)
            await session.commit()
            await session.refresh(tx)
            return tx

    async def wallet_debit_for_call(
        self,
        *,
        organization_id: int,
        workflow_run_id: int,
        duration_seconds: float,
        billing_mode: str = WorkflowBillingMode.PER_MINUTE.value,
        price_per_minute: Optional[Decimal] = None,
        price_per_call: Optional[Decimal] = None,
        pulse_seconds: int = 0,
        note: Optional[str] = None,
    ) -> Optional[WalletTransactionModel]:
        """Normal (non-campaign) per-call charge, at the calling agent's own
        rate (per-minute or flat per-call — see billing_mode). Idempotent
        per workflow_run_id — a retried completion hook can't double-charge.
        Returns None if the org's wallet is off, or if this run was already
        debited. Callers should not call this at all when the agent has no
        rate configured for its active mode (see workflow_run_billing.py).
        """
        async with self.async_session() as session:
            org = await self._lock_organization(session, organization_id)
            if not org.wallet_enabled:
                return None

            cost = resolve_call_cost(
                billing_mode=billing_mode,
                duration_seconds=duration_seconds,
                price_per_minute=price_per_minute,
                price_per_call=price_per_call,
                pulse_seconds=pulse_seconds,
            )
            org.wallet_balance = org.wallet_balance - cost

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=-cost,
                currency=org.wallet_currency,
                type=WalletTransactionType.DEBIT.value,
                balance_after=org.wallet_balance,
                workflow_run_id=workflow_run_id,
                note=note,
                transaction_metadata=_call_charge_metadata(
                    duration_seconds=duration_seconds,
                    billing_mode=billing_mode,
                    price_per_minute=price_per_minute,
                    price_per_call=price_per_call,
                    pulse_seconds=pulse_seconds,
                ),
            )
            session.add(tx)
            try:
                await session.commit()
            except IntegrityError:
                # Unique index on workflow_run_id WHERE type='debit' — this
                # run was already charged by a concurrent/retried call.
                await session.rollback()
                return None
            await session.refresh(tx)
            return tx

    async def wallet_record_campaign_call_cost(
        self,
        *,
        organization_id: int,
        campaign_id: int,
        workflow_run_id: int,
        duration_seconds: float,
        billing_mode: str = WorkflowBillingMode.PER_MINUTE.value,
        price_per_minute: Optional[Decimal] = None,
        price_per_call: Optional[Decimal] = None,
        pulse_seconds: int = 0,
        note: Optional[str] = None,
    ) -> Optional[WalletTransactionModel]:
        """Record a single call's cost *within* an already-reserved
        campaign. Ledger-only — never touches wallet_balance, since that
        money already left the wallet at reservation time. Idempotent per
        workflow_run_id.
        """
        cost = resolve_call_cost(
            billing_mode=billing_mode,
            duration_seconds=duration_seconds,
            price_per_minute=price_per_minute,
            price_per_call=price_per_call,
            pulse_seconds=pulse_seconds,
        )

        async with self.async_session() as session:
            org = await session.get(OrganizationModel, organization_id)
            if org is None:
                raise ValueError(f"Organization {organization_id} not found")

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=-cost,
                currency=org.wallet_currency,
                type=WalletTransactionType.CAMPAIGN_COST.value,
                balance_after=None,
                workflow_run_id=workflow_run_id,
                campaign_id=campaign_id,
                note=note,
                transaction_metadata=_call_charge_metadata(
                    duration_seconds=duration_seconds,
                    billing_mode=billing_mode,
                    price_per_minute=price_per_minute,
                    price_per_call=price_per_call,
                    pulse_seconds=pulse_seconds,
                ),
            )
            session.add(tx)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(tx)
            return tx

    async def wallet_reserve_for_campaign(
        self,
        *,
        organization_id: int,
        campaign_id: int,
        estimated_cost: Decimal,
        created_by_user_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> WalletTransactionModel:
        """Atomic check-and-reserve at campaign launch. Raises
        InsufficientBalanceError if `wallet_balance - credit_limit` can't
        cover the estimate. One reservation per campaign — a retried
        launch call raises ValueError on the second attempt rather than
        silently re-reserving.
        """
        if estimated_cost <= 0:
            raise ValueError("Estimated cost must be positive")

        async with self.async_session() as session:
            org = await self._lock_organization(session, organization_id)

            available = org.wallet_balance - org.credit_limit
            if available < estimated_cost:
                raise InsufficientBalanceError(organization_id, available, estimated_cost)

            org.wallet_balance = org.wallet_balance - estimated_cost

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=-estimated_cost,
                currency=org.wallet_currency,
                type=WalletTransactionType.CAMPAIGN_RESERVE.value,
                balance_after=org.wallet_balance,
                campaign_id=campaign_id,
                created_by_user_id=created_by_user_id,
                note=note,
            )
            session.add(tx)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(
                    f"Campaign {campaign_id} already has a reservation"
                ) from exc
            await session.refresh(tx)
            return tx

    async def wallet_cancel_reservation(
        self,
        *,
        organization_id: int,
        campaign_id: int,
        created_by_user_id: Optional[int] = None,
        note: str = "Reservation cancelled — campaign failed to activate",
    ) -> WalletTransactionModel:
        """Compensating path: refund a campaign's reservation in full when
        the campaign failed to actually start after reserving. Writes to
        the same campaign_reconcile slot the normal end-of-campaign
        reconciliation uses, so at most one of them can ever apply to a
        given campaign.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == campaign_id,
                    WalletTransactionModel.type == WalletTransactionType.CAMPAIGN_RESERVE.value,
                )
            )
            reserve_tx = result.scalar_one_or_none()
            if reserve_tx is None:
                raise ValueError(f"No reservation found for campaign {campaign_id}")

            refund_amount = -reserve_tx.amount  # reserve amount was stored negative

            org = await self._lock_organization(session, organization_id)
            org.wallet_balance = org.wallet_balance + refund_amount

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=refund_amount,
                currency=org.wallet_currency,
                type=WalletTransactionType.CAMPAIGN_RECONCILE.value,
                balance_after=org.wallet_balance,
                campaign_id=campaign_id,
                created_by_user_id=created_by_user_id,
                note=note,
            )
            session.add(tx)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError(
                    f"Campaign {campaign_id} has already been reconciled"
                ) from exc
            await session.refresh(tx)
            return tx

    async def wallet_reconcile_campaign(
        self,
        *,
        organization_id: int,
        campaign_id: int,
        note: Optional[str] = None,
    ) -> Optional[WalletTransactionModel]:
        """Final true-up when a campaign reaches a terminal state
        (completed/failed/cancelled), once every call it dispatched has
        itself reached a terminal state. Sums this campaign's
        CAMPAIGN_COST rows, compares to its CAMPAIGN_RESERVE amount, and
        refunds the unused portion (or debits the rare overage) as one
        signed CAMPAIGN_RECONCILE row. Idempotent — one per campaign.

        Returns None if this campaign was never reserved (billing wasn't
        configured when it launched).
        """
        async with self.async_session() as session:
            reserve_result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == campaign_id,
                    WalletTransactionModel.type == WalletTransactionType.CAMPAIGN_RESERVE.value,
                )
            )
            reserve_tx = reserve_result.scalar_one_or_none()
            if reserve_tx is None:
                return None

            reserved_amount = -reserve_tx.amount  # stored negative

            cost_result = await session.execute(
                select(WalletTransactionModel).where(
                    WalletTransactionModel.campaign_id == campaign_id,
                    WalletTransactionModel.type == WalletTransactionType.CAMPAIGN_COST.value,
                )
            )
            actual_spent = -sum(
                (row.amount for row in cost_result.scalars().all()),
                start=Decimal("0"),
            )

            reconcile_amount = reserved_amount - actual_spent  # + refund, - overage

            org = await self._lock_organization(session, organization_id)
            org.wallet_balance = org.wallet_balance + reconcile_amount

            tx = WalletTransactionModel(
                organization_id=organization_id,
                amount=reconcile_amount,
                currency=org.wallet_currency,
                type=WalletTransactionType.CAMPAIGN_RECONCILE.value,
                balance_after=org.wallet_balance,
                campaign_id=campaign_id,
                note=note,
                transaction_metadata={
                    "reserved_amount": str(reserved_amount),
                    "actual_spent": str(actual_spent),
                },
            )
            session.add(tx)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(tx)
            return tx

    async def update_wallet_settings(
        self,
        *,
        organization_id: int,
        wallet_currency: Optional[str] = None,
        credit_limit: Optional[Decimal] = None,
        wallet_enabled: Optional[bool] = None,
    ) -> Optional[OrganizationModel]:
        """Superadmin-only wallet configuration: currency, credit floor, and
        the master on/off switch. The rate itself lives per-agent (see
        update_workflow_billing_settings), not here. Never touches
        wallet_balance — that only moves through ledgered transactions
        (topup/adjust/debit/reserve/reconcile). A None argument here means
        "leave unchanged", not "clear" — wallet_enabled is a real tri-state
        (None/True/False), so it's only touched when explicitly passed.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationModel).where(OrganizationModel.id == organization_id)
            )
            org = result.scalars().first()
            if org is None:
                return None
            if wallet_currency is not None:
                org.wallet_currency = wallet_currency
            if credit_limit is not None:
                org.credit_limit = credit_limit
            if wallet_enabled is not None:
                org.wallet_enabled = wallet_enabled
            await session.commit()
            await session.refresh(org)
            return org

    async def update_workflow_billing_settings(
        self,
        *,
        workflow_id: int,
        avg_call_duration_minutes: Optional[Decimal] = None,
        price_per_minute: Optional[Decimal] = None,
        billing_mode: Optional[str] = None,
        price_per_call: Optional[Decimal] = None,
        pulse_seconds: Optional[int] = None,
    ) -> Optional[WorkflowModel]:
        """Superadmin-only per-agent wallet inputs: which pricing mode this
        agent uses (billing_mode), the rate for that mode
        (price_per_minute + avg_call_duration_minutes + pulse_seconds for
        PER_MINUTE, or price_per_call for PER_CALL — see campaign_billing.
        reserve_campaign_wallet and workflow_run_billing.py for how each is
        read), and the mode-inactive fields are simply left in place, not
        cleared. A None argument here means "leave unchanged", not "clear".
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowModel).where(WorkflowModel.id == workflow_id)
            )
            workflow = result.scalars().first()
            if workflow is None:
                return None
            if avg_call_duration_minutes is not None:
                workflow.avg_call_duration_minutes = avg_call_duration_minutes
            if price_per_minute is not None:
                workflow.price_per_minute = price_per_minute
            if billing_mode is not None:
                workflow.billing_mode = billing_mode
            if price_per_call is not None:
                workflow.price_per_call = price_per_call
            if pulse_seconds is not None:
                workflow.pulse_seconds = pulse_seconds
            await session.commit()
            await session.refresh(workflow)
            return workflow

    async def list_wallet_transactions(
        self,
        *,
        organization_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WalletTransactionModel], int]:
        """Paginated ledger for an org, newest first."""
        async with self.async_session() as session:
            count_result = await session.execute(
                select(func.count())
                .select_from(WalletTransactionModel)
                .where(WalletTransactionModel.organization_id == organization_id)
            )
            total_count = count_result.scalar_one()

            result = await session.execute(
                select(WalletTransactionModel)
                .where(WalletTransactionModel.organization_id == organization_id)
                .options(selectinload(WalletTransactionModel.workflow_run))
                .order_by(
                    WalletTransactionModel.created_at.desc(),
                    WalletTransactionModel.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), total_count
