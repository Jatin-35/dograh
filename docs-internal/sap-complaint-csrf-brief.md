# Implementation Brief — SAP Complaint Creation with CSRF/session handling

**For:** the engineer or coding agent implementing this.
**Deliverable:** one backend HTTP endpoint that Dograh's voice agent calls to
create a Think Gas SAP complaint, handling the CSRF handshake internally.

Read section 4 before writing code. It contains five failure modes that the
obvious implementation hits, three of which only show up under production load
and are therefore expensive to find later.

---

## 1. Context

Think Gas complaints are created through an SAP OData V2 service. SAP Gateway
protects modifying requests with a CSRF token that is **bound to the session
that issued it**. Creating a complaint is therefore a two-step handshake:

1. `GET` the entity set with `x-csrf-token: Fetch` — SAP replies with the token
   in a **response header** and a session cookie in `Set-Cookie`.
2. `POST` the complaint using **that exact token together with that exact
   session cookie**.

Use either half from a different session and SAP rejects the write.

## 2. The problem we are actually solving

Dograh's HTTP tool can call the SAP `GET` directly, and it returns HTTP 200
with this body:

```json
{ "d": { "results": [] } }
```

That body is useless — the values we need are in the **response headers**, and
Dograh's HTTP tool surfaces the parsed body to the next tool call, not the
headers. So the second tool (the `POST`) never reliably receives the token or
the cookie, and complaint creation fails or silently uses stale values.

This is not a Dograh bug to work around in the workflow. It is a case where the
integration genuinely needs response-header access and same-session cookie
continuity, which belongs in backend code.

**There is also a security argument.** If the token and cookie pass through the
voice agent, they enter the LLM's context and are persisted in the call
transcript. Session credentials should never go there.

## 3. Decision

Build **one** backend endpoint that does the whole handshake internally:

```
Dograh voice agent
  └─> POST <backend>/tools/create_complaint_with_fresh_csrf
        ├─ GET  SAP  (x-csrf-token: Fetch)   → token + session cookie
        ├─ POST SAP  (same token, same session) → complaint
        └─ return { status, ticket_id, speak }
```

Dograh sends only business fields. It never sees a token or cookie.

**Do not build a separate `get_complaint_csrf_token` tool for the agent to
call.** A two-step flow exposed to the LLM re-creates the exact bug we are
fixing: the model can call step 2 with a token from a previous call, out of
order, or twice. Keep the handshake atomic and server-side.

If the existing Dograh tools `get_complaint_csrf_token` and `create_complaint2`
are wired into a workflow, remove both from the node and the prompt once this
endpoint is live. Leaving them available means the model may still choose them.

---

## 4. Five things that will break — read before coding

### 4.1 `response.headers.get("Set-Cookie")` is wrong, and it will corrupt the cookie

SAP returns **multiple** `Set-Cookie` headers. `requests` folds duplicate
headers into one comma-joined string in `response.headers`. Cookie values
legitimately contain commas — every `Expires=Wed, 21 Oct 2025 ...` attribute
has one — so any code that splits the joined value on `,` will cut a cookie in
half. It will appear to work in testing against one response shape and fail
against another.

**Do not parse `Set-Cookie` by hand at all.** Use a `requests.Session()` and
let the cookie jar do it. The jar handles multiple headers, attributes,
domains and paths correctly, and it automatically replays the cookies on the
subsequent `POST` — which is precisely the same-session guarantee we need.

The CSRF token itself is fine to read from `response.headers` — it is
single-valued, and `requests` already uses a case-insensitive dict, so a manual
case-insensitive helper is redundant.

### 4.2 The session must be per-request, not module-level

This backend serves a voice platform, so concurrent calls are normal. A
module-level `requests.Session()` shared across requests means **caller A's SAP
session cookie can be used for caller B's complaint**. Create the session
inside the request handler and let it be garbage collected.

### 4.3 SAP expires tokens; handle `403 + x-csrf-token: Required`

Standard SAP Gateway behaviour: when a token is expired or rejected, SAP
answers the `POST` with **HTTP 403** and the response header
`x-csrf-token: Required`. This is the single most common cause of intermittent
production failures in SAP integrations.

Handle it explicitly: on that exact condition, re-fetch the token **on the same
session** once, and retry the `POST` once. Do not retry more than once, and do
not retry on any other 403 — a genuine authorisation failure must surface.

