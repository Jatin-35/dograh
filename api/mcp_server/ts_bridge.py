"""Python-side bridge to the Node TS validator.

Spawns `node api/mcp_server/ts_validator/src/index.ts` as a short-lived
subprocess per call, streams a JSON request on stdin, reads a JSON
response from stdout. The validator never executes LLM code — it either
emits TypeScript from a workflow JSON (`generate`) or parses LLM-authored
TS back into a workflow JSON via AST walking (`parse`).

The subprocess startup cost is ~100-200ms per call. Fine for MCP tool
rates; if it ever matters, the validator can be promoted to a long-lived
worker over a unix socket without changing this interface.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from api.services.workflow.dto import EdgeDataDTO
from api.services.workflow.node_specs import all_specs

_VALIDATOR_ENTRY = Path(__file__).resolve().parent / "ts_validator" / "src" / "index.ts"

# Parse/generate is pure AST work on a single file — a healthy run is well
# under a second including Node startup. Anything approaching this bound is a
# wedged process, not slow work, and must not hold a request open.
_VALIDATOR_TIMEOUT_SECONDS = 30


class TsBridgeError(Exception):
    """The Node subprocess failed before producing a JSON response."""


def _specs_payload() -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in all_specs()]


def _edge_field_names() -> list[str]:
    return list(EdgeDataDTO.model_fields.keys())


def _run_validator_sync(request: dict[str, Any]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["node", str(_VALIDATOR_ENTRY)],
        input=json.dumps(request).encode("utf-8"),
        capture_output=True,
        timeout=_VALIDATOR_TIMEOUT_SECONDS,
    )


async def _invoke(request: dict[str, Any]) -> dict[str, Any]:
    # Runs the Node subprocess synchronously in a worker thread rather than
    # via asyncio.create_subprocess_exec(). The latter needs
    # ProactorEventLoop on Windows — under SelectorEventLoop (which this app
    # runs under, for WebRTC-related reasons — see start_services_dev.ps1's
    # top-of-file note) it raises NotImplementedError at
    # BaseEventLoop._make_subprocess_transport. A thread-run subprocess.run()
    # doesn't touch the event loop's subprocess machinery at all, so it works
    # under either policy.
    try:
        result = await asyncio.to_thread(_run_validator_sync, request)
    except subprocess.TimeoutExpired as e:
        raise TsBridgeError(
            f"ts_validator did not respond within {_VALIDATOR_TIMEOUT_SECONDS}s "
            "and was terminated."
        ) from e
    except FileNotFoundError as e:
        raise TsBridgeError(
            "Node.js (>=22.6) is required to parse workflow source but the "
            "'node' executable was not found on PATH."
        ) from e
    stdout, stderr = result.stdout, result.stderr
    if result.returncode != 0 and not stdout:
        raise TsBridgeError(
            f"ts_validator exited {result.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')}"
        )
    try:
        return json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise TsBridgeError(
            f"ts_validator emitted non-JSON: {stdout!r} (stderr: {stderr!r})"
        ) from e


async def generate_code(workflow: dict[str, Any], *, workflow_name: str = "") -> str:
    """Emit SDK TypeScript source from a workflow JSON payload.

    Raises `TsBridgeError` if the validator can't produce code (unknown
    node type, dangling edge reference, etc.) — these are bugs at the
    caller layer, not user input, so we fail loudly.
    """
    result = await _invoke(
        {
            "command": "generate",
            "workflow": workflow,
            "specs": _specs_payload(),
            "edgeFieldNames": _edge_field_names(),
            "workflowName": workflow_name,
        }
    )
    if not result.get("ok"):
        errs = result.get("errors") or [{"message": "unknown failure"}]
        raise TsBridgeError(
            "generate_code failed: " + "; ".join(e.get("message", "") for e in errs)
        )
    return result["code"]


async def parse_code(code: str) -> dict[str, Any]:
    """Parse LLM-authored TS back into a workflow JSON.

    Returns the raw validator response — `{"ok": True, "workflow": {...}}`
    on success, `{"ok": False, "stage": "parse" | "validate", "errors": [...]}`
    on author-side failure. Author-side failures are surfaced to the LLM
    verbatim so it can iterate; callers should not re-wrap them.
    """
    return await _invoke(
        {
            "command": "parse",
            "code": code,
            "specs": _specs_payload(),
            "edgeFieldNames": _edge_field_names(),
        }
    )
