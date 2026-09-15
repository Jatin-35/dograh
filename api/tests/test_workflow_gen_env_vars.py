"""Scout's access to Code Editor environment variables.

Before this, Scout could read and write the workspace's *code* but had no way
to touch its secrets — it could tell a user which key to set, but not set it,
so every secret-carrying integration stalled on a manual step. What has to
hold, same as every other secret-carrying tool here: a value is never echoed
back once stored, a write requires the same explicit approval a file write
does, and a value is masked out of the approval card the user actually sees.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.workflow_gen import agent_loop
from api.services.workflow_gen.agent_loop import _mask_secret_arguments, _summarize_mutating_call
from api.services.workflow_gen.tool_schemas import MUTATING_TOOLS, SECRET_ARGUMENT_KEYS, TOOL_SCHEMAS
from api.services.workflow_gen.toolbox import WorkflowGenToolbox, WorkflowGenToolboxError
from api.tests.test_workflow_gen_agent_loop import _FakeCompletion, _FakeMessage

ORG_ID = 3

# ---------------------------------------------------------------------------
# The contract with the model
# ---------------------------------------------------------------------------


def test_the_env_var_tools_are_exposed():
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert {"list_env_vars", "set_env_var", "delete_env_var"} <= names


def test_writing_requires_approval_listing_does_not():
    """Same rule as the code files: a write is never applied silently, but a
    read must not stall the conversation with a card to approve."""
    assert "set_env_var" in MUTATING_TOOLS
    assert "delete_env_var" in MUTATING_TOOLS
    assert "list_env_vars" not in MUTATING_TOOLS


def test_a_value_is_masked_before_the_approval_card_is_built():
    """The regression this exists to prevent: a secret the user just typed in
    chat landing, unmasked, in the persisted approval-card payload."""
    assert "value" in SECRET_ARGUMENT_KEYS
    masked = _mask_secret_arguments({"key": "THINKGAS_COMPLAINT_PASSWORD", "value": "hunter2"})
    assert masked["key"] == "THINKGAS_COMPLAINT_PASSWORD"
    assert "hunter2" not in masked["value"]


def test_the_approval_summary_never_contains_the_value():
    summary = _summarize_mutating_call(
        "set_env_var", {"key": "THINKGAS_COMPLAINT_PASSWORD", "value": "hunter2"}
    )
    assert "THINKGAS_COMPLAINT_PASSWORD" in summary
    assert "hunter2" not in summary
    assert "hidden" in summary.lower()


def test_the_delete_summary_names_the_key():
    summary = _summarize_mutating_call("delete_env_var", {"key": "OLD_KEY"})
    assert "OLD_KEY" in summary


# ---------------------------------------------------------------------------
# The toolbox methods themselves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_env_vars_never_returns_a_value():
    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    with patch(
        "api.services.workflow_gen.toolbox.code_workspace.list_env_vars",
        AsyncMock(
            return_value=[
                {"key": "ORDERS_API_KEY", "hint": "5678", "length": 20},
                {"key": "THINKGAS_COMPLAINT_PASSWORD", "hint": "9999", "length": 12},
            ]
        ),
    ):
        result = await toolbox.list_env_vars()

    assert result == {
        "env_vars": [
            {"key": "ORDERS_API_KEY", "hint": "5678", "length": 20},
            {"key": "THINKGAS_COMPLAINT_PASSWORD", "hint": "9999", "length": 12},
        ]
    }
    assert "value" not in json.dumps(result)


@pytest.mark.asyncio
async def test_set_env_var_refuses_when_encryption_is_not_configured():
    """Scout must say this plainly rather than silently doing nothing or
    pretending it worked — it needs a deployment operator, not a retry."""
    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    with patch(
        "api.services.workflow_gen.toolbox.code_secrets.is_configured", return_value=False
    ):
        with pytest.raises(WorkflowGenToolboxError, match="CODE_EDITOR_ENCRYPTION_KEY"):
            await toolbox.set_env_var("ORDERS_API_KEY", "sk-123")


@pytest.mark.asyncio
async def test_set_env_var_stores_the_value_and_returns_only_a_hint():
    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    set_env_var = AsyncMock()
    with patch(
        "api.services.workflow_gen.toolbox.code_secrets.is_configured", return_value=True
    ), patch("api.services.workflow_gen.toolbox.code_workspace.set_env_var", set_env_var):
        result = await toolbox.set_env_var("ORDERS_API_KEY", "sk-super-secret-123")

    set_env_var.assert_awaited_once_with(ORG_ID, "ORDERS_API_KEY", "sk-super-secret-123")
    assert result["saved"] is True
    assert result["key"] == "ORDERS_API_KEY"
    # The length is what lets a confirmation mask a value with exactly as
    # many placeholder characters as it actually has — get it wrong here and
    # every "Saved ORDERS_API_KEY" confirmation is lying about the length.
    assert result["length"] == len("sk-super-secret-123")
    assert "sk-super-secret-123" not in json.dumps(result)


@pytest.mark.asyncio
async def test_set_env_var_surfaces_an_invalid_name_for_the_model_to_fix():
    from api.services.code_editor.workspace import WorkspaceError

    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    with patch(
        "api.services.workflow_gen.toolbox.code_secrets.is_configured", return_value=True
    ), patch(
        "api.services.workflow_gen.toolbox.code_workspace.set_env_var",
        AsyncMock(side_effect=WorkspaceError("'bad key!' is not a valid variable name.")),
    ):
        with pytest.raises(WorkflowGenToolboxError, match="not a valid variable name"):
            await toolbox.set_env_var("bad key!", "x")


@pytest.mark.asyncio
async def test_delete_env_var_reports_a_missing_key_clearly():
    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    with patch(
        "api.services.workflow_gen.toolbox.db_client.delete_code_editor_env_var",
        AsyncMock(return_value=False),
    ):
        with pytest.raises(WorkflowGenToolboxError, match="does not exist"):
            await toolbox.delete_env_var("NEVER_SET")


@pytest.mark.asyncio
async def test_delete_env_var_succeeds():
    toolbox = WorkflowGenToolbox(ORG_ID, user_id=1)
    with patch(
        "api.services.workflow_gen.toolbox.db_client.delete_code_editor_env_var",
        AsyncMock(return_value=True),
    ):
        result = await toolbox.delete_env_var("OLD_KEY")
    assert result == {"deleted": True, "key": "OLD_KEY"}


# ---------------------------------------------------------------------------
# The full approve/decline round trip through the agent loop
# ---------------------------------------------------------------------------


def _pending(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": "action-1",
        "tool_call_id": "call-1",
        "action_type": tool_name,
        "arguments": arguments,
        "sibling_call_ids": [],
    }


def _prior(tool_name: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "set the SAP password"},
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


@pytest.mark.asyncio
async def test_approving_set_env_var_calls_the_toolbox(monkeypatch):
    set_calls: list[tuple[str, str]] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def set_env_var(self, key, value):
            set_calls.append((key, value))
            return {"saved": True, "key": key, "hint": "3456"}

    async def _complete(messages, tools, **kwargs):
        return _FakeCompletion(_FakeMessage(content="Saved it."))

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _complete)

    events = [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=ORG_ID,
            user_id=1,
            prior_messages=_prior("set_env_var"),
            pending_action=_pending(
                "set_env_var",
                {"key": "THINKGAS_COMPLAINT_PASSWORD", "value": "correct-horse-3456"},
            ),
            approve=True,
        )
    ]

    assert set_calls == [("THINKGAS_COMPLAINT_PASSWORD", "correct-horse-3456")]
    assert [e for e in events if e["type"] == "error"] == []


@pytest.mark.asyncio
async def test_declining_set_env_var_writes_nothing(monkeypatch):
    set_calls: list[tuple[str, str]] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def set_env_var(self, key, value):
            set_calls.append((key, value))
            return {"saved": True, "key": key, "hint": "3456"}

    async def _complete(messages, tools, **kwargs):
        return _FakeCompletion(_FakeMessage(content="Understood, left it alone."))

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _complete)

    async for _ in agent_loop.execute_confirmed_action(
        organization_id=ORG_ID,
        user_id=1,
        prior_messages=_prior("set_env_var"),
        pending_action=_pending(
            "set_env_var", {"key": "THINKGAS_COMPLAINT_PASSWORD", "value": "x"}
        ),
        approve=False,
    ):
        pass

    assert set_calls == []


@pytest.mark.asyncio
async def test_approving_delete_env_var_calls_the_toolbox(monkeypatch):
    delete_calls: list[str] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def delete_env_var(self, key):
            delete_calls.append(key)
            return {"deleted": True, "key": key}

    async def _complete(messages, tools, **kwargs):
        return _FakeCompletion(_FakeMessage(content="Deleted it."))

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _complete)

    events = [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=ORG_ID,
            user_id=1,
            prior_messages=_prior("delete_env_var"),
            pending_action=_pending("delete_env_var", {"key": "OLD_KEY"}),
            approve=True,
        )
    ]

    assert delete_calls == ["OLD_KEY"]
    assert [e for e in events if e["type"] == "error"] == []


# ---------------------------------------------------------------------------
# list_env_vars is dispatched like every other safe/read-only tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_env_vars_is_dispatched_through_the_generic_safe_path():
    """No special-casing needed: `_dispatch_safe_tool` calls any toolbox
    method by name, so this only has to prove it's reachable, not stalled by
    a missing case somewhere."""
    toolbox = MagicMock()
    toolbox.list_env_vars = AsyncMock(return_value={"env_vars": []})

    result = await agent_loop._dispatch_safe_tool(toolbox, "list_env_vars", {})

    toolbox.list_env_vars.assert_awaited_once()
    assert result == {"env_vars": []}


# ---------------------------------------------------------------------------
# The system prompt describes the real shape of `context`
# ---------------------------------------------------------------------------


def test_the_prompt_no_longer_claims_a_call_key_holds_the_context():
    """This was a real, pre-existing bug in the prompt: it taught the model
    to read `event["call"]["workflow_id"]`, but the router's actual second
    argument is `context`, and there is no `call` key at all. Fixed alongside
    the env var tools since it's the same subsystem's documentation."""
    from api.services.workflow_gen.system_prompt import WORKFLOW_GEN_SYSTEM_PROMPT

    assert 'context["workflow_id"]' in WORKFLOW_GEN_SYSTEM_PROMPT
    assert "which carries `workflow_id`" not in WORKFLOW_GEN_SYSTEM_PROMPT


def test_the_prompt_teaches_scout_to_manage_secrets_itself():
    from api.services.workflow_gen.system_prompt import WORKFLOW_GEN_SYSTEM_PROMPT

    assert "set_env_var" in WORKFLOW_GEN_SYSTEM_PROMPT
    assert "list_env_vars" in WORKFLOW_GEN_SYSTEM_PROMPT
    assert "delete_env_var" in WORKFLOW_GEN_SYSTEM_PROMPT


def test_the_prompt_tells_scout_to_confirm_with_the_hint():
    """A stored value is never shown again anywhere in this feature — the
    hint (its last few characters) is the only way anyone, including Scout,
    can catch a mistyped paste after the fact. Confirming by key name alone
    would silently drop that safety net."""
    from api.services.workflow_gen.system_prompt import WORKFLOW_GEN_SYSTEM_PROMPT

    assert "hint" in WORKFLOW_GEN_SYSTEM_PROMPT
    assert "confirm by key name only" not in WORKFLOW_GEN_SYSTEM_PROMPT
