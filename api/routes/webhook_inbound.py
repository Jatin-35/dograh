"""Public receiver for Webhook Sync: a client's CRM POSTs new leads here.

No user session: the endpoint UUID identifies the organization and the
endpoint's secret authenticates the request (see services/webhook_sync/auth).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.services.webhook_sync.payload import MAX_BODY_BYTES
from api.services.webhook_sync.receiver import receive_webhook
from api.services.webhook_sync.request_log import install_access_log_redaction

router = APIRouter(prefix="/webhooks/inbound", tags=["webhook-sync"])

# URL-token secrets must not reach the server's access log.
install_access_log_redaction()


def _too_large() -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": "Request body is too large"}, status_code=413
    )


@router.api_route("/{endpoint_uuid}", methods=["GET", "HEAD"])
async def webhook_handshake(endpoint_uuid: str) -> JSONResponse:
    """Answer a CRM's reachability check with 200.

    LeadSquared refuses to save a webhook ("Webhook URL is invalid") unless the
    URL answers HEAD with 200. Nothing is looked up, so this reveals nothing
    about which endpoints exist; leads still arrive only by POST.
    """
    return JSONResponse({"success": True, "message": "Send leads to this URL with POST."})


@router.post("/{endpoint_uuid}")
async def receive_lead_webhook(endpoint_uuid: str, request: Request) -> JSONResponse:
    """Accept one lead (JSON object or form post) or several (JSON array).

    Replies 200 ``{success, received, created, duplicates, invalid, replayed,
    rejected, lead_ids, leads}``; 401 on bad credentials, 404 on an unknown or
    paused endpoint, 413 over 1 MB, 422 when no phone number is present, 429
    over the endpoint's rate limit.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _too_large()
    # Read in chunks and stop at the limit, so a body without (or with a
    # false) Content-Length can't be buffered whole.
    raw_body = bytearray()
    async for chunk in request.stream():
        raw_body.extend(chunk)
        if len(raw_body) > MAX_BODY_BYTES:
            return _too_large()
    result = await receive_webhook(
        endpoint_uuid,
        bytes(raw_body),
        dict(request.headers),
        dict(request.query_params),
        request.client.host if request.client else None,
    )
    return JSONResponse(
        result.body, status_code=result.status_code, headers=result.headers
    )
