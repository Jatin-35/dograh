"""Background jobs for Webhook Sync."""

from datetime import UTC, datetime, timedelta

from loguru import logger

from api.db import db_client
from api.services.webhook_sync.request_log import (
    PAYLOAD_RETENTION_DAYS,
    RETENTION_DAYS,
)


async def cleanup_webhook_sync_data(_ctx) -> dict[str, int]:
    """Apply Webhook Sync's retention: delete request logs older than
    RETENTION_DAYS, and clear the full CRM payload from leads older than
    PAYLOAD_RETENTION_DAYS (the lead's own fields are kept). Both hold
    personal data from the CRM, so neither is kept longer than needed."""
    now = datetime.now(UTC)
    logs = await db_client.delete_webhook_request_logs_before(
        now - timedelta(days=RETENTION_DAYS)
    )
    payloads = await db_client.clear_webhook_lead_payloads_before(
        now - timedelta(days=PAYLOAD_RETENTION_DAYS)
    )
    if logs or payloads:
        logger.info(
            f"Webhook Sync retention: deleted {logs} request logs, "
            f"cleared {payloads} lead payloads"
        )
    return {"request_logs_deleted": logs, "payloads_cleared": payloads}
