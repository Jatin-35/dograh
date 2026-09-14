"""The Code Editor sandbox.

The tests that earn their keep are the isolation ones: that user code cannot
read the platform's secrets out of the environment, cannot escape its workspace,
and cannot hang a call forever. Those are the properties that stop being true
quietly.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest

from handlers.runtime.executor import ExecutionRequest, execute

ROUTER = "all_events_entry_point.py"


def _files(body: str) -> dict[str, str]:
    return {ROUTER: f"def all_events_handler(event, context):\n{body}\n"}


# ---------------------------------------------------------------------------
# It runs code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_handler_returns_its_result():
    result = await execute(
        ExecutionRequest(
            files=_files("    return {'status': 'shipped', 'id': event['order_id']}"),
            event={"function_name": "get_order_status", "order_id": "ORD-1"},
        )
    )
    assert result.status_code == 200
    assert result.result == {"status": "shipped", "id": "ORD-1"}


@pytest.mark.asyncio
async def test_print_output_is_captured_as_logs():
    """The spec requires stdout back in the console, and it must not be confused
    with the return value."""
    result = await execute(
        ExecutionRequest(
            files=_files("    print('looking up', event['order_id'])\n    return {'ok': True}"),
            event={"order_id": "ORD-1"},
        )
    )
    assert result.status_code == 200
    assert "looking up ORD-1" in result.logs
    assert result.result == {"ok": True}


@pytest.mark.asyncio
async def test_helper_modules_can_be_imported():
    """The spec asks for a single router, not a single file."""
    files = {
        ROUTER: (
            "from helpers.orders import lookup\n"
            "def all_events_handler(event, context):\n"
            "    return lookup(event['order_id'])\n"
        ),
        "helpers/__init__.py": "",
        "helpers/orders.py": "def lookup(order_id):\n    return {'id': order_id, 'status': 'shipped'}\n",
    }
    result = await execute(ExecutionRequest(files=files, event={"order_id": "ORD-9"}))
    assert result.status_code == 200
    assert result.result["status"] == "shipped"


@pytest.mark.asyncio
async def test_an_async_handler_is_awaited():
    files = {
        ROUTER: (
            "async def all_events_handler(event, context):\n"
            "    return {'async': True}\n"
        )
    }
    result = await execute(ExecutionRequest(files=files, event={}))
    assert result.status_code == 200
    assert result.result == {"async": True}


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_secrets_are_not_visible_to_user_code(monkeypatch):
    """The environment is built from nothing, not inherited. Inheriting would
    hand user code DATABASE_URL, the Fernet key and every other secret."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://real:secret@db/prod")
    monkeypatch.setenv("SAP_BASIC_AUTH_HEADER", "Basic supersecret")
    monkeypatch.setenv("CODE_EDITOR_ENCRYPTION_KEY", "fernet-key-value")

    result = await execute(
        ExecutionRequest(
            files={
                ROUTER: (
                    "import os\n"
                    "def all_events_handler(event, context):\n"
                    "    return dict(os.environ)\n"
                )
            },
            event={},
            env={"MY_API_KEY": "org-scoped-value"},
        )
    )

    assert result.status_code == 200
    env = result.result
    assert env.get("MY_API_KEY") == "org-scoped-value"
    for leaked in ("DATABASE_URL", "SAP_BASIC_AUTH_HEADER", "CODE_EDITOR_ENCRYPTION_KEY"):
        assert leaked not in env, f"{leaked} leaked into the sandbox"


@pytest.mark.asyncio
async def test_declared_vars_cannot_shadow_the_sandbox_controls():
    result = await execute(
        ExecutionRequest(
            files={
                ROUTER: (
                    "import os\n"
                    "def all_events_handler(event, context):\n"
                    "    return {'cpu': os.environ.get('_SANDBOX_CPU_SECONDS'),\n"
                    "            'path': os.environ.get('PATH')}\n"
                )
            },
            event={},
            env={"_SANDBOX_CPU_SECONDS": "99999", "PATH": "/evil"},
        )
    )
    assert result.status_code == 200
    # The bootstrap consumes _SANDBOX_* before user code runs, and PATH is ours.
    assert result.result["cpu"] is None
    assert result.result["path"] != "/evil"


@pytest.mark.asyncio
async def test_a_path_escaping_the_workspace_is_refused():
    result = await execute(
        ExecutionRequest(files={"../../escape.py": "x = 1"}, event={})
    )
    assert result.status_code == 400
    assert "outside the workspace" in result.error


@pytest.mark.asyncio
async def test_the_platforms_own_modules_are_not_importable():
    """sys.path is replaced with the workspace alone, so user code cannot reach
    into the API package and use its database client."""
    result = await execute(
        ExecutionRequest(
            files={
                ROUTER: (
                    "def all_events_handler(event, context):\n"
                    "    import api.db\n"
                    "    return {'reached': True}\n"
                )
            },
            event={},
        )
    )
    assert result.status_code == 500
    assert "ModuleNotFoundError" in (result.error or "")


