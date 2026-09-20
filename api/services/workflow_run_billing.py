"""Workflow-run billing hooks.

Dograh does not rate or deduct credits locally. MPS owns credit accounting.
For hosted deployments, Dograh reports completed platform usage to MPS.
When a server-minted MPS correlation id exists, MPS uses model-service usage
as the canonical duration. Otherwise Dograh reports the completed run duration.
"""

from typing import Any

from loguru import logger

from api.constants import DEPLOYMENT_MODE
from api.db import db_client
from api.enums import WorkflowBillingMode
from api.services.managed_model_services import get_mps_correlation_id
from api.services.mps_service_key_client import mps_service_key_client


def _workflow_run_organization_id(workflow_run) -> int | None:
    workflow = getattr(workflow_run, "workflow", None)
    return getattr(workflow, "organization_id", None)


def _duration_seconds_from_usage_info(workflow_run) -> float | None:
    usage_info: dict[str, Any] = getattr(workflow_run, "usage_info", None) or {}
    duration = usage_info.get("call_duration_seconds")
    try:
        duration_seconds = float(duration)
    except (TypeError, ValueError):
        return None

    return duration_seconds if duration_seconds > 0 else None


def _is_usage_not_ready_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 409:
        return False
    return "usage_not_ready" in (getattr(response, "text", "") or "")


async def report_workflow_run_platform_usage(workflow_run) -> None:
    """Report hosted platform usage for a completed workflow run to MPS."""
    if DEPLOYMENT_MODE == "oss":
        return

    if not getattr(workflow_run, "is_completed", False):
        logger.warning(
            "Workflow run is not completed in report_workflow_run_platform_usage"
        )
        return

    organization_id = _workflow_run_organization_id(workflow_run)
    if organization_id is None:
        logger.warning(
            "Skipping platform usage report for workflow run {}: no organization_id",
            workflow_run.id,
        )
        return

    correlation_id = get_mps_correlation_id(
        getattr(workflow_run, "initial_context", None)
    )
    duration_seconds = (
        None if correlation_id else _duration_seconds_from_usage_info(workflow_run)
    )
    if not correlation_id and duration_seconds is None:
        logger.warning(
            "Skipping platform usage report for workflow run {}: no billable duration",
            workflow_run.id,
        )
        return

    try:
        result = await mps_service_key_client.report_platform_usage(
            organization_id=organization_id,
            correlation_id=correlation_id,
            duration_seconds=duration_seconds,
            workflow_run_id=workflow_run.id,
            metadata={
                "source": "workflow_run_completion",
                "workflow_id": getattr(workflow_run, "workflow_id", None),
                "duration_source": (
                    "mps_correlation" if correlation_id else "dograh_usage_info"
                ),
            },
        )
        logger.info(
            "Reported platform usage for workflow run {} to MPS: {}",
            workflow_run.id,
            result,
        )
    except Exception as e:
        if _is_usage_not_ready_error(e):
            # A run can start and receive an MPS correlation id, then fail or end
            # before billable STT usage is recorded. MPS returns usage_not_ready
            # for that no-platform-fee path, so keep it out of error alerts.
            logger.warning(
                "Failed to report platform usage for workflow run {}: {}",
                workflow_run.id,
                e,
            )
        else:
            logger.error(
                "Failed to report platform usage for workflow run {}: {}",
                workflow_run.id,
                e,
            )


async def report_completed_workflow_run_platform_usage(workflow_run_id: int) -> None:
    """Load a completed workflow run and report platform usage to MPS."""
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(
            "Skipping platform usage report: workflow run {} not found",
            workflow_run_id,
        )
        return

    await report_workflow_run_platform_usage(workflow_run)


async def report_workflow_run_wallet_usage(workflow_run) -> None:
    """Debit the self-hosted wallet for a completed workflow run, at the
    calling agent's own rate (per-minute or flat per-call — whichever
    billing_mode the agent is set to).

    Independent of DEPLOYMENT_MODE/MPS — the wallet is Dograh's own prepaid
    accounting, not MPS's. No-ops if the agent has no rate set for its
    active billing_mode (wallet billing not configured for this agent), the
    org's wallet is off, or there's no billable duration. Campaign calls
    are ledger-only (cost already reserved at launch); normal calls debit
    wallet_balance directly.
    """
    if not getattr(workflow_run, "is_completed", False):
        return

    organization_id = _workflow_run_organization_id(workflow_run)
    if organization_id is None:
        return

    duration_seconds = _duration_seconds_from_usage_info(workflow_run)
    if duration_seconds is None:
        return

    workflow = getattr(workflow_run, "workflow", None)
    billing_mode = getattr(workflow, "billing_mode", WorkflowBillingMode.PER_MINUTE.value)
    price_per_minute = getattr(workflow, "price_per_minute", None)
    price_per_call = getattr(workflow, "price_per_call", None)
    pulse_seconds = getattr(workflow, "pulse_seconds", 0) or 0
    if billing_mode == WorkflowBillingMode.PER_CALL.value:
        if price_per_call is None:
            return
    elif price_per_minute is None:
        return

    campaign_id = getattr(workflow_run, "campaign_id", None)

    try:
        org = await db_client.get_organization_by_id(organization_id)
        if org is None or not org.wallet_enabled:
            return

        if campaign_id is not None:
            await db_client.wallet_record_campaign_call_cost(
                organization_id=organization_id,
                campaign_id=campaign_id,
                workflow_run_id=workflow_run.id,
                duration_seconds=duration_seconds,
                billing_mode=billing_mode,
                price_per_minute=price_per_minute,
                price_per_call=price_per_call,
                pulse_seconds=pulse_seconds,
            )
        else:
            await db_client.wallet_debit_for_call(
                organization_id=organization_id,
                workflow_run_id=workflow_run.id,
                duration_seconds=duration_seconds,
                billing_mode=billing_mode,
                price_per_minute=price_per_minute,
                price_per_call=price_per_call,
                pulse_seconds=pulse_seconds,
            )
    except Exception as e:
        logger.error(
            "Failed to record wallet usage for workflow run {}: {}",
            workflow_run.id,
            e,
        )


async def report_completed_workflow_run_wallet_usage(workflow_run_id: int) -> None:
    """Load a completed workflow run and record wallet usage."""
    workflow_run = await db_client.get_workflow_run_by_id(workflow_run_id)
    if not workflow_run:
        logger.warning(
            "Skipping wallet usage report: workflow run {} not found",
            workflow_run_id,
        )
        return

    await report_workflow_run_wallet_usage(workflow_run)