### 4.4 The timeout budget must fit inside a voice turn

Your draft used `timeout=20`. Dograh's HTTP tool defaults to **5000 ms**
(`timeout_ms`, `api/services/workflow/tools/custom_tool.py:303`). With a 20 s
backend timeout, Dograh gives up at 5 s while SAP is still working — and the
complaint may still get created. The caller hears silence, the agent reports
failure, and a ticket exists anyway. If the model then retries, you have two
tickets for one complaint.

Budget the whole handshake to fit: **connect 3 s, and a total wall-clock budget
of ~8 s across both SAP calls**, and raise Dograh's `timeout_ms` on this tool to
**10000** so the backend always fails first and can return a structured error.

Also set `customMessage` on the Dograh tool (e.g. *"Let me register that
complaint for you, one moment."*). It is queued **before** the HTTP call
(`api/services/workflow/pipecat_engine_custom_tools.py:396`), so it fills the
silence while SAP works. The field's own description says "after tool
execution" — that description is wrong; the code plays it first.

### 4.5 Keep the response small — it goes into the LLM's context and is persisted

Every field you return is injected into the model's context and written into
the stored call transcript. Returning `api_response` with the full SAP payload
on every call is how transcripts bloat, and on this platform an oversized tool
result has previously killed a conversation outright.

Return only what the agent needs to speak, plus a correlation id. Log the full
SAP payload server-side against that id.

**Good:**
```json
{ "status": "success", "ticket_id": "1000012345", "speak": "Your complaint is registered. The reference number is 1 0 0 0 0 1 2 3 4 5.", "trace_id": "c7f1a2" }
```

**Bad:** the whole SAP OData envelope, `debug`, request echo, and a stack trace.

Note the spaced digits in `speak` — TTS reads `1000012345` as "one billion,
twelve thousand, three hundred forty-five", which is useless to a caller
writing down a reference number.

---

## 5. Reference implementation

Python 3.11+, `requests`. If the host service is async FastAPI, port this to
`httpx.AsyncClient` — `requests` is blocking and will stall an event loop.
`httpx.AsyncClient` has the same cookie-jar behaviour, so the design is
unchanged.

```python
import logging
import os
import re
import uuid

import requests

logger = logging.getLogger(__name__)

SAP_HOST = "ucp.think-gas.com"
CREATE_COMPLAINT_URL = (
    f"https://{SAP_HOST}/sap/opu/odata/sap/ZCM_COMPLAINTS_SRV/CreateComplaintSet"
)

# Must fit inside the voice turn — see 4.4. connect, read.
SAP_TIMEOUT = (3.0, 5.0)

REQUIRED_FIELDS = ("ImPartner", "ImQcode", "ImNotes", "ImTicketcreationtag")
MAX_NOTES_CHARS = 240  # confirm against the SAP field length — see section 9


def _sap_auth_header() -> str:
    """SAP Basic Auth, from the environment. Never inline, never logged."""
    value = os.environ.get("SAP_BASIC_AUTH_HEADER", "").strip()
    if not value:
        raise RuntimeError("SAP_BASIC_AUTH_HEADER is not configured")
    return value


def _new_sap_session() -> requests.Session:
    """A fresh session per request.

    Per-request, never module-level: this serves concurrent phone calls, and a
    shared session would let one caller's SAP session cookie be used for
    another caller's complaint (4.2).

    sap-usercontext goes in the cookie *jar*, not a Cookie header. Setting a
    Cookie header manually overrides the jar wholesale, which would discard the
    SAP_SESSIONID the GET is about to establish — the exact bug this design
    exists to prevent (4.1).
    """
    session = requests.Session()
    session.headers.update(
        {"Accept": "application/json", "Authorization": _sap_auth_header()}
    )
    session.cookies.set(
        "sap-usercontext", "sap-client=120", domain=SAP_HOST, path="/"
    )
    return session


def _clean_notes(value: str) -> str:
    text = " ".join(str(value or "").split())
    return text[:MAX_NOTES_CHARS].rstrip()


class SapError(Exception):
    """Carries a caller-safe message plus the detail we log but never return."""

    def __init__(self, message: str, *, detail: object = None, status: int = 0):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


def _fetch_csrf_token(session: requests.Session, trace_id: str) -> str:
    """Fetch a CSRF token. The session cookie lands in the jar automatically."""
    response = session.get(
        CREATE_COMPLAINT_URL,
        headers={"x-csrf-token": "Fetch"},
        timeout=SAP_TIMEOUT,
    )

    token = response.headers.get("x-csrf-token", "").strip()
    has_session = any(
        c.name.upper().startswith("SAP_SESSIONID") for c in session.cookies
    )

    # Booleans only. Never the token, the cookie, or the Authorization header.
    logger.info(
        "sap.csrf_fetch trace=%s status=%s token=%s session_cookie=%s",
        trace_id, response.status_code, bool(token), has_session,
    )

    if response.status_code in (401, 403):
        raise SapError(
            "SAP rejected our credentials.",
            detail=response.text[:2000],
            status=response.status_code,
        )
    if not response.ok:
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
        # Do not proceed. A POST without the session cookie will be rejected,
        # and a silent failure here is worse than an explicit one.
        raise SapError(
            "SAP returned a CSRF token but no SAP_SESSIONID cookie.",
            detail=dict(response.headers),
            status=response.status_code,
        )
    return token


_TICKET_KEYS = (
    "ExObjectId", "ObjectId", "LvObjectId",
    "CompNumber", "ComplaintNumber", "TicketNumber",
)


def _extract_ticket_id(payload: object) -> str:
    """Pull the complaint id out of the SAP OData envelope.

    The candidate key list is a guess until the real create response is
    captured — see section 9. Narrow it to the actual key once known; trying
    six keys hides a contract change instead of failing on it.
    """
    if not isinstance(payload, dict):
        return ""
    candidates = [payload, payload.get("d")]
    d = payload.get("d")
    if isinstance(d, dict) and isinstance(d.get("results"), list) and d["results"]:
        candidates.append(d["results"][0])
    for source in candidates:
        if isinstance(source, dict):
            for key in _TICKET_KEYS:
                if source.get(key):
                    return str(source[key]).strip()
    return ""


def _speakable(ticket_id: str) -> str:
    """Digits spaced out so TTS reads them as a reference number (4.5)."""
    return " ".join(ticket_id) if re.fullmatch(r"\d+", ticket_id) else ticket_id


def create_complaint_with_fresh_csrf(event: dict) -> dict:
    """Fetch a fresh CSRF token and create the complaint in the same session."""
    trace_id = uuid.uuid4().hex[:8]

    missing = [f for f in REQUIRED_FIELDS if not str(event.get(f, "")).strip()]
    if missing:
        return {
            "status": "error",
            "speak": "I'm missing some details needed to raise the complaint.",
            "message": f"Missing required field(s): {', '.join(missing)}",
            "trace_id": trace_id,
        }

    payload = {
        "ImPartner": str(event["ImPartner"]).strip(),
        "ImQcode": str(event["ImQcode"]).strip(),
        "ImNotes": _clean_notes(event["ImNotes"]),
        "ImTicketcreationtag": str(event["ImTicketcreationtag"]).strip(),
    }

    try:
        with _new_sap_session() as session:
            token = _fetch_csrf_token(session, trace_id)

            response = session.post(
                CREATE_COMPLAINT_URL,
                headers={"x-csrf-token": token, "Content-Type": "application/json"},
                json=payload,
                timeout=SAP_TIMEOUT,
            )

            # SAP's standard "your token went stale" reply. Re-fetch on the SAME
            # session and retry exactly once (4.3). Any other 403 is a real
            # authorisation failure and must surface.
            if (
                response.status_code == 403
                and response.headers.get("x-csrf-token", "").lower() == "required"
            ):
                logger.info("sap.csrf_stale trace=%s retrying once", trace_id)
                token = _fetch_csrf_token(session, trace_id)
                response = session.post(
                    CREATE_COMPLAINT_URL,
                    headers={"x-csrf-token": token, "Content-Type": "application/json"},
                    json=payload,
                    timeout=SAP_TIMEOUT,
                )

            logger.info(
                "sap.create_complaint trace=%s status=%s", trace_id, response.status_code
            )

            if response.status_code in (401, 403):
                raise SapError(
                    "SAP rejected the complaint request.",
                    detail=response.text[:2000],
                    status=response.status_code,
                )
            if not response.ok:
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
                )

            ticket_id = _extract_ticket_id(body)
            if not ticket_id:
                raise SapError(
                    "SAP accepted the complaint but returned no ticket id.",
                    detail=body,
                    status=response.status_code,
                )

    except requests.exceptions.Timeout:
        logger.warning("sap.timeout trace=%s", trace_id)
        return {
            "status": "error",
            # Deliberate wording: SAP may have created the ticket anyway (4.4).
            # Never tell the caller it failed outright.
            "speak": "I couldn't confirm the complaint in time. Let me take your "
                     "details and our team will confirm shortly.",
            "message": "SAP timed out; creation status unknown.",
            "trace_id": trace_id,
        }
    except requests.exceptions.RequestException as exc:
        logger.warning("sap.network trace=%s err=%s", trace_id, exc.__class__.__name__)
        return {
            "status": "error",
            "speak": "I'm having trouble reaching the system right now.",
            "message": "Network error contacting SAP.",
            "trace_id": trace_id,
        }
    except SapError as exc:
        # Full detail to the log, never to the response (4.5).
        logger.error(
            "sap.error trace=%s status=%s msg=%s detail=%r",
            trace_id, exc.status, exc.message, exc.detail,
        )
        return {
            "status": "error",
            "speak": "I wasn't able to register the complaint just now.",
            "message": exc.message,
            "trace_id": trace_id,
        }

    logger.info("sap.created trace=%s ticket=%s", trace_id, ticket_id)
    return {
        "status": "success",
        "ticket_id": ticket_id,
        "speak": f"Your complaint has been registered. The reference number is "
                 f"{_speakable(ticket_id)}.",
        "trace_id": trace_id,
    }
```

---

## 6. Dograh-side configuration

Register one HTTP tool. Verified shapes from the Dograh codebase:

```json
{
  "name": "create_complaint_with_fresh_csrf",
  "description": "Create a Think Gas SAP complaint ticket. Fetches a fresh SAP CSRF token and session internally, then creates the complaint in the same session. Use QPPO for recharge/balance complaints and QNCP for new connection requests. Confirm the partner number with the caller before calling this.",
  "category": "http_api",
  "config": {
    "method": "POST",
    "url": "https://<backend-host>/tools/create_complaint_with_fresh_csrf",
    "credential_uuid": "<credential for backend auth — see below>",
    "timeout_ms": 10000,
    "customMessage": "Let me register that complaint for you, one moment.",
    "customMessageType": "text",
    "parameters": [
      {"name": "ImPartner", "type": "string", "required": true,
       "description": "SAP Business Partner number, verified with the caller. Never invent or guess this."},
      {"name": "ImQcode", "type": "string", "required": true,
       "description": "Complaint queue code: QPPO for recharge or balance issues, QNCP for a new connection request."},
      {"name": "ImNotes", "type": "string", "required": true,
       "description": "Concise plain-text summary of the caller's complaint, under 240 characters."},
      {"name": "ImTicketcreationtag", "type": "string", "required": true,
       "description": "Always the literal value: voicebot"}
    ],
    "preset_parameters": [
      {"name": "caller_number", "type": "string",
       "value_template": "{{initial_context.phone_number}}", "required": false}
    ]
  }
}
```

Notes on this config:

- **`timeout_ms: 10000`**, above the backend's ~8 s budget, so the backend
  always fails first and returns a structured, speakable error rather than
  Dograh reporting a bare timeout (4.4).
- **`ImTicketcreationtag` is better as a preset than a model parameter.** It is
  always `voicebot`; asking the model to supply a constant is a chance for it
  to supply something else. Move it to `preset_parameters` with
  `value_template: "voicebot"` and drop it from `parameters`. Literal values in
  `value_template` pass through unchanged — verified.
- **Auth between Dograh and your backend** goes in `credential_uuid`, created
  in the Dograh UI credential manager. **Never put a token in `headers`** — the
  tool config is visible to anyone who can edit the agent. This is separate
  from `SAP_BASIC_AUTH_HEADER`, which lives only in the backend environment and
  is never sent to or seen by Dograh.
- **Preset parameters are merged after the model decides** and are invisible to
  it, so `caller_number` reaches your backend without entering the prompt.

If you route several functions through one catch-all endpoint, add
`{"name": "function_name", "type": "string", "value_template": "create_complaint_with_fresh_csrf"}`
as a preset and dispatch on it server-side.

---

## 7. Error handling matrix

Every row returns HTTP 200 to Dograh with a `status` field — never a 5xx. A
non-2xx from your backend gives the agent nothing speakable.

| Condition | `status` | Caller hears |
|---|---|---|
| Missing required field | `error` | "I'm missing some details…" |
| SAP 401/403 on fetch or create | `error` | "I wasn't able to register the complaint just now." |
| SAP 403 + `x-csrf-token: Required` | retried once, then as above | — |
| SAP 200 but no `X-CSRF-Token` | `error` | as above |
| SAP 200 but no `SAP_SESSIONID` cookie | `error` | as above |
| SAP 5xx | `error` | as above |
| Timeout | `error` | **"I couldn't confirm in time…"** — never "it failed" |
| Non-JSON response | `error` | as above |
| Created but no ticket id | `error` | as above |
| Success | `success` | "Your complaint has been registered. The reference number is…" |

The timeout wording is deliberate and matters: on a timeout the complaint **may
have been created**. Telling a caller it failed invites a duplicate.

---

## 8. Test plan

1. **Happy path.** Valid partner, `QPPO`. Assert a ticket id comes back and
   that the log line contains no token, cookie or `Authorization` value.
2. **Multiple `Set-Cookie` headers.** Mock SAP returning three `Set-Cookie`
   headers, one with an `Expires=Wed, 21 Oct 2025 07:28:00 GMT` attribute.
   Assert the correct `SAP_SESSIONID` is sent on the `POST`. *This is the test
   that catches 4.1; a hand-rolled comma split fails it.*
3. **Stale token.** Mock the first `POST` returning 403 with
   `x-csrf-token: Required`, the second succeeding. Assert exactly two `POST`s
   and one extra fetch, and a successful result.
4. **Genuine 403.** 403 *without* that header. Assert no retry.
5. **Missing token / missing cookie.** 200 with each absent in turn. Assert a
   specific error and that **no `POST` is attempted**.
6. **Concurrency.** Fire 20 concurrent requests against a mock that issues a
   distinct session id per fetch. Assert each `POST` carries the session id from
   its own fetch. *This is the test that catches 4.2.*
7. **Timeout.** Mock SAP sleeping past the budget. Assert the "couldn't
   confirm" wording, not a failure claim.
8. **Response size.** Assert the returned payload is under 2 KB even when SAP
   returns a large error body (4.5).
9. **Secret hygiene.** Capture all log output across the suite and assert it
   contains neither the string `Basic ` nor any token or cookie value.

---

## 9. Resolve before shipping

1. **The real create-response shape.** `_extract_ticket_id` currently guesses
   across six keys. Capture one successful `POST` in Postman and narrow it to
   the actual field. Guessing hides a contract change instead of failing on it.
2. **`ImQcode` values.** The brief says `QPPO` / `QNCP`, but the test payloads
   use `DUBI`. Confirm which are real and which is a test queue, and make sure
   the tool description lists only production codes — the model will use
   whatever it is shown.
3. **`ImNotes` length.** `MAX_NOTES_CHARS = 240` is an assumption. Confirm the
   SAP field length; silently truncating a complaint is worse than rejecting it.
4. **Does SAP return 201 or 200 on create?** `response.ok` covers both, but
   confirm so the tests assert the real thing.
5. **Duplicate protection.** SAP offers no idempotency key. If duplicates
   matter, pass a per-call identifier from Dograh via `preset_parameters` and
   keep a short-TTL cache keyed on it, returning the first ticket id on a
   repeat. Confirm the available context variable name in the Dograh UI.
6. **Is `sap-usercontext=sap-client=120` actually required**, or does SAP set it
   itself on the fetch? If SAP sets it, drop the manual jar entry and let the
   session carry it.

---

## 10. Security — non-negotiable

- `SAP_BASIC_AUTH_HEADER` comes from the environment or a secret manager.
  Never in code, never in the repo, never in a Dograh tool config.
- **Never log**: the `Authorization` header, the Basic Auth value, the full
  CSRF token, or the full session cookie. Log booleans and a `trace_id`.
  Masking is second best; prefer booleans.
- **Never return** the token or cookie to Dograh. They would enter the LLM
  context and be written into the stored call transcript.
- Never hardcode a token or session id, including values copied from Postman.
  Every value is read fresh from the response that issued it.
- The backend endpoint must require its own auth (bearer token via Dograh's
  `credential_uuid`). It creates real tickets in SAP; an open endpoint is an
  open complaint-injection API.
