"""Custom tool handlers for Dograh.

A small service where integration logic that can't be expressed as a plain HTTP
tool lives — anything needing response headers, a persisted session, a
multi-step handshake, or bespoke response shaping.

It runs in its own container, deliberately:

* A handler that blocks or hangs cannot stall the voice pipeline. The API
  process runs calls on an async event loop; one wedged integration in there
  would degrade every concurrent call, not just the one that triggered it.
* A handler that crashes takes down this service, not the platform.
* Integrations can be redeployed in seconds without restarting the API.

It is reachable only on the internal Docker network. Dograh tools point at
``http://handlers:8080/tools/<name>``; nginx never proxies it.

Auth is still required on that internal network. Dograh's custom HTTP tools
have no SSRF guard, so any org admin can point a tool at any internal address —
without a shared key, one org could invoke another org's integrations.
"""

import asyncio
import logging
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from handlers import integrations  # noqa: F401  (imported for registration)
from handlers.registry import get_handler, handler_names, manifest

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("handlers")

# Must stay below the calling Dograh tool's timeout_ms so this service fails
# first and can return a structured, speakable error rather than the caller
# hearing a bare timeout. See handlers/README.md.
HANDLER_TIMEOUT_SECONDS = float(os.environ.get("HANDLER_TIMEOUT_SECONDS", "8"))

app = FastAPI(title="Dograh custom tool handlers", docs_url=None, redoc_url=None)


async def require_api_key(x_handler_key: str = Header(default="")) -> None:
    """Shared-secret auth. Compared in constant time to avoid leaking the key
    a byte at a time through response timing."""
    expected = os.environ.get("HANDLERS_API_KEY", "")
    if not expected:
        # Refuse to run unauthenticated rather than defaulting open — this
        # service creates real records in customer systems.
        raise HTTPException(status_code=503, detail="HANDLERS_API_KEY is not configured")
    if not secrets.compare_digest(x_handler_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Handler-Key")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "handlers": handler_names()}


@app.get("/_manifest", dependencies=[Depends(require_api_key)])
async def tool_manifest() -> dict[str, Any]:
    """Every handler's tool definition.

    `api/scripts/sync_handler_tools.py` reads this and creates or updates the
    matching Dograh tools, so a new integration never has to be re-typed into
    the UI. The schema is generated from the same decorator the handler is
    registered with, so the two cannot drift.
    """
    return {"tools": manifest()}


@app.post("/execute", dependencies=[Depends(require_api_key)])
async def execute_workspace(payload: dict[str, Any]) -> JSONResponse:
    """Run an organization's Code Editor workspace against one event.

    Lives here rather than in the API for the same reason the handlers do: user
    code must not share a process with the voice pipeline. A run that hangs or
    exhausts memory affects this container only.
    """
    from handlers.runtime.executor import ExecutionRequest, execute

    files = payload.get("files") or {}
    if not isinstance(files, dict) or not files:
        return JSONResponse(
            {"statusCode": 400, "error": "No files supplied.", "logs": ""}
        )

    result = await execute(
        ExecutionRequest(
            files={str(k): str(v) for k, v in files.items()},
            event=payload.get("event") or {},
            context=payload.get("context") or {},
            env={str(k): str(v) for k, v in (payload.get("env") or {}).items()},
            timeout_seconds=float(
                payload.get("timeout_seconds") or HANDLER_TIMEOUT_SECONDS
            ),
        )
    )
    return JSONResponse(result.as_dict())


@app.post("/tools/{function_name}", dependencies=[Depends(require_api_key)])
async def run_tool(function_name: str, payload: dict[str, Any]) -> JSONResponse:
    """Invoke one handler.

    Always answers HTTP 200 with a ``status`` field. A non-2xx would give the
    voice agent nothing it can say to the caller, so failures are reported in
    the body instead — every response carries a ``speak`` string the agent can
    read out, and a ``trace_id`` that ties it to the server-side logs.
    """
    trace_id = uuid.uuid4().hex[:8]

    # A catch-all route may carry the real name in the body, matching the
    # convention of injecting function_name as a Dograh preset parameter.
    if function_name == "dispatch":
        function_name = str(payload.get("function_name", "")).strip()

    func = get_handler(function_name)
    if func is None:
        logger.warning("unknown_handler trace=%s name=%s", trace_id, function_name)
        return JSONResponse(
            {
                "status": "error",
                "speak": "I can't do that right now.",
                "message": f"No handler registered for {function_name!r}.",
                "trace_id": trace_id,
            }
        )

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(func(payload), timeout=HANDLER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "handler_timeout trace=%s name=%s after=%.1fs",
            trace_id, function_name, time.monotonic() - started,
        )
        return JSONResponse(
            {
                "status": "error",
                # Never assert failure on a timeout: the downstream system may
                # have completed the write. Telling the caller it failed invites
                # a duplicate.
                "speak": "I couldn't confirm that in time. Let me take your "
                         "details and our team will follow up.",
                "message": "Handler timed out; outcome unknown.",
                "trace_id": trace_id,
            }
        )
    except Exception:
        # Full detail to the log, never to the caller — the response is injected
        # into the model's context and persisted in the call transcript.
        logger.exception("handler_error trace=%s name=%s", trace_id, function_name)
        return JSONResponse(
            {
                "status": "error",
                "speak": "Something went wrong on my side.",
                "message": "The handler raised an unexpected error.",
                "trace_id": trace_id,
            }
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "handler_ok trace=%s name=%s status=%s ms=%s",
        trace_id, function_name, result.get("status"), elapsed_ms,
    )
    result.setdefault("trace_id", trace_id)
    return JSONResponse(result)
