"""Think Gas — SAP complaint creation.

SAP Gateway protects writes with a CSRF token that is bound to the session that
issued it, and both the token and the session cookie come back in *response
headers*. A plain Dograh HTTP tool only carries the response body forward, so
the two-step handshake cannot be expressed as two tools — the second call never
receives the values it needs. That is why this lives in code.

The handshake is kept entirely inside this process. The token and cookie are
never returned to Dograh: they would enter the model's context and be written
into the stored call transcript, and a language model would be responsible for
carrying a live session credential accurately, in order, exactly once.
"""

import logging
import os
import re
from typing import Any

import httpx

from handlers.registry import Parameter, Preset, handler

logger = logging.getLogger(__name__)

SAP_HOST = "ucp.think-gas.com"
CREATE_COMPLAINT_URL = (
    f"https://{SAP_HOST}/sap/opu/odata/sap/ZCM_COMPLAINTS_SRV/CreateComplaintSet"
)

# Both SAP calls must finish inside the voice turn. Dograh's tool timeout is
# what the caller actually waits on; this budget sits under it so we fail first
# and can return something speakable.
SAP_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)

REQUIRED_FIELDS = ("ImPartner", "ImQcode", "ImNotes", "ImTicketcreationtag")

# Assumption pending confirmation against the SAP field length. Silently
# truncating a complaint is worse than rejecting it, so keep this honest.
MAX_NOTES_CHARS = 240

# Candidate keys for the complaint id. Narrow this to the real one once a
# successful create response has been captured — trying several hides a
# contract change instead of failing on it.
_TICKET_KEYS = (
    "ExObjectId",
    "ObjectId",
    "LvObjectId",
    "CompNumber",
    "ComplaintNumber",
    "TicketNumber",
)


class SapError(Exception):
    """A caller-safe message plus detail that is logged but never returned."""

    def __init__(self, message: str, *, detail: Any = None, status: int = 0):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


def _auth_header() -> str:
    value = os.environ.get("SAP_BASIC_AUTH_HEADER", "").strip()
    if not value:
        raise SapError("SAP credentials are not configured on this deployment.")
    return value


def _new_client() -> httpx.AsyncClient:
    """A fresh client per request.

    Per-request, never module-level: this serves concurrent phone calls, and a
    shared cookie jar would let one caller's SAP session be used for another
    caller's complaint.

    sap-usercontext goes into the jar rather than a Cookie header. Setting that
    header explicitly replaces the jar wholesale on every request, which would
    discard the SAP_SESSIONID the fetch is about to establish — the exact
    failure this design exists to avoid. Letting the jar own cookies also means
    SAP's multiple Set-Cookie headers are parsed properly; folding them into one
    string and splitting on commas corrupts any cookie carrying an Expires
    attribute, which contains a comma of its own.
    """
    client = httpx.AsyncClient(
        timeout=SAP_TIMEOUT,
        headers={"Accept": "application/json", "Authorization": _auth_header()},
    )
    client.cookies.set("sap-usercontext", "sap-client=120", domain=SAP_HOST, path="/")
    return client


async def _fetch_csrf_token(client: httpx.AsyncClient, trace_id: str) -> str:
    """Fetch a CSRF token. The session cookie lands in the jar automatically."""
    response = await client.get(CREATE_COMPLAINT_URL, headers={"x-csrf-token": "Fetch"})

    token = response.headers.get("x-csrf-token", "").strip()
    has_session = any(
        name.upper().startswith("SAP_SESSIONID") for name in client.cookies.keys()
    )

    # Booleans only. Never the token, the cookie, or the Authorization value.
    logger.info(
        "sap.csrf_fetch trace=%s status=%s token=%s session=%s",
        trace_id, response.status_code, bool(token), has_session,
    )

    if response.status_code in (401, 403):
        raise SapError(
            "SAP rejected our credentials.",
            detail=response.text[:2000],
            status=response.status_code,
        )
    if response.is_error:
        raise SapError(
            f"SAP returned HTTP {response.status_code} during the token fetch.",
            detail=response.text[:2000],
            status=response.status_code,
        )
    if not token:
        raise SapError(
            "SAP returned 200 but no X-CSRF-Token header.",
            detail=dict(response.headers),
            status=response.status_code,
        )
    if not has_session:
        # Do not continue. A write without the session cookie is rejected, and
        # failing explicitly here is far easier to diagnose than a 403 later.
        raise SapError(
            "SAP returned a CSRF token but no SAP_SESSIONID cookie.",
            detail=dict(response.headers),
            status=response.status_code,
        )
    return token


def _extract_ticket_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    sources: list[Any] = [payload, payload.get("d")]
    data = payload.get("d")
    if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
        sources.append(data["results"][0])
    for source in sources:
        if isinstance(source, dict):
            for key in _TICKET_KEYS:
                if source.get(key):
                    return str(source[key]).strip()
    return ""


