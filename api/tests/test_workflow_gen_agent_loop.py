"""Repair-loop behaviour for the in-product assistant's mutating tools.

`create_tool` used to be the odd one out: `create_workflow`/`save_workflow`
got bounded self-repair when the model emitted a bad payload, while a bad
`tool_definition` went straight to a user-visible error card with a raw
Pydantic dump. These tests pin the repaired behaviour — the model gets to
fix its own mistake first, and the user only ever sees an error once the
bounded attempts are exhausted.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from api.services.workflow_gen import agent_loop
from api.services.workflow_gen.toolbox import WorkflowGenToolboxError


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict[str, Any]):
        self.id = call_id
        self.function = _FakeFunction(name, json.dumps(arguments))


class _FakeMessage:
    def __init__(self, content: str | None = None, tool_calls: list[_FakeToolCall] | None = None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]
        if exclude_none:
            payload = {k: v for k, v in payload.items() if v is not None}
        return payload


class _FakeCompletion:
    def __init__(self, message: _FakeMessage):
        self.choices = [type("Choice", (), {"message": message})()]


def _tool_call_completion(call_id: str, tool_definition: dict[str, Any]) -> _FakeCompletion:
    return _FakeCompletion(
        _FakeMessage(
            tool_calls=[_FakeToolCall(call_id, "create_tool", {"tool_definition": tool_definition})]
        )
    )


def _assert_every_tool_call_answered(messages: list[dict[str, Any]]) -> None:
    """The invariant OpenAI enforces with a 400: every assistant `tool_calls`
    entry needs a matching `tool` reply. Breaking it doesn't just fail one
    turn — the transcript is persisted, so every later turn replays it and
    fails identically, wedging the thread permanently."""
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            assert call["id"] in answered, (
                f"tool_call {call['id']} ({call['function']['name']}) was left unanswered — "
                "this is exactly what triggers the OpenAI 400"
            )


def _pending_action(tool_definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": "action-1",
        "tool_call_id": "call-1",
        "action_type": "create_tool",
        "arguments": {"tool_definition": tool_definition},
        "sibling_call_ids": [],
    }


def _install_fakes(monkeypatch, *, create_tool_side_effects: list[Any], completions: list[Any]):
    """Wire a fake toolbox + LLM into the agent loop.

    `create_tool_side_effects` is consumed one per attempt: an exception is
    raised, anything else is returned. `completions` is consumed one per
    `llm_client.complete` call."""
    attempts: list[dict[str, Any]] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def create_tool(self, tool_definition: dict[str, Any]) -> dict[str, Any]:
            attempts.append(tool_definition)
            outcome = create_tool_side_effects.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    async def _fake_complete(messages, tools, **kwargs):
        return completions.pop(0)

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)
    return attempts


async def _collect(pending_action: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=[],
            pending_action=pending_action,
            approve=True,
        )
    ]


@pytest.mark.asyncio
async def test_create_tool_repairs_a_bad_definition_without_showing_the_user_an_error(monkeypatch):
    attempts = _install_fakes(
        monkeypatch,
        create_tool_side_effects=[
            WorkflowGenToolboxError(
                "The tool definition didn't match the expected shape.",
                errors=["definition: Field required"],
            ),
            {"tool_uuid": "tool-uuid-1", "name": "Lookup", "status": "active"},
        ],
        completions=[
            _tool_call_completion("call-2", {"name": "Lookup", "definition": {"category": "calculator"}}),
            _FakeCompletion(_FakeMessage(content="Created the Lookup tool.")),
        ],
    )

    events = await _collect(_pending_action({"name": "Lookup"}))

    assert len(attempts) == 2, "the model should get a second attempt after a bad definition"
    assert [e for e in events if e["type"] == "error"] == [], (
        "a repairable definition error must not surface to the user"
    )
    # The repair happens inside the already-approved action — the user is not
    # asked to confirm the corrected definition a second time.
    assert [e for e in events if e["type"] == "approval"] == []


@pytest.mark.asyncio
async def test_create_tool_gives_up_after_max_repair_attempts_with_one_error(monkeypatch):
    failure = WorkflowGenToolboxError("bad shape", errors=["definition: Field required"])
    attempts = _install_fakes(
        monkeypatch,
        create_tool_side_effects=[failure, failure, failure],
        completions=[
            _tool_call_completion("call-2", {"name": "Lookup"}),
            _tool_call_completion("call-3", {"name": "Lookup"}),
            _FakeCompletion(_FakeMessage(content="I couldn't create that tool.")),
        ],
    )

    events = await _collect(_pending_action({"name": "Lookup"}))

    assert len(attempts) == agent_loop.MAX_REPAIR_ATTEMPTS
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1, "exactly one user-visible error after exhausting repairs"
    assert "definition: Field required" in errors[0]["data"]["message"]


@pytest.mark.asyncio
async def test_credential_secret_never_reaches_the_approval_card(monkeypatch):
    """The approval payload is persisted and re-sent to the browser every
    time the thread reopens, so it must carry a masked secret — while the
    real value stays in `arguments`, which is what actually executes."""
    secret = "fake-test-key-Ab3!xY9$Qz7#2026"

    completions = [
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall(
                        "call-cred",
                        "create_credential",
                        {
                            "name": "Solis CRM API key",
                            "credential_type": "api_key",
                            "credential_data": {
                                "header_name": "Authorization",
                                "api_key": secret,
                            },
                        },
                    )
                ]
            )
        )
    ]

    async def _fake_complete(messages, tools, **kwargs):
        return completions.pop(0)

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)

    steps = [
        step
        async for step in agent_loop.run_turn(
            organization_id=1,
            user_id=1,
            prior_messages=[],
            user_message="add this tool with my api key",
        )
    ]

    approval = next(s for s in steps if s.event["type"] == "approval")
    shown = json.dumps(approval.event["data"])
    assert secret not in shown, "the raw secret must never be sent to the browser"
    assert "2026" in shown, "a masked tail should still be shown for recognition"

    # …but the executable copy is untouched, or the credential would be wrong.
    assert approval.pending_action["arguments"]["credential_data"]["api_key"] == secret
    assert secret not in json.dumps(approval.pending_action["preview"])


@pytest.mark.asyncio
async def test_model_looking_something_up_mid_repair_does_not_break_the_transcript(monkeypatch):
    """Told not to inline a secret, the model answers a rejected `create_tool`
    by calling `list_credentials` instead of resubmitting. That lookup must be
    served and answered, and it must not be mistaken for giving up."""
    create_attempts: list[dict[str, Any]] = []
    lookups: list[str] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def list_credentials(self) -> list[dict[str, Any]]:
            lookups.append("list_credentials")
            return [{"credential_uuid": "cred-1", "name": "Solis CRM"}]

        async def create_tool(self, tool_definition: dict[str, Any]) -> dict[str, Any]:
            create_attempts.append(tool_definition)
            if len(create_attempts) == 1:
                raise WorkflowGenToolboxError(
                    "Don't inline secrets.", errors=["definition.headers: use credential_uuid instead"]
                )
            return {"tool_uuid": "tool-1", "name": "TractorModelMaster", "status": "active"}

    completions = [
        # First repair hop: looks up credentials rather than resubmitting.
        _FakeCompletion(
            _FakeMessage(tool_calls=[_FakeToolCall("call-look", "list_credentials", {})])
        ),
        # Second hop: now able to resubmit correctly.
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall(
                        "call-2",
                        "create_tool",
                        {"tool_definition": {"name": "TractorModelMaster", "credential_uuid": "cred-1"}},
                    )
                ]
            )
        ),
        _FakeCompletion(_FakeMessage(content="Added the tool to node 2.")),
    ]

    async def _fake_complete(messages, tools, **kwargs):
        # Every call into the model must see a well-formed transcript.
        _assert_every_tool_call_answered(messages)
        return completions.pop(0)

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)

    steps = [
        step
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=[],
            pending_action=_pending_action({"name": "TractorModelMaster"}),
            approve=True,
        )
    ]

    assert lookups == ["list_credentials"], "the model's lookup should actually be served"
    assert len(create_attempts) == 2, "it should still get to resubmit after looking up"
    assert [s.event for s in steps if s.event["type"] == "error"] == []
    _assert_every_tool_call_answered(steps[-1].messages)


@pytest.mark.asyncio
async def test_model_that_never_resubmits_still_leaves_a_valid_transcript(monkeypatch):
    """Even when repair genuinely fails, the saved transcript must stay
    replayable — otherwise the thread is dead for every future message."""

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def list_credentials(self) -> list[dict[str, Any]]:
            return []

        async def create_tool(self, tool_definition: dict[str, Any]) -> dict[str, Any]:
            raise WorkflowGenToolboxError("bad", errors=["definition: Field required"])

    def _lookup_completion():
        return _FakeCompletion(
            _FakeMessage(tool_calls=[_FakeToolCall(f"look-{uuid4().hex[:6]}", "list_credentials", {})])
        )

    async def _fake_complete(messages, tools, **kwargs):
        _assert_every_tool_call_answered(messages)
        return _lookup_completion() if len(messages) < 40 else _FakeCompletion(
            _FakeMessage(content="I couldn't build that tool.")
        )

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)

    steps = [
        step
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=[],
            pending_action=_pending_action({"name": "Broken"}),
            approve=True,
        )
    ]

    _assert_every_tool_call_answered(steps[-1].messages)
    assert len([s for s in steps if s.event["type"] == "error"]) == 1


@pytest.mark.asyncio
async def test_a_transcript_already_saved_broken_is_repaired_on_the_next_turn(monkeypatch):
    """Sessions poisoned by an earlier bug must heal, not stay dead."""
    poisoned = [
        {"role": "user", "content": "add this tool at node 2"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_orphaned",
                    "type": "function",
                    "function": {"name": "create_tool", "arguments": "{}"},
                }
            ],
        },
    ]

    async def _fake_complete(messages, tools, **kwargs):
        _assert_every_tool_call_answered(messages)
        return _FakeCompletion(_FakeMessage(content="Let's try that again."))

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)

    steps = [
        step
        async for step in agent_loop.run_turn(
            organization_id=1, user_id=1, prior_messages=poisoned, user_message="try again"
        )
    ]

    assert [s.event for s in steps if s.event["type"] == "error"] == []
    _assert_every_tool_call_answered(steps[-1].messages)


@pytest.mark.asyncio
async def test_workflow_save_rejected_by_the_shared_tool_is_repaired_silently(monkeypatch):
    """`save_workflow` delegates to the shared MCP tool, which reports
    failure as `{"saved": False, error_code, error}` data rather than
    raising. That must feed the repair loop, not the user's screen."""
    attempts: list[str] = []

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

        async def save_workflow(self, workflow_id: int, code: str) -> dict[str, Any]:
            attempts.append(code)
            if len(attempts) == 1:
                return {
                    "saved": False,
                    "error_code": "graph_validation",
                    "error": "Workflow must have exactly one start node (line 4, col 11)",
                }
            return {
                "saved": True,
                "workflow_id": 7,
                "name": "Support Flow",
                "node_count": 3,
                "edge_count": 2,
                "version_number": 2,
                "status": "draft",
            }

    completions = [
        _FakeCompletion(
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall("call-2", "save_workflow", {"workflow_id": 7, "code": "fixed"})
                ]
            )
        ),
        _FakeCompletion(_FakeMessage(content="Saved as a draft.")),
    ]

    async def _fake_complete(messages, tools, **kwargs):
        return completions.pop(0)

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)

    events = [
        step.event
        async for step in agent_loop.execute_confirmed_action(
            organization_id=1,
            user_id=1,
            prior_messages=[],
            pending_action={
                "action_id": "action-1",
                "tool_call_id": "call-1",
                "action_type": "save_workflow",
                "arguments": {"workflow_id": 7, "code": "broken"},
                "sibling_call_ids": [],
            },
            approve=True,
        )
    ]

    assert attempts == ["broken", "fixed"]
    assert [e for e in events if e["type"] == "error"] == []
    ready = [e for e in events if e["type"] == "workflow_ready"]
    assert len(ready) == 1
    # MCP keys the id as `workflow_id`; the SSE contract uses `workflow_id`
    # in its payload, sourced from the normalized `id`.
    assert ready[0]["data"]["workflow_id"] == 7
    assert ready[0]["data"]["node_count"] == 3


