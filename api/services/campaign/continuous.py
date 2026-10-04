"""Continuous (always-on) campaigns.

A normal campaign dials a fixed list: it reserves the wallet for the whole
list up front, and completes once the list is done and it has been idle for an
hour. A continuous campaign is fed one call at a time for as long as its
source exists (Webhook Sync leads), so it never completes on its own, a
failed batch doesn't end it, and each call is authorized and billed against
the wallet like a direct call instead of against a campaign reservation.

The flag lives in ``orchestrator_metadata`` (no schema change).
"""

from typing import Any, Optional

from api.db import db_client

CONTINUOUS_KEY = "continuous"


def is_continuous_campaign(campaign: Any) -> bool:
    metadata = getattr(campaign, "orchestrator_metadata", None) or {}
    return bool(metadata.get(CONTINUOUS_KEY))


def is_managed_elsewhere(campaign: Any) -> bool:
    """A Webhook Sync endpoint owns this campaign: its agent, calling rules
    and paused state follow the endpoint, so it is managed only from there."""
    return is_continuous_campaign(campaign)


def reject_if_managed_elsewhere(campaign: Any) -> None:
    """Refuse a Campaigns-page action (start, pause, stop, edit, resume) on a
    campaign an endpoint owns. Stopping one there used to strand its queued
    leads: the endpoint started a new campaign and nothing called them."""
    if is_managed_elsewhere(campaign):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail=(
                "This campaign belongs to a Webhook Sync endpoint. Pause, resume "
                "or change it from the endpoint's page under Webhook Sync."
            ),
        )


async def campaign_bills_per_call(campaign_id: Optional[int]) -> bool:
    """True when this campaign's calls are checked and debited one by one."""
    if campaign_id is None:
        return False
    campaign = await db_client.get_campaign_by_id(campaign_id)
    return campaign is not None and is_continuous_campaign(campaign)