def _speakable(ticket_id: str) -> str:
    """Space the digits so TTS reads a reference number rather than a quantity.

    "1000012345" is otherwise spoken as "one billion, twelve thousand, three
    hundred forty-five", which is useless to someone writing it down.
    """
    return " ".join(ticket_id) if re.fullmatch(r"\d+", ticket_id) else ticket_id


@handler(
    "create_complaint_with_fresh_csrf",
    description=(
        "Create a Think Gas SAP complaint ticket. Fetches a fresh SAP session "
        "internally, so call this directly — there is no separate token step. "
        "Confirm the partner number with the caller before calling this."
    ),
    parameters=[
        Parameter(
            "ImPartner",
            "string",
            "SAP Business Partner number, verified with the caller. Never guess this.",
        ),
        Parameter(
            "ImQcode",
            "string",
            "Complaint queue code: QPPO for a recharge or balance issue, "
            "QNCP for a new connection request.",
        ),
        Parameter(
            "ImNotes",
            "string",
            "Concise plain-text summary of the complaint, under 240 characters.",
        ),
    ],
    presets=[
        # A constant. Left as a model parameter it is just a chance for the
        # model to send something else.
        Preset("ImTicketcreationtag", "voicebot"),
    ],
    custom_message="Let me register that complaint for you, one moment.",
)
async def create_complaint_with_fresh_csrf(event: dict[str, Any]) -> dict[str, Any]:
    """Fetch a fresh CSRF token and create the complaint in the same session."""
    trace_id = str(event.get("trace_id") or "-")

    missing = [f for f in REQUIRED_FIELDS if not str(event.get(f, "")).strip()]
    if missing:
        return {
            "status": "error",
            "speak": "I'm missing some details needed to raise the complaint.",
            "message": f"Missing required field(s): {', '.join(missing)}",
        }

    notes = " ".join(str(event["ImNotes"]).split())[:MAX_NOTES_CHARS].rstrip()
    payload = {
        "ImPartner": str(event["ImPartner"]).strip(),
        "ImQcode": str(event["ImQcode"]).strip(),
        "ImNotes": notes,
        "ImTicketcreationtag": str(event["ImTicketcreationtag"]).strip(),
    }

    try:
        async with _new_client() as client:
            token = await _fetch_csrf_token(client, trace_id)
            response = await client.post(
                CREATE_COMPLAINT_URL,
                headers={"x-csrf-token": token, "Content-Type": "application/json"},
                json=payload,
            )

            # SAP's standard "your token went stale" reply. Re-fetch on the same
            # client, so the retry keeps the same session, and try once more.
            # Any other 403 is a genuine authorisation failure and must surface.
            if (
                response.status_code == 403
                and response.headers.get("x-csrf-token", "").lower() == "required"
            ):
                logger.info("sap.csrf_stale trace=%s retrying once", trace_id)
                token = await _fetch_csrf_token(client, trace_id)
                response = await client.post(
                    CREATE_COMPLAINT_URL,
                    headers={"x-csrf-token": token, "Content-Type": "application/json"},
                    json=payload,
                )

            logger.info(
                "sap.create trace=%s status=%s", trace_id, response.status_code
            )

            if response.status_code in (401, 403):
                raise SapError(
                    "SAP rejected the complaint request.",
                    detail=response.text[:2000],
                    status=response.status_code,
                )
            if response.is_error:
                raise SapError(
                    f"SAP returned HTTP {response.status_code}.",
                    detail=response.text[:2000],
                    status=response.status_code,
                )

            try:
                body = response.json()
            except ValueError:
                raise SapError(
                    "SAP returned a non-JSON response.",
                    detail=response.text[:2000],
                    status=response.status_code,
                ) from None

            ticket_id = _extract_ticket_id(body)
            if not ticket_id:
                raise SapError(
                    "SAP accepted the complaint but returned no ticket id.",
                    detail=body,
                    status=response.status_code,
                )

    except httpx.TimeoutException:
        logger.warning("sap.timeout trace=%s", trace_id)
        return {
            "status": "error",
            # SAP may have created the ticket regardless. Never claim failure.
            "speak": "I couldn't confirm the complaint in time. Let me take your "
                     "details and our team will confirm shortly.",
            "message": "SAP timed out; creation status unknown.",
        }
    except httpx.RequestError as exc:
        logger.warning("sap.network trace=%s err=%s", trace_id, type(exc).__name__)
        return {
            "status": "error",
            "speak": "I'm having trouble reaching the system right now.",
            "message": "Network error contacting SAP.",
        }
    except SapError as exc:
        logger.error(
            "sap.error trace=%s status=%s msg=%s detail=%r",
            trace_id, exc.status, exc.message, exc.detail,
        )
        return {
            "status": "error",
            "speak": "I wasn't able to register the complaint just now.",
            "message": exc.message,
        }

    logger.info("sap.created trace=%s ticket=%s", trace_id, ticket_id)
    return {
        "status": "success",
        "ticket_id": ticket_id,
        "speak": (
            f"Your complaint has been registered. "
            f"The reference number is {_speakable(ticket_id)}."
        ),
    }
