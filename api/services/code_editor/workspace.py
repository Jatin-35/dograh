"""Workspace operations: files, versions, environment variables, test runs.

Routes stay thin and call in here; nothing in this module knows about HTTP, so
Scout's tools and the REST API drive exactly the same code rather than two
implementations that drift.
"""

import logging
import os
from typing import Any, Optional

import httpx

from api.db import db_client
from api.db.models import CodeEditorVersionModel
from api.services.code_editor import secrets
from api.services.code_editor.validation import (
    ENTRY_POINT_PATH,
    ValidationResult,
    validate_file,
)

logger = logging.getLogger(__name__)

# The handlers service owns execution — it is the container isolated from the
# voice pipeline, so the sandbox lives there and the API calls into it.
HANDLERS_URL = os.environ.get("HANDLERS_URL", "http://localhost:8080")

# How much longer than the sandbox's own kill we are willing to wait on the
# HTTP call. The ordering is the point, and it is what a live call depends on:
#
#     sandbox kill (10s)  <  this HTTP wait (12s)  <  tool timeout (15s)
#
# Above the sandbox, so in the normal case — user code hangs — the sandbox wins
# the race and we relay its structured, speakable error. Below the calling
# tool's `DEFAULT_TOOL_TIMEOUT_MS`, so even when the handlers *service* itself
# wedges we still answer before the agent gives up, instead of holding an API
# worker long after the caller has stopped waiting.
#
# Derived rather than fixed, so a longer Test Run stays consistent instead of
# tripping a constant someone forgot to raise alongside it.
EXECUTE_TIMEOUT_MARGIN_SECONDS = 2.0

STARTER_ENTRY_POINT = '''"""Your organization's function router.

Every tool call from every agent arrives here. Route on `function_name`, which
the platform injects automatically — you never declare it in a schema.

`context` carries the call it came from: workflow_id, workflow_run_id, the
caller's number and your organization id. Use it when the same function has to
behave differently depending on which agent or which caller triggered it.
"""


def all_events_handler(event, context):
    function_name = event.get("function_name")

    if function_name == "get_order_status":
        order_id = event.get("order_id")
        # Call your API or database here.
        return {
            "status": "shipped",
            "tracking_number": "TRK123456",
            # `speak` is what the agent reads to the caller. Keep everything
            # you return small: it goes into the model's context and is stored
            # in the call transcript.
            "speak": f"Order {order_id} has shipped.",
        }

    return {"error": f"Unknown function: {function_name}"}
'''

STARTER_FUNCTION = """{
  "name": "get_order_status",
  "description": "Gets the current delivery status of a customer order. Ask the caller for their order number first if they haven't given it.",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "order_id": {
        "type": "string",
        "description": "The customer's order number, for example ORD-12345."
      }
    },
    "additionalProperties": false,
    "required": ["order_id"]
  }
}
"""

STARTER_FILES = {
    ENTRY_POINT_PATH: STARTER_ENTRY_POINT,
    "function_definitions/get_order_status.json": STARTER_FUNCTION,
}


