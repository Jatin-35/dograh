"""Scout's Code Editor tools.

Scout is the same agent here, with a different toolset. What has to hold: a
file write is never applied without the user accepting it, the card shows a
diff rather than a wall of proposed content, and a validation failure is
repaired by the model instead of shown to the user as an error.
"""

import json
from typing import Any

import pytest

from api.services.workflow_gen import agent_loop
from api.services.workflow_gen.tool_schemas import MUTATING_TOOLS, TOOL_SCHEMAS
from api.services.workflow_gen.toolbox import WorkflowGenToolboxError
from api.tests.test_workflow_gen_agent_loop import (
    _FakeCompletion,
    _FakeMessage,
    _FakeToolCall,
)

ROUTER = "all_events_entry_point.py"


# ---------------------------------------------------------------------------
# The contract with the model
# ---------------------------------------------------------------------------


def test_the_code_tools_are_exposed():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {
        "list_code_files",
        "read_code_file",
        "write_code_file",
        "delete_code_file",
        "test_code_run",
    } <= names


def test_writes_require_approval_and_reads_do_not():
    """The spec is explicit that generated code is never applied without the
    user accepting it; reads and sandboxed test runs must not nag."""
    assert "write_code_file" in MUTATING_TOOLS
    assert "delete_code_file" in MUTATING_TOOLS
    assert "read_code_file" not in MUTATING_TOOLS
    assert "list_code_files" not in MUTATING_TOOLS
    assert "test_code_run" not in MUTATING_TOOLS


def test_the_write_description_states_the_rules_the_model_keeps_breaking():
    schema = next(
        t for t in TOOL_SCHEMAS if t["function"]["name"] == "write_code_file"
    )
    description = schema["function"]["description"]
    assert "filename must equal" in description
    assert "function_name" in description


# ---------------------------------------------------------------------------
# The approval card
# ---------------------------------------------------------------------------


class _Toolbox:
    def __init__(self, existing: dict[str, str] | None = None):
        self.existing = existing or {}
        self.written: list[tuple[str, str]] = []

    async def read_code_file(self, path: str) -> dict[str, Any]:
        if path not in self.existing:
            raise WorkflowGenToolboxError(f"{path} does not exist.")
        return {"path": path, "content": self.existing[path]}

    async def write_code_file(self, path: str, content: str) -> dict[str, Any]:
        self.written.append((path, content))
        return {"saved": True, "path": path, "warnings": []}


@pytest.mark.asyncio
async def test_a_new_file_is_previewed_as_an_addition():
    preview = await agent_loop._preview_code_file(
        _Toolbox(), "function_definitions/get_order.json", '{\n  "name": "get_order"\n}'
    )
    assert preview["is_new"] is True
    assert preview["diff"].startswith("+")


@pytest.mark.asyncio
async def test_an_edit_is_previewed_as_a_diff_not_the_whole_file():
    """Handing someone 200 lines and asking 'ok?' is a rubber stamp, not a
    review."""
    before = "def all_events_handler(event, context):\n    return {}\n"
    after = "def all_events_handler(event, context):\n    return {'ok': True}\n"

    preview = await agent_loop._preview_code_file(_Toolbox({ROUTER: before}), ROUTER, after)

    assert preview["is_new"] is False
    assert preview["previous_chars"] == len(before)
    assert "-    return {}" in preview["diff"]
    assert "+    return {'ok': True}" in preview["diff"]
    # The unchanged first line appears as context, not as a change.
    assert "+def all_events_handler" not in preview["diff"]


@pytest.mark.asyncio
async def test_an_identical_rewrite_says_so():
    content = "def all_events_handler(event, context):\n    return {}\n"
    preview = await agent_loop._preview_code_file(_Toolbox({ROUTER: content}), ROUTER, content)
    assert "No change" in preview["diff"]