@pytest.mark.asyncio
async def test_two_runs_do_not_share_a_workspace():
    """Each execution gets a fresh directory, so one org's files can never be
    visible to another's code."""
    write = await execute(
        ExecutionRequest(
            files={
                ROUTER: (
                    "def all_events_handler(event, context):\n"
                    "    open('leaked.txt','w').write('secret')\n"
                    "    return {'wrote': True}\n"
                )
            },
            event={},
        )
    )
    assert write.status_code == 200

    read = await execute(
        ExecutionRequest(
            files={
                ROUTER: (
                    "import os\n"
                    "def all_events_handler(event, context):\n"
                    "    return {'files': sorted(os.listdir('.'))}\n"
                )
            },
            event={},
        )
    )
    assert "leaked.txt" not in read.result["files"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_infinite_loop_is_stopped():
    """On a live call this would be silence, so it must be bounded."""
    result = await execute(
        ExecutionRequest(
            files=_files("    while True:\n        pass"),
            event={},
            timeout_seconds=2.0,
        )
    )
    assert result.status_code == 504
    assert "exceeded" in result.error


@pytest.mark.asyncio
async def test_an_exception_returns_the_message_and_traceback():
    result = await execute(
        ExecutionRequest(files=_files("    raise ValueError('bad order id')"), event={})
    )
    assert result.status_code == 500
    assert "ValueError: bad order id" in result.error
    assert "bad order id" in result.traceback


@pytest.mark.asyncio
async def test_a_syntax_error_is_reported_not_raised():
    result = await execute(
        ExecutionRequest(files={ROUTER: "def all_events_handler(event, context)\n"}, event={})
    )
    assert result.status_code == 500
    assert "SyntaxError" in result.error


@pytest.mark.asyncio
async def test_a_missing_handler_is_reported_clearly():
    result = await execute(
        ExecutionRequest(files={ROUTER: "def other(event, context):\n    return {}\n"}, event={})
    )
    assert result.status_code == 500
    assert "all_events_handler" in result.error


@pytest.mark.asyncio
async def test_an_unserialisable_result_fails_where_it_happened():
    result = await execute(
        ExecutionRequest(files=_files("    return {'when': object()}"), event={})
    )
    assert result.status_code == 500
    assert "not JSON serializable" in (result.error or "") or "TypeError" in (result.error or "")


@pytest.mark.asyncio
async def test_enormous_output_is_truncated():
    """Logs are shown in a console and can reach a transcript; unbounded output
    is how a UI locks up."""
    result = await execute(
        ExecutionRequest(
            files=_files("    print('x' * 5_000_000)\n    return {'ok': True}"),
            event={},
            timeout_seconds=20.0,
        )
    )
    assert len(result.logs) < 30_000
    assert "omitted" in result.logs


@pytest.mark.asyncio
async def test_concurrent_executions_stay_independent():
    async def run(n: int):
        return await execute(
            ExecutionRequest(
                files=_files(f"    return {{'n': {n}}}"), event={}, timeout_seconds=15.0
            )
        )

    results = await asyncio.gather(*(run(i) for i in range(8)))
    assert [r.result["n"] for r in results] == list(range(8))


# ---------------------------------------------------------------------------
# It cleans up after itself
# ---------------------------------------------------------------------------


def _sandbox_leftovers() -> set[Path]:
    """Every temp artefact this module creates, by its own prefix."""
    root = Path(tempfile.gettempdir())
    return set(root.glob("dograh-code-*"))


@pytest.mark.asyncio
async def test_a_completed_run_leaves_nothing_on_disk():
    """A leaked workspace per call is invisible until the disk fills, which on
    a live deployment is an outage rather than a bug report. The sidecar
    payload/result files live *beside* the workspace, so removing the directory
    alone would not be enough."""
    before = _sandbox_leftovers()
    result = await execute(
        ExecutionRequest(files=_files("    return {'ok': True}"), event={})
    )
    assert result.status_code == 200
    assert _sandbox_leftovers() - before == set()


@pytest.mark.asyncio
async def test_a_crashed_run_leaves_nothing_on_disk():
    before = _sandbox_leftovers()
    await execute(
        ExecutionRequest(files=_files("    raise RuntimeError('boom')"), event={})
    )
    assert _sandbox_leftovers() - before == set()


@pytest.mark.asyncio
async def test_a_timed_out_run_leaves_nothing_on_disk():
    """The timeout path returns early, before the normal result handling — the
    cleanup has to be in `finally` to cover it, and this is what proves it is."""
    before = _sandbox_leftovers()
    result = await execute(
        ExecutionRequest(
            files=_files("    while True:\n        pass"),
            event={},
            timeout_seconds=1.0,
        )
    )
    assert result.status_code == 504
    assert _sandbox_leftovers() - before == set()


@pytest.mark.asyncio
async def test_a_refused_workspace_leaves_nothing_on_disk():
    """The escape check fires before the subprocess starts, which is its own
    early return out of the function."""
    before = _sandbox_leftovers()
    result = await execute(
        ExecutionRequest(files={"../escape.py": "x = 1"}, event={})
    )
    assert result.status_code == 400
    assert _sandbox_leftovers() - before == set()


# ---------------------------------------------------------------------------
# The sandbox's own controls are not user-visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sandbox_controls_are_removed_before_user_code_runs():
    """`_SANDBOX_CPU_SECONDS` and `_SANDBOX_MEMORY_BYTES` are how the bootstrap
    is told what caps to apply. They are popped unconditionally — including on
    a platform where `resource` is missing and the caps cannot be applied at
    all, which is the case this regressed in once: the pops sat after the
    `ImportError` return, so on Windows they stayed in the environment."""
    result = await execute(
        ExecutionRequest(
            files=_files(
                "    import os\n"
                "    return sorted(k for k in os.environ if k.startswith('_SANDBOX'))"
            ),
            event={},
        )
    )
    assert result.status_code == 200
    assert result.result == []


@pytest.mark.asyncio
async def test_user_code_cannot_see_the_parents_own_python_path():
    """PYTHONPATH is dropped from the child's environment, and a declared org
    variable cannot reinstate it — either would put the application root back
    within reach of an `import api.db`."""
    result = await execute(
        ExecutionRequest(
            files=_files(
                "    import os\n    return os.environ.get('PYTHONPATH', '<unset>')"
            ),
            event={},
            env={"PYTHONPATH": "/app"},
        )
    )
    assert result.status_code == 200
    assert result.result == "<unset>"