@pytest.mark.asyncio
async def test_unparseable_source_never_reaches_an_approval_card(monkeypatch):
    """Proposed TypeScript is parsed before the approval is shown, so the
    user is never asked to approve source that can't even compile."""
    parse_calls: list[str] = []

    async def _fake_parse(code: str):
        parse_calls.append(code)
        if len(parse_calls) == 1:
            return {"ok": False, "stage": "parse", "errors": [{"message": "Unexpected token", "line": 3, "column": 9}]}
        return {
            "ok": True,
            "workflowName": "Dental Receptionist",
            "workflow": {"nodes": [{}, {}], "edges": [{}]},
        }

    completions = [
        _FakeCompletion(
            _FakeMessage(tool_calls=[_FakeToolCall("call-1", "create_workflow", {"code": "broken"})])
        ),
        _FakeCompletion(
            _FakeMessage(tool_calls=[_FakeToolCall("call-2", "create_workflow", {"code": "fixed"})])
        ),
    ]

    async def _fake_complete(messages, tools, **kwargs):
        return completions.pop(0)

    class _FakeToolbox:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(agent_loop, "WorkflowGenToolbox", _FakeToolbox)
    monkeypatch.setattr(agent_loop.llm_client, "complete", _fake_complete)
    monkeypatch.setattr(agent_loop, "parse_code", _fake_parse)

    steps = [
        step
        async for step in agent_loop.run_turn(
            organization_id=1, user_id=1, prior_messages=[], user_message="build it"
        )
    ]

    approvals = [s.event for s in steps if s.event["type"] == "approval"]
    assert parse_calls == ["broken", "fixed"], "the bad source should be re-parsed after repair"
    assert len(approvals) == 1, "only the parseable source should reach an approval"
    # The card shows real counts from the parsed source, not a code blob.
    assert approvals[0]["data"]["summary"] == 'Ready to create "Dental Receptionist" — 2 node(s), 1 edge(s).'


@pytest.mark.asyncio
async def test_create_tool_failure_from_tool_management_is_also_repairable(monkeypatch):
    """A duplicate name or bad credential reference comes back as a plain
    message with no structured `errors` — it must still reach the model as a
    repair prompt rather than a dead end, since renaming fixes it."""
    attempts = _install_fakes(
        monkeypatch,
        create_tool_side_effects=[
            WorkflowGenToolboxError("A tool named 'Lookup' already exists."),
            {"tool_uuid": "tool-uuid-2", "name": "Lookup v2", "status": "active"},
        ],
        completions=[
            _tool_call_completion("call-2", {"name": "Lookup v2"}),
            _FakeCompletion(_FakeMessage(content="Created it as Lookup v2.")),
        ],
    )

    events = await _collect(_pending_action({"name": "Lookup"}))

    assert len(attempts) == 2
    assert [e for e in events if e["type"] == "error"] == []
