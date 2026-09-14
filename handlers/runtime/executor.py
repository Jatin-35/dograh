"""Execute an organization's Code Editor workspace in a subprocess.

What this does and does not protect against, stated plainly because the
difference matters:

**Does**: a crash, an infinite loop, a runaway allocation, a process that writes
gigabytes, code that reads the platform's own secrets out of the environment, or
one organization seeing another's files. Each run gets a fresh temporary
directory, a replaced (not inherited) environment, CPU and memory caps, and a
wall-clock kill.

**Does not**: stop determined code from reaching the network, from reading files
the container's user can read, or from exhausting shared resources in ways
rlimits miss. A subprocess is a robustness boundary, not a security boundary
against code that is actively trying to escape.

`execute()` is deliberately the only entry point, so replacing this with gVisor,
Firecracker or a hosted sandbox later is one module, not a refactor.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BOOTSTRAP = Path(__file__).with_name("bootstrap.py")

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
MAX_LOG_CHARS = 20_000
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024


@dataclass
class ExecutionResult:
    status_code: int
    result: Any = None
    logs: str = ""
    error: str | None = None
    traceback: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "statusCode": self.status_code,
            "result": self.result,
            "logs": self.logs,
            "durationMs": self.duration_ms,
        }
        if self.error:
            payload["error"] = self.error
        if self.traceback:
            payload["traceback"] = self.traceback
        return payload


@dataclass
class ExecutionRequest:
    files: dict[str, str]
    event: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    memory_bytes: int = DEFAULT_MEMORY_BYTES


def _materialise(workspace: Path, files: dict[str, str]) -> None:
    """Write the virtual workspace to disk for this run only."""
    total = 0
    for path, content in files.items():
        # The stored path is already validated, but this is the point where a
        # bad one would become a write outside the sandbox, so it is checked
        # again here rather than trusted.
        target = (workspace / path).resolve()
        if not str(target).startswith(str(workspace.resolve())):
            raise ValueError(f"Refusing to write outside the workspace: {path!r}")
        total += len(content.encode())
        if total > MAX_WORKSPACE_BYTES:
            raise ValueError("Workspace exceeds the maximum size.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _child_env(env: dict[str, str], timeout_seconds: float, memory_bytes: int) -> dict[str, str]:
    """The child's entire environment.

    Built from nothing rather than copied from the parent: inheriting would hand
    user code DATABASE_URL, SAP credentials, the Fernet key and every other
    secret this container holds.
    """
    child = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Consumed and removed by the bootstrap before user code runs.
        "_SANDBOX_CPU_SECONDS": str(max(1, int(timeout_seconds))),
        "_SANDBOX_MEMORY_BYTES": str(memory_bytes),
    }

    if sys.platform == "win32":
        # Windows cannot initialise sockets — so asyncio cannot start — without
        # SYSTEMROOT. Not a secret, and only needed so local development works;
        # the container is Linux and takes neither of these.
        for name in ("SYSTEMROOT", "SystemRoot", "COMSPEC"):
            if name in os.environ:
                child[name] = os.environ[name]
    for key, value in env.items():
        # Never let a declared variable shadow the sandbox's own controls.
        if key.startswith("_SANDBOX_") or key in {"PATH", "PYTHONPATH"}:
            continue
        child[key] = str(value)
    return child


def _truncate(text: str) -> str:
    if len(text) <= MAX_LOG_CHARS:
        return text
    omitted = len(text) - MAX_LOG_CHARS
    return f"{text[:MAX_LOG_CHARS]}\n… [{omitted} more characters omitted]"


async def execute(request: ExecutionRequest) -> ExecutionResult:
    """Run the workspace's router against one event."""
    started = asyncio.get_running_loop().time()
    workspace = Path(tempfile.mkdtemp(prefix="dograh-code-"))

    try:
        try:
            _materialise(workspace, request.files)
        except ValueError as exc:
            return ExecutionResult(status_code=400, error=str(exc))

        payload_path = workspace.parent / f"{workspace.name}.payload.json"
        result_path = workspace.parent / f"{workspace.name}.result.json"
        payload_path.write_text(
            json.dumps({"event": request.event, "context": request.context}, default=str),
            encoding="utf-8",
        )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(BOOTSTRAP),
            str(workspace),
            str(payload_path),
            str(result_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_env(request.env, request.timeout_seconds, request.memory_bytes),
            cwd=str(workspace),
        )

        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=request.timeout_seconds
            )
        except asyncio.TimeoutError:
            # kill, not terminate: code that ignores SIGTERM is exactly the code
            # that got us here.
            process.kill()
            await process.wait()
            return ExecutionResult(
                status_code=504,
                error=(
                    f"Execution exceeded {request.timeout_seconds:g}s and was stopped. "
                    "On a live call this would be silence, so keep handlers fast."
                ),
                duration_ms=int(
                    (asyncio.get_running_loop().time() - started) * 1000
                ),
            )

        logs = _truncate((stdout or b"").decode("utf-8", errors="replace"))
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)

        if not result_path.exists():
            # The child died before writing — killed by an rlimit, most likely.
            return ExecutionResult(
                status_code=500,
                logs=logs,
                error=(
                    "The code stopped without returning a result. This usually "
                    "means it ran out of memory or CPU time."
                ),
                duration_ms=duration_ms,
            )

        outcome = json.loads(result_path.read_text(encoding="utf-8"))
        if outcome.get("ok"):
            return ExecutionResult(
                status_code=200,
                result=outcome.get("result"),
                logs=logs,
                duration_ms=duration_ms,
            )
        return ExecutionResult(
            status_code=500,
            logs=logs,
            error=outcome.get("error"),
            traceback=outcome.get("traceback"),
            duration_ms=duration_ms,
        )

    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        for leftover in (
            workspace.parent / f"{workspace.name}.payload.json",
            workspace.parent / f"{workspace.name}.result.json",
        ):
            try:
                os.unlink(leftover)
            except OSError:
                pass
