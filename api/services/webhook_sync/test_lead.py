"""Send a test lead to an endpoint from the dashboard.

It goes through the real receiver with the endpoint's own credentials, in
whichever way the endpoint authenticates, so it tests exactly what the CRM's
requests will: authentication, mapping, duplicate check, storing, and (with
auto-call on) a real call to the number entered.
"""

import json
import time
from typing import Any, Optional

from api.services.webhook_sync.auth import TOKEN_PARAM, sign
from api.services.webhook_sync.receiver import ReceiveResult, receive_webhook

TEST_SOURCE = "Dashboard test"


def _request(endpoint: Any, body: bytes) -> tuple[dict[str, str], dict[str, str]]:
    """Headers and query that authenticate as the endpoint's CRM would."""
    headers = {"content-type": "application/json"}
    query: dict[str, str] = {}
    if endpoint.auth_type == "api_key":
        headers["x-api-key"] = endpoint.secret
    elif endpoint.auth_type == "hmac":
        timestamp = str(int(time.time()))
        headers["x-botrix-timestamp"] = timestamp
        headers["x-botrix-signature"] = sign(endpoint.secret, body, timestamp)
    elif endpoint.auth_type == "url_token":
        query[TOKEN_PARAM] = endpoint.secret
    return headers, query


async def send_test_lead(
    endpoint: Any, *, phone: str, name: Optional[str] = None
) -> ReceiveResult:
    payload = {"mobile": phone, "name": name or "Test Lead", "source": TEST_SOURCE}
    body = json.dumps(payload).encode()
    headers, query = _request(endpoint, body)
    return await receive_webhook(
        endpoint.endpoint_uuid, body, headers, query, peer="dashboard"
    )