class WorkspaceError(Exception):
    def __init__(self, message: str, *, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------


async def ensure_workspace(organization_id: int) -> dict[str, str]:
    """Return the draft, seeding a starter workspace the first time.

    A first-time user opening an empty tree has nothing to learn from. The
    starter files are a working example of every convention this feature
    depends on — the router signature, the schema shape, the filename rule.
    """
    files = await db_client.get_code_editor_workspace(organization_id)
    if files:
        return files
    for path, content in STARTER_FILES.items():
        await db_client.upsert_code_editor_file(organization_id, path, content)
    return dict(STARTER_FILES)


async def save_file(
    organization_id: int, path: str, content: str, user_id: Optional[int] = None
) -> ValidationResult:
    """Validate then persist. Invalid files are refused, not stored.

    Validating on save rather than on deploy is deliberate: a malformed schema
    caught here costs five seconds, and caught at deploy costs a broken agent
    mid-call.
    """
    result = validate_file(path, content)
    if not result.ok:
        raise WorkspaceError(f"{path} is not valid.", errors=result.errors)
    await db_client.upsert_code_editor_file(organization_id, path, content, user_id)
    return result


async def delete_file(organization_id: int, path: str) -> bool:
    if path == ENTRY_POINT_PATH:
        # Without a router nothing can be dispatched, and the failure would
        # surface as every tool call failing rather than as a missing file.
        raise WorkspaceError(f"{ENTRY_POINT_PATH} cannot be deleted.")
    return await db_client.delete_code_editor_file(organization_id, path)


# ----------------------------------------------------------------------
# Environment variables
# ----------------------------------------------------------------------


async def list_env_vars(organization_id: int) -> list[dict[str, Any]]:
    """Keys and hints only — a stored value is never returned again.

    `length` lets a caller mask a value with exactly as many placeholder
    characters as it actually has, instead of a fixed, made-up width. It is
    `None` for a row saved before this was tracked — there is no plaintext
    left to recover it from.
    """
    rows = await db_client.list_code_editor_env_vars(organization_id)
    return [
        {
            "key": row.key,
            "hint": row.value_hint,
            "length": row.value_length,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


async def set_env_var(organization_id: int, key: str, value: str) -> None:
    if not key or not key.replace("_", "").isalnum():
        raise WorkspaceError(
            f"{key!r} is not a valid variable name (letters, digits, underscores)."
        )
    await db_client.upsert_code_editor_env_var(
        organization_id,
        key,
        secrets.encrypt(value),
        secrets.hint(value),
        len(value),
    )


async def resolve_env(organization_id: int) -> dict[str, str]:
    """Decrypt the org's variables for one execution.

    A variable that cannot be decrypted is skipped rather than failing the whole
    run — usually it predates a key rotation, and failing everything would make
    one stale entry look like a broken platform.
    """
    resolved: dict[str, str] = {}
    for row in await db_client.list_code_editor_env_vars(organization_id):
        try:
            resolved[row.key] = secrets.decrypt(row.key, row.value_encrypted)
        except Exception:
            logger.warning(
                "code_editor env var %r for org %s could not be decrypted; skipping",
                row.key,
                organization_id,
            )
    return resolved


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def build_context(
    organization_id: int,
    workflow_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
    caller_number: Optional[str] = None,
) -> dict[str, Any]:
    """The `context` argument handed to the router.

    Dograh has no `business` entity — the reference platform's model is one
    WhatsApp number per business. The equivalent routing information here is
    which agent and which call the function was invoked from, so that is what
    gets injected.
    """
    return {
        "organization_id": organization_id,
        "workflow_id": workflow_id,
        "workflow_run_id": workflow_run_id,
        "caller_number": caller_number,
    }


async def run_test(
    organization_id: int,
    event: dict[str, Any],
    *,
    files: Optional[dict[str, str]] = None,
    timeout_seconds: float = 10.0,
    workflow_id: Optional[int] = None,
    workflow_run_id: Optional[int] = None,
    caller_number: Optional[str] = None,
) -> dict[str, Any]:
    """Execute a workspace snapshot against a test payload.

    `files=None` means the draft — the point of Test Run is to iterate before
    deploying, and silently running the deployed code would make every edit
    appear to have no effect. Callers testing a specific snapshot (the
    deployed version, or a real live call replaying it) pass `files`
    explicitly instead.

    `workflow_id`/`workflow_run_id`/`caller_number` are the real call this
    invocation belongs to. None from every caller except the actual runtime
    route (`run_deployed_function`), which is the only place a real call's
    identity exists — an interactive Test Latest/Test Deployed run has no call
    behind it, so its context is honestly org-only.
    """
    workspace = files if files is not None else await ensure_workspace(organization_id)
    if ENTRY_POINT_PATH not in workspace:
        raise WorkspaceError(f"{ENTRY_POINT_PATH} is missing from the workspace.")

    payload = {
        "files": workspace,
        "event": {**event, "function_name": event.get("function_name")},
        "context": build_context(
            organization_id,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            caller_number=caller_number,
        ),
        "env": await resolve_env(organization_id),
        "timeout_seconds": timeout_seconds,
    }

    try:
        http_timeout = timeout_seconds + EXECUTE_TIMEOUT_MARGIN_SECONDS
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.post(
                f"{HANDLERS_URL}/execute",
                json=payload,
                headers={"X-Handler-Key": os.environ.get("HANDLERS_API_KEY", "")},
            )
    except httpx.TimeoutException as exc:
        # Distinct from "unreachable": the service accepted the connection and
        # then did not answer. Saying "is the container running?" here would
        # send someone to check a container that is running fine.
        raise WorkspaceError(
            f"The code execution service did not respond within {http_timeout:g}s. "
            "The sandbox stops code after its own timeout, so this usually means "
            "the handlers service itself is overloaded rather than your code "
            "being slow."
        ) from exc
    except httpx.RequestError as exc:
        raise WorkspaceError(
            "Could not reach the code execution service. "
            f"({type(exc).__name__}: is the handlers container running?)"
        ) from exc

    if response.status_code == 401:
        raise WorkspaceError(
            "The code execution service rejected our key. "
            "HANDLERS_API_KEY must match on the api and handlers containers."
        )
    if response.is_error:
        raise WorkspaceError(
            f"The code execution service returned HTTP {response.status_code}."
        )
    return response.json()


# ----------------------------------------------------------------------
# Versions
# ----------------------------------------------------------------------


async def create_version(
    organization_id: int, description: Optional[str], user_id: Optional[int] = None
) -> CodeEditorVersionModel:
    """Snapshot the draft, refusing to freeze something invalid.

    A version is meant to be deployable. Letting a broken snapshot exist means
    discovering it at deploy time, when the obvious move is to roll back to a
    version that may itself be broken.
    """
    files = await db_client.get_code_editor_workspace(organization_id)
    if not files:
        raise WorkspaceError("There is nothing to snapshot — the workspace is empty.")

    problems: list[str] = []
    for path, content in sorted(files.items()):
        result = validate_file(path, content)
        problems.extend(f"{path}: {error}" for error in result.errors)
    if problems:
        raise WorkspaceError("The workspace has errors and cannot be versioned.", errors=problems)

    return await db_client.create_code_editor_version(
        organization_id, files, description, user_id
    )
