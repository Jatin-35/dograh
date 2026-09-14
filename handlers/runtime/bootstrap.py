"""Runs inside the sandbox subprocess. Never imported by the parent.

Materialises a workspace, calls its router, and writes the result somewhere the
parent can read it. Deliberately tiny and dependency-free: everything here runs
in the same process as user code, so anything imported becomes reachable by it.

The result goes to a file rather than stdout because stdout belongs to the
user's ``print`` statements — the spec requires those be captured and shown, and
interleaving them with the return value would make both unparseable.
"""

import json
import os
import runpy
import sys
import traceback

ENTRY_POINT = "all_events_entry_point.py"
HANDLER = "all_events_handler"


def _apply_limits() -> None:
    """Best-effort resource caps.

    Unavailable on Windows, where `resource` does not exist — local development
    then runs without caps, which is why the container is the enforcement point
    and not the developer's machine.
    """
    # Popped first, and unconditionally: these must not be visible to user code
    # even on a platform where the limits themselves can't be applied.
    cpu_seconds = int(os.environ.pop("_SANDBOX_CPU_SECONDS", "10"))
    address_space = int(os.environ.pop("_SANDBOX_MEMORY_BYTES", str(512 * 1024 * 1024)))

    try:
        import resource
    except ImportError:
        return

    # CPU stops a busy loop that ignores the wall-clock timeout; address space
    # stops a runaway allocation taking the whole container's memory with it.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    # No core dumps: they can be large and would contain the org's secrets.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Cap new files so user code can't fill the container's disk.
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))


def _restrict_import_path(workspace: str) -> None:
    """Make the workspace importable and the platform not.

    Removing everything would take the standard library with it — `runpy` itself
    needs `pkgutil`. What has to go is the *application* root: with it on the
    path, user code could `import api.db` and hold the platform's database
    client. Third-party packages stay available, because handlers that can't
    call an HTTP library aren't much use.
    """
    # This file is <app_root>/handlers/runtime/bootstrap.py.
    app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    blocked = {app_root, script_dir, os.path.join(app_root, "handlers"), ""}

    kept = [p for p in sys.path if os.path.abspath(p or ".") not in {
        os.path.abspath(b or ".") for b in blocked
    }]
    sys.path[:] = [workspace] + kept


def main() -> int:
    workspace, payload_path, result_path = sys.argv[1:4]
    _apply_limits()

    with open(payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)

    os.chdir(workspace)
    _restrict_import_path(workspace)

    outcome: dict[str, object]
    try:
        module = runpy.run_path(os.path.join(workspace, ENTRY_POINT))
        func = module.get(HANDLER)
        if not callable(func):
            raise RuntimeError(
                f"{ENTRY_POINT} does not define a callable {HANDLER}(event, context)."
            )
        result = func(payload["event"], payload["context"])
        if hasattr(result, "__await__"):
            import asyncio

            result = asyncio.run(result)
        # Fail here rather than at the caller: a result that can't be serialised
        # would otherwise surface as an opaque parse error far from its cause.
        json.dumps(result)
        outcome = {"ok": True, "result": result}
    except BaseException as exc:  # noqa: BLE001 - user code, report anything
        outcome = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            # Only the user's frames are useful; the bootstrap's own are noise.
            "traceback": "".join(traceback.format_exception(exc))[-4000:],
        }

    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(outcome, fh, default=str)
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
