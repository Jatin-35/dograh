"""Alerts for a Webhook Sync endpoint that needs attention.

Without an alert, a broken integration is only visible on the dashboard: a CRM
whose key was rotated, a wallet that ran dry or a campaign the circuit breaker
paused can lose a day of leads before anyone looks. Each alert is recorded in
the endpoint's History and, when SMTP is configured, emailed to the endpoint's
alert addresses (or, if none are set, to the person who created it).

The same alert for the same endpoint is sent at most once per
``ALERT_COOLDOWN_SECONDS``, so a CRM retrying a bad request every minute does
not flood anyone's inbox.
"""

from typing import Any, Optional

from loguru import logger

from api.db import db_client
from api.schemas.webhook_sync import CallSettings
from api.services.notifications.email import send_email
from api.services.webhook_sync.rate_limit import _get_redis

ALERT_COOLDOWN_SECONDS = 6 * 3600
# Requests in a row that must fail before "requests failing" is raised.
FAILED_REQUESTS_THRESHOLD = 5

REQUESTS_FAILING = "requests_failing"
CALLS_NOT_PLACED = "calls_not_placed"
CALLING_PAUSED = "calling_paused"
CALLBACKS_FAILING = "callbacks_failing"

_SUBJECTS = {
    REQUESTS_FAILING: "Webhook Sync: requests from your CRM are failing",
    CALLS_NOT_PLACED: "Webhook Sync: leads could not be called",
    CALLING_PAUSED: "Webhook Sync: calling was paused automatically",
    CALLBACKS_FAILING: "Webhook Sync: call results could not be sent to your CRM",
}


def _settings(endpoint: Any) -> CallSettings:
    try:
        return CallSettings.model_validate(endpoint.call_settings or {})
    except Exception:
        return CallSettings()


async def _recipients(endpoint: Any) -> list[str]:
    emails = list(_settings(endpoint).alert_emails)
    if emails:
        return emails
    if endpoint.created_by:
        found = await db_client.get_webhook_user_emails([endpoint.created_by])
        if found.get(endpoint.created_by):
            return [found[endpoint.created_by]]
    return []


async def _first_in_cooldown(endpoint_id: int, kind: str) -> bool:
    """True if this is the first such alert in the cooldown window. A Redis
    error lets the alert through: a duplicate beats a missed alert."""
    try:
        client = await _get_redis()
        return bool(
            await client.set(
                f"webhook_sync_alert:{endpoint_id}:{kind}",
                "1",
                nx=True,
                ex=ALERT_COOLDOWN_SECONDS,
            )
        )
    except Exception as e:
        logger.warning(f"Webhook Sync alert cooldown check failed: {e}")
        return True


async def raise_alert(endpoint: Any, kind: str, message: str) -> bool:
    """Record and email one alert, unless the same one went out recently.
    Returns True when the alert was raised. Never raises."""
    try:
        if not await _first_in_cooldown(endpoint.id, kind):
            return False
        recipients = await _recipients(endpoint)
        url = await _endpoint_page(endpoint)
        body = (
            f"{message}\n\n"
            f"Endpoint: {endpoint.name}\n"
            + (f"Open it: {url}\n" if url else "")
            + "\nYou get this at most once every 6 hours per problem."
        )
        emailed = await send_email(
            recipients, _SUBJECTS.get(kind, "Webhook Sync"), body
        )
        await db_client.create_webhook_audit_log(
            organization_id=endpoint.organization_id,
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.name,
            user_id=None,
            action="alert",
            changes={
                "kind": kind,
                "message": message,
                "emailed_to": recipients if emailed else [],
            },
        )
        logger.warning(
            f"Webhook Sync alert on endpoint {endpoint.id} ({kind}): {message}"
            f"{' (emailed)' if emailed else ''}"
        )
        return True
    except Exception as e:
        logger.error(f"Webhook Sync: could not raise alert {kind}: {e}")
        return False


async def _endpoint_page(endpoint: Any) -> Optional[str]:
    from api.constants import PUBLIC_BASE_URL, UI_APP_URL

    base = PUBLIC_BASE_URL or UI_APP_URL
    return f"{base.rstrip('/')}/webhook-sync/{endpoint.id}" if base else None


# ======== Triggers ========


async def record_request_outcome(
    endpoint: Any, succeeded: bool, error: Optional[str] = None
) -> None:
    """Count requests that failed in a row (bad key, bad data, our errors);
    a success resets the count. At the threshold, raise an alert."""
    key = f"webhook_sync_failed_requests:{endpoint.id}"
    try:
        client = await _get_redis()
        if succeeded:
            await client.delete(key)
            return
        count = await client.incr(key)
        await client.expire(key, 24 * 3600)
    except Exception as e:
        logger.warning(f"Webhook Sync: could not count failed requests: {e}")
        return
    if count >= FAILED_REQUESTS_THRESHOLD:
        await raise_alert(
            endpoint,
            REQUESTS_FAILING,
            f"The last {count} requests from your CRM were rejected. "
            f"Latest reason: {error or 'unknown'}. Leads sent in these "
            "requests were not stored. Check the endpoint's Logs tab.",
        )


async def calls_not_placed(endpoint: Any, reason: str) -> None:
    await raise_alert(
        endpoint,
        CALLS_NOT_PLACED,
        f"A lead could not be called: {reason}. New leads will keep failing "
        "the same way until this is fixed; once it is, use Call again on them.",
    )


async def calling_paused(endpoint: Any) -> None:
    await raise_alert(
        endpoint,
        CALLING_PAUSED,
        "Calling was paused because too many calls failed in a row (for "
        "example a telephony or number problem). Leads are still being "
        "stored. Fix the cause, then press Resume calling on the endpoint.",
    )


async def callbacks_failing(endpoint: Any, failed: int) -> None:
    await raise_alert(
        endpoint,
        CALLBACKS_FAILING,
        f"{failed} call result(s) could not be delivered to your CRM's callback "
        "URL after every retry. Check the URL, then use Resend to CRM on the "
        "affected leads.",
    )
