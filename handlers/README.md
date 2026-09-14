# Custom tool handlers

Python behind a Dograh tool, for integrations a plain HTTP tool can't express:
response headers, a persisted session, a multi-step handshake, or a response
that needs shaping before the agent sees it.

Runs as its own container on the internal Docker network. Dograh tools call
`http://handlers:8080/tools/<name>`. Nothing is exposed publicly.

## Adding a handler

1. Create `handlers/integrations/<client>_<system>.py`:

```python
from handlers.registry import handler

@handler("check_order_status")
async def check_order_status(event: dict) -> dict:
    order_id = event["order_id"]
    ...
    return {
        "status": "success",
        "speak": f"Your order is out for delivery.",
        "order_status": "shipped",
    }
```

2. Import it in `handlers/integrations/__init__.py`.
3. Add any secret to the `handlers` service in `docker-compose.yaml`.
4. Register the Dograh tool (below).
5. Write tests. `test_think_gas_sap.py` is the reference.

The registry refuses duplicate names, so a clash fails at boot rather than
picking a winner at call time.

## What a handler must return

Always a dict. Always include `speak` — the agent reads it to the caller.

```python
{"status": "success", "speak": "Your complaint is registered. Reference 1 2 3.", ...}
{"status": "error",   "speak": "I couldn't do that just now.", "message": "<for logs>"}
```

Four rules, each learned the hard way:

**Keep the response small.** It is injected into the model's context and
persisted in the call transcript. Return what the agent needs to say plus a
couple of fields — never a raw upstream payload. An oversized tool result has
previously killed a conversation outright on this platform.

**Never return credentials.** Tokens, cookies and auth headers would land in the
transcript. Keep them inside the handler.

**Never claim failure on a timeout.** The upstream write may have succeeded.
Say "I couldn't confirm that in time", not "it failed" — the difference is
whether the caller ends up with two tickets.

**Space out digits in `speak`.** TTS reads `1000012345` as "one billion, twelve
thousand…". `" ".join(ticket_id)` makes it a reference number again.

## Timeouts

This is a voice platform. A slow handler is dead air, not a spinner.

```
Dograh tool timeout_ms   10000   ← what the caller waits on
HANDLER_TIMEOUT_SECONDS      8   ← this service gives up first
per-request upstream timeout ~5  ← the handler gives up before that
```

Set them in that order so the innermost fails first and something speakable
always comes back. Set `customMessage` on the Dograh tool ("one moment, let me
check that") — it is queued *before* the HTTP call, so it covers the wait.

## Registering the Dograh tool

```json
{
  "name": "create_complaint_with_fresh_csrf",
  "category": "http_api",
  "config": {
    "method": "POST",
    "url": "http://handlers:8080/tools/create_complaint_with_fresh_csrf",
    "credential_uuid": "<credential holding X-Handler-Key>",
    "timeout_ms": 10000,
    "customMessage": "Let me register that complaint for you, one moment.",
    "customMessageType": "text",
    "parameters": [
      {"name": "ImPartner", "type": "string", "required": true,
       "description": "SAP Business Partner number, verified with the caller."},
      {"name": "ImQcode", "type": "string", "required": true,
       "description": "QPPO for recharge or balance issues, QNCP for a new connection."},
      {"name": "ImNotes", "type": "string", "required": true,
       "description": "Concise plain-text summary, under 240 characters."}
    ],
    "preset_parameters": [
      {"name": "ImTicketcreationtag", "type": "string", "value_template": "voicebot"}
    ]
  }
}
```

Two things worth copying from that example:

- **Constants belong in `preset_parameters`, not `parameters`.** Asking the
  model to supply a fixed value is just a chance for it to supply another one.
  Presets are merged after the model decides and never enter its context.
- **The auth key goes in `credential_uuid`**, never in `headers` — a tool's
  config is visible to anyone who can edit the agent.

## Running locally

```bash
export HANDLERS_API_KEY=dev-key
export SAP_BASIC_AUTH_HEADER="Basic ..."
uvicorn handlers.main:app --port 8080 --reload

curl -s localhost:8080/health
curl -s -X POST localhost:8080/tools/create_complaint_with_fresh_csrf \
  -H "X-Handler-Key: dev-key" -H "Content-Type: application/json" \
  -d '{"ImPartner":"5060000019","ImQcode":"QPPO","ImNotes":"test","ImTicketcreationtag":"voicebot"}'
```

Tests: `python -m pytest handlers/tests -c handlers/pytest.ini`

## Deploying

```bash
cd ~/dograh && git pull origin botrix-main
sudo docker compose --profile remote build handlers
sudo docker compose --profile remote up -d handlers
```

Seconds, and the API is never restarted. Note there is one replica, so a
restart drops any tool call in flight during those few seconds — deploy when
traffic is low, or add a second replica behind an nginx upstream once more than
one client depends on it.

## Conventions

- Handlers are `async`. Use `httpx`, never blocking `requests`.
- Build clients **per request**, never at module level — this serves concurrent
  calls, and a shared cookie jar leaks one caller's session into another's.
- Log booleans and a `trace_id`. Never a token, cookie or `Authorization` value.
- Don't import Dograh's `api` package. This image stays small and independent.