@pytest.mark.asyncio
async def test_an_enormous_diff_is_bounded():
    """The preview is persisted into the transcript and replayed every turn, so
    an unbounded diff would grow the context permanently."""
    before = "\n".join(f"line {i}" for i in range(2_000))
    after = "\n".join(f"changed {i}" for i in range(2_000))

    preview = await agent_loop._preview_code_file(_Toolbox({"helpers/big.py": before}), "helpers/big.py", after)

    assert len(preview["diff"].splitlines()) <= 205
    assert "more diff lines" in preview["diff"]


def test_the_summary_distinguishes_creating_from_rewriting():
    creating = agent_loop._summarize_mutating_call(
        "write_code_file", {"path": "helpers/sap.py", "content": "x = 1"}, {}
    )
    assert "create" in creating.lower()

    rewriting = agent_loop._summarize_mutating_call(
        "write_code_file",
        {"path": "helpers/sap.py", "content": "x = 1"},
        {"previous_chars": 400},
    )
    assert "rewrite" in rewriting.lower()
    assert "400" in rewriting


# ---------------------------------------------------------------------------
# Execution after approval
# ---------------------------------------------------------------------------


def _pending(path: str, content: str) -> dict[str, Any]:
    return {
        "action_id": "a1",
        "tool_call_id": "call-1",
        "action_type": "write_code_file",
        "arguments": {"path": path, "content": content},
        "sibling_call_ids": [],
    }


def _prior(tool_name: str = "write_code_file") -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "add a function"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
    ]


def _install(monkeypatch, *, write_effects: list[Any], completions: list[Any]):
    attempts: list[tuple[str, str]] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def write_code_file(self, path, content):
            attempts.append((path, content))
            outcome = write_effects.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    async def _complete(messages, tools, **kwargs):
        return completions.pop(0)

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _complete)
    return attempts


@pytest.mark.asyncio
async def test_approving_writes_the_file(monkeypatch):
    attempts = _install(
        monkeypatch,
        write_effects=[{"saved": True, "path": "helpers/sap.py", "warnings": []}],
        completions=[_FakeCompletion(_FakeMessage(content="Saved it."))],
    )

    events = [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=_prior(),
            pending_action=_pending("helpers/sap.py", "x = 1"),
            approve=True,
        )
    ]

    assert attempts == [("helpers/sap.py", "x = 1")]
    assert [e for e in events if e["type"] == "error"] == []


@pytest.mark.asyncio
async def test_declining_writes_nothing(monkeypatch):
    attempts = _install(
        monkeypatch,
        write_effects=[],
        completions=[_FakeCompletion(_FakeMessage(content="Understood, left it alone."))],
    )

    async for _ in agent_loop.execute_confirmed_action(
        organization_id=1,
        user_id=1,
        prior_messages=_prior(),
        pending_action=_pending("helpers/sap.py", "x = 1"),
        approve=False,
    ):
        pass

    assert attempts == []


@pytest.mark.asyncio
async def test_a_validation_failure_is_repaired_rather_than_shown_as_an_error(monkeypatch):
    """A schema whose filename doesn't match its name is the model's mistake to
    fix, not something to show the user a red card for."""
    good = json.dumps({"name": "get_order", "description": "d", "parameters": {"type": "object", "properties": {}}})
    attempts = _install(
        monkeypatch,
        write_effects=[
            WorkflowGenToolboxError(
                "file is not valid.",
                errors=["Filename and name disagree: file is 'get_order'.json but 'name' is 'getOrder'."],
            ),
            {"saved": True, "path": "function_definitions/get_order.json", "warnings": []},
        ],
        completions=[
            _FakeCompletion(
                _FakeMessage(
                    tool_calls=[
                        _FakeToolCall(
                            "call-2",
                            "write_code_file",
                            {"path": "function_definitions/get_order.json", "content": good},
                        )
                    ]
                )
            ),
            _FakeCompletion(_FakeMessage(content="Fixed the name and saved it.")),
        ],
    )

    events = [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=_prior(),
            pending_action=_pending("function_definitions/get_order.json", "{}"),
            approve=True,
        )
    ]

    assert len(attempts) == 2, "the model should get a second attempt"
    assert [e for e in events if e["type"] == "error"] == []
    # The corrected write is not re-confirmed — the user already approved it.
    assert [e for e in events if e["type"] == "approval"] == []
