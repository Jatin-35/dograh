"""Mirror a Conditional Webhook's delivery result onto its run annotation.

The node writes ``status: queued`` when it queues a send; the delivery task
calls ``record_delivery_outcome`` once the receiver answers, so the run report
shows whether the message was actually delivered, not just queued.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from api.db import db_client

_PREFIX = "conditional_webhook_"
_MAX_ERROR_CHARS = 300
_MAX_RESPONSE_CHARS = 1000

# Path segments this long and token-shaped are treated as credentials (an API
# key embedded in the URL, as WhatsApp panels do); query values whose name
# looks like a credential are masked too. The run report is shown to people who
# must not be able to copy a key out of it.
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9+/=_\-.]{20,}$")
_SECRET_PARAM = re.compile(r"key|token|secret|auth|password|signature", re.I)


def _mask(value: str) -> str:
    return f"{value[:4]}••••" if len(value) > 4 else "••••"


def mask_url(url: Optional[str]) -> Optional[str]:
    """The URL with anything credential-shaped hidden."""
    if not url:
        return url
    parts = urlsplit(url)
    path = "/".join(
        _mask(segment) if _TOKEN_SEGMENT.match(segment) else segment
        for segment in parts.path.split("/")
    )
    # Display only, so the query is rebuilt unencoded (keeps "••••" readable).
    query = "&".join(
        f"{name}={_mask(value) if _SECRET_PARAM.search(name) else value}"
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
    )
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def annotation_key(node_id: str) -> str:
    return f"{_PREFIX}{node_id}"


async def record_delivery_outcome(
    *,
    workflow_run_id: int,
    webhook_node_id: str,
    status: str,
    attempt: int,
    status_code: Optional[int] = None,
    error: Optional[str] = None,
    response_body: Optional[str] = None,
) -> bool:
    """``status`` is delivered, retrying or failed.

    A no-op (returns False) for any delivery that is not a Conditional
    Webhook's: only those have a ``conditional_webhook_<node id>`` entry.
    """
    patch: dict = {
        "status": status,
        "sent": status == "delivered",
        "attempts": attempt,
        "http_status": status_code,
    }
    if status == "delivered":
        patch["error"] = None  # clear an earlier attempt's error
    elif error:
        patch["error"] = error[:_MAX_ERROR_CHARS]
    if response_body is not None:
        patch["response"] = response_body[:_MAX_RESPONSE_CHARS]
    return await db_client.patch_workflow_run_annotation(
        workflow_run_id, annotation_key(webhook_node_id), patch, create=False
    )
