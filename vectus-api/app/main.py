"""Vectus area-manager lookup service.

    POST /lookup   {product, city?, district?, state?}  ->  {status, ...}
    GET  /health

Always answers HTTP 200 with a ``status`` for anything the bot can act on;
Dograh's HTTP tool hands the body to the LLM either way, and a status reads
better than an error page. 401/422 remain for a misconfigured caller.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import mavis
from .data import KBError, build_index, load_index
from .matching import Lookup, lookup
from .schemas import LookupRequest

KB_PATH = os.environ.get(
    "VECTUS_KB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "Vectus_KB_Revised.txt")
)
API_KEY = os.environ.get("VECTUS_API_KEY", "")
# Live area-manager table (LeadSquared Mavis). Unset = serve the KB file only.
MAVIS_URL = os.environ.get("MAVIS_URL", "")
MAVIS_API_KEY = os.environ.get("MAVIS_API_KEY", "")
MAVIS_REFRESH_HOURS = float(os.environ.get("MAVIS_REFRESH_HOURS", "6"))

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vectus.api")


def _load_from_mavis():
    rows = mavis.load_rows(MAVIS_URL, MAVIS_API_KEY, KB_PATH)
    return build_index(rows, strict_aliases=False, source="mavis")


async def refresh_index(app: FastAPI) -> bool:
    """Swap in a fresh Mavis index; keep serving the current one on any failure."""
    try:
        index = await asyncio.to_thread(_load_from_mavis)
    except (mavis.MavisError, KBError) as exc:
        app.state.refresh_error = str(exc)
        log.warning("Mavis refresh failed, still serving %s data: %s",
                    app.state.index.source, exc)
        return False
    app.state.index = index
    app.state.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    app.state.refresh_error = None
    return True


async def _refresh_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(MAVIS_REFRESH_HOURS * 3600)
        await refresh_index(app)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not API_KEY:
        raise RuntimeError("VECTUS_API_KEY is not set; refusing to start without auth")
    # The KB file always loads first: it is the fallback, and a bad KB must
    # still stop startup (raises KBError).
    app.state.index = load_index(KB_PATH)
    app.state.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    app.state.refresh_error = None
    task = None
    if MAVIS_URL and MAVIS_API_KEY:
        await refresh_index(app)
        task = asyncio.create_task(_refresh_loop(app))
    yield
    if task:
        task.cancel()


# redirect_slashes off: "/lookup/" would otherwise answer 307, and Dograh's HTTP
# tool does not follow redirects on POST — one stray slash in the tool URL
# would silently break every lookup. Both spellings are served instead.
app = FastAPI(title="Vectus lookup", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None, redirect_slashes=False)


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    missing_product = any(e.get("loc", [None])[-1] == "product" for e in exc.errors())
    if missing_product:
        # The bot forgot the product: tell it what to do rather than a 422.
        return JSONResponse({
            "status": "invalid_product",
            "allowed": ["Water tank", "Moundling"],
            "message": "Product is required. Ask the caller whether they need a "
                       "water tank or moulding.",
        })
    # Only field and reason: echoing the input back can carry bytes (a non-JSON
    # body) that the response cannot serialise.
    detail = [{"field": ".".join(str(p) for p in e.get("loc", [])), "error": e.get("msg")}
              for e in exc.errors()]
    return JSONResponse({"status": "bad_request", "detail": detail}, status_code=422)


def _check_key(key: str | None) -> None:
    if not key or not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/health")
@app.get("/health/", include_in_schema=False)
async def health(request: Request):
    ix = request.app.state.index
    return {"status": "ok", "records": len(ix.records), "contacts": ix.contacts,
            "source": ix.source, "loaded_at": request.app.state.loaded_at,
            "refresh_error": request.app.state.refresh_error}


@app.post("/lookup")
@app.post("/lookup/", include_in_schema=False)
async def post_lookup(body: LookupRequest, request: Request,
                      x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    started = time.perf_counter()
    result = lookup(request.app.state.index, Lookup(**body.model_dump()))
    # Log what was asked and the outcome, never the phone number.
    log.info(json.dumps({
        "event": "lookup",
        "request": body.model_dump(),
        "status": result["status"],
        "match": result.get("match"),
        "ms": round((time.perf_counter() - started) * 1000, 2),
    }, ensure_ascii=False))
    return result
