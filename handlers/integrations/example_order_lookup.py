"""Worked example — copy this when adding an integration.

Nothing here is specific to any client or protocol. It shows the shape every
handler follows: validate, call something, return a small speakable result.

Delete it once you have real handlers, or keep it as a smoke test — it is the
quickest way to prove the whole path (Dograh tool → this service → response →
agent speech) works without depending on a third-party system being up.
"""

import logging
import os
from typing import Any

import httpx

from handlers.registry import Parameter, Preset, handler

logger = logging.getLogger(__name__)

# Upstream must finish inside the voice turn. See handlers/README.md for how
# this sits under HANDLER_TIMEOUT_SECONDS and the tool's timeout_ms.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=3.0)


@handler(
    "check_order_status",
    description=(
        "Look up the current delivery status of a customer order. Ask the "
        "caller for their order number first if they haven't given it."
    ),
    parameters=[
        Parameter(
            "order_id",
            "string",
            "The customer's order number, for example ORD-12345.",
        ),
    ],
    presets=[
        # Call context the model neither needs nor should spend tokens on.
        Preset("caller_number", "{{initial_context.phone_number}}", required=False),
    ],
    custom_message="Let me look that order up for you.",
)
async def check_order_status(event: dict[str, Any]) -> dict[str, Any]:
    order_id = str(event.get("order_id", "")).strip()
    if not order_id:
        return {
            "status": "error",
            "speak": "I didn't catch the order number — could you say it again?",
            "message": "Missing required field: order_id",
        }

    base_url = os.environ.get("ORDERS_API_URL", "").strip()
    if not base_url:
        return {
            "status": "error",
            "speak": "I can't look up orders right now.",
            "message": "ORDERS_API_URL is not configured.",
        }

    try:
        # Built per request, never at module level: this serves concurrent
        # calls, and a shared client leaks one caller's state into another's.
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/orders/{order_id}",
                headers={"Authorization": os.environ.get("ORDERS_API_KEY", "")},
            )
    except httpx.TimeoutException:
        return {
            "status": "error",
            "speak": "That's taking longer than expected. Let me take your "
                     "number and we'll call you back.",
            "message": "Upstream timed out.",
        }
    except httpx.RequestError as exc:
        logger.warning("orders.network err=%s", type(exc).__name__)
        return {
            "status": "error",
            "speak": "I'm having trouble reaching our order system.",
            "message": "Network error contacting the orders API.",
        }

    if response.status_code == 404:
        return {
            "status": "not_found",
            "speak": "I couldn't find an order with that number. "
                     "Could you check it and read it to me again?",
            "message": f"Order {order_id} not found.",
        }
    if response.is_error:
        # Detail to the log, never to the response — it is injected into the
        # model's context and persisted in the transcript.
        logger.error(
            "orders.error status=%s body=%s", response.status_code, response.text[:2000]
        )
        return {
            "status": "error",
            "speak": "I couldn't get the status of that order just now.",
            "message": f"Orders API returned HTTP {response.status_code}.",
        }

    order = response.json()

    # Return only what the agent needs to say, plus a field or two it may need
    # to reason with. Never the whole upstream payload.
    return {
        "status": "success",
        "order_status": order.get("status"),
        "speak": (
            f"Your order is {order.get('status', 'being processed')}"
            + (f", expected {order['eta']}." if order.get("eta") else ".")
        ),
    }
