"""The agent loop: LLM (Azure OpenAI) <-> WorkflowGenToolbox.

An async generator, not a request/response function — each step yields an
SSE-shaped event (`status` | `assistant` | `approval` | `workflow_ready` |
`error` | `done`, the frozen contract from the redesign plan) plus the full
running message-transcript snapshot, so the caller (`session_service.py`)
can persist state at every step — critically, `pending_action` is part of
the same yielded snapshot as the `approval` event, so the caller persists it
*before* forwarding the SSE frame to the client. That's what makes approval
state durable across a worker restart or a dropped connection, not just an
in-memory thing living inside this generator's stack frame.

Workflow authoring runs through the same MCP tool implementations external
clients use (see `toolbox.py`): the model authors SDK TypeScript, which is
parsed and validated by the Node bridge before anything is persisted. Every
mutating tool — `create_workflow`, `save_workflow`, `create_tool` — shares
one bounded self-repair path, so a model mistake is corrected in place
rather than surfaced to the user.
"""

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

from loguru import logger

from api.mcp_server.ts_bridge import TsBridgeError, parse_code
from api.services.configuration.masking import mask_key
from api.services.workflow_gen import llm_client
from api.services.workflow_gen.tool_schemas import (
    MUTATING_TOOLS,
    SECRET_ARGUMENT_KEYS,
    TOOL_SCHEMAS,
)
from api.services.workflow_gen.toolbox import WorkflowGenToolbox, WorkflowGenToolboxError

MAX_TOOL_ITERATIONS = 10
MAX_REPAIR_ATTEMPTS = 3
# Read-only lookups the model may make *while* repairing a rejected payload
# (e.g. `list_credentials` after being told not to inline a secret) before it
# resubmits. Bounded so a model that never resubmits can't loop forever.
MAX_REPAIR_TOOL_HOPS = 3

_WORKFLOW_SOURCE_TOOLS = ("create_workflow", "save_workflow")

_STATUS_BEFORE_TOOL = {
    "list_node_types": "Checking available node types…",
    "get_node_type": "Checking available node types…",
    "list_workflows": "Looking up existing workflows…",
    "get_workflow": "Looking up existing workflows…",
    "get_voice_prompting_guide": "Reviewing prompting guidance…",
    "search_docs": "Searching the docs…",
    "read_doc": "Searching the docs…",
    "list_docs": "Searching the docs…",
    "list_tools": "Checking available tools…",
    "list_documents": "Checking available documents…",
    "list_credentials": "Checking available credentials…",
    "list_recordings": "Checking available recordings…",
}


@dataclass
class LoopStep:
    event: dict[str, Any]
    messages: list[dict[str, Any]]
    pending_action: dict[str, Any] | None = None
    workflow_id: int | None = None


def _status(message: str) -> dict[str, Any]:
    return {"type": "status", "data": {"message": message}}


def _assistant(message: str) -> dict[str, Any]:
    return {"type": "assistant", "data": {"message": message}}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "data": {"code": code, "message": message}}


def _done() -> dict[str, Any]:
    return {"type": "done", "data": {}}


def _message_to_dict(message: Any) -> dict[str, Any]:
    return message.model_dump(exclude_none=True)


def _workflow_ready(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "workflow_ready",
        "data": {
            "workflow_id": result["id"],
            "name": result["name"],
            "node_count": result.get("node_count", 0),
            "edge_count": result.get("edge_count", 0),
            "valid": True,
            "url": f"/workflow/{result['id']}",
        },
    }


async def _dispatch_safe_tool(toolbox: WorkflowGenToolbox, name: str, arguments: dict[str, Any]) -> Any:
    """Call a read-only toolbox method; never raises — errors come back as a
    tool-result string so the LLM can react to them."""
    method = getattr(toolbox, name, None)
    if method is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return await method(**arguments)
    except WorkflowGenToolboxError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"workflow_gen tool call '{name}' failed: {e}")
        return {"error": str(e)}


def _parse_arguments(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return json.loads(raw or "{}"), None
    except json.JSONDecodeError as e:
        return None, f"Malformed tool arguments: {e}"


def _answer_dangling_tool_calls(messages: list[dict[str, Any]], reason: str) -> None:
    """Guarantee every assistant `tool_calls` entry has a matching tool reply.

    OpenAI rejects a transcript where one doesn't, with HTTP 400 — and that
    rejection is self-perpetuating, because the bad transcript is persisted
    and every later turn replays it and fails identically, wedging the thread
    for good. Any single missed path anywhere in this module would cause
    that, so the invariant is enforced here rather than trusted.

    Replies are inserted directly after the assistant message that made the
    call, not appended at the end, since the API expects them to follow it.
    """
    answered = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    rebuilt: list[dict[str, Any]] = []
    for message in messages:
        rebuilt.append(message)
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if call_id and call_id not in answered:
                answered.add(call_id)
                rebuilt.append(
                    {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"skipped": reason})}
                )
    messages[:] = rebuilt


def _mask_secret_arguments(value: Any) -> Any:
    """Redact secret values anywhere in a tool-argument tree.

    The approval card's payload is persisted with the session and re-sent to
    the browser every time the thread is reopened, so a raw API key must not
    travel inside it. The unmasked arguments stay in `pending_action`, which
    is what actually executes on confirm — this only affects what's shown.
    """
    if isinstance(value, dict):
        return {
            key: (
                mask_key(item)
                if key in SECRET_ARGUMENT_KEYS and isinstance(item, str)
                else _mask_secret_arguments(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_secret_arguments(item) for item in value]
    return value


def _describe_ts_error(err: dict[str, Any]) -> str:
    line, col = err.get("line"), err.get("column")
    where = ""
    if line is not None:
        where = f" (line {line}" + (f", col {col}" if col is not None else "") + ")"
    return f"{err.get('message', 'unknown error')}{where}"


def _mcp_result_errors(result: dict[str, Any]) -> list[str]:
    """Flatten an MCP tool's structured failure into repair lines.

    The tools return `{created|saved: False, error_code, error}` rather than
    raising, so a failure is data the model can act on — the `error_code`
    prefix tells it what kind of fix is needed."""
    code = result.get("error_code", "error")
    message = (result.get("error") or "The action failed.").strip()
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return [f"[{code}] {line}" for line in lines] or [f"[{code}] The action failed."]


def _normalize_workflow_result(result: dict[str, Any]) -> dict[str, Any]:
    """MCP keys the workflow as `workflow_id`; the SSE contract and persisted
    session state key it as `id`."""
    return {**result, "id": result["workflow_id"]}


async def _preview_workflow_source(code: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse proposed TypeScript *before* the approval card is shown.

    Two reasons not to wait until the user confirms: they shouldn't be asked
    to approve source that cannot even parse, and the card needs real
    node/edge counts to show rather than a wall of code. Returns
    `(preview, errors)` — exactly one side is populated.
    """
    try:
        parsed = await parse_code(code)
    except TsBridgeError as e:
        return None, [f"[bridge_error] {e}"]

    if not parsed.get("ok"):
        code_key = "parse_error" if parsed.get("stage", "parse") == "parse" else "validation_error"
        described = [_describe_ts_error(e) for e in (parsed.get("errors") or [])]
        return None, [f"[{code_key}] {d}" for d in described] or [
            f"[{code_key}] The source could not be parsed."
        ]

    workflow = parsed.get("workflow") or {}
    return {
        "name": (parsed.get("workflowName") or "").strip(),
        "node_count": len(workflow.get("nodes", [])),
        "edge_count": len(workflow.get("edges", [])),
    }, []


async def _run_loop(
    messages: list[dict[str, Any]],
    *,
    toolbox: WorkflowGenToolbox,
) -> AsyncIterator[LoopStep]:
    # Covers both a bug anywhere above and a transcript already saved in a
    # broken state by an earlier version — without this, such a thread stays
    # permanently unusable because every turn replays the same bad history.
    _answer_dangling_tool_calls(messages, "This step was interrupted and did not complete.")

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            completion = await llm_client.complete(messages, TOOL_SCHEMAS)
        except llm_client.WorkflowGenLLMError as e:
            messages.append({"role": "assistant", "content": f"I hit a problem talking to the model: {e}"})
            yield LoopStep(_error("llm_failure", str(e)), list(messages))
            yield LoopStep(_done(), list(messages))
            return

        choice_message = completion.choices[0].message
        messages.append(_message_to_dict(choice_message))

        tool_calls = choice_message.tool_calls or []
        if not tool_calls:
            if choice_message.content:
                yield LoopStep(_assistant(choice_message.content), list(messages))
            yield LoopStep(_done(), list(messages))
            return

        mutating_calls = [c for c in tool_calls if c.function.name in MUTATING_TOOLS]
        if mutating_calls:
            call = mutating_calls[0]
            arguments, parse_error = _parse_arguments(call.function.arguments)
            if parse_error:
                # Every tool_call in this assistant message needs a response
                # before the next completion call, not just the broken one.
                for c in tool_calls:
                    content = {"error": parse_error} if c.id == call.id else {"skipped": "A sibling tool call had malformed arguments."}
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(content)})
                continue

            preview: dict[str, Any] | None = None
            if call.function.name in _WORKFLOW_SOURCE_TOOLS:
                yield LoopStep(_status("Checking the proposed changes…"), list(messages))
                preview, preview_errors = await _preview_workflow_source(
                    arguments.get("code", "")
                )
                if preview_errors:
                    # Don't put unparseable source in front of the user —
                    # hand the errors back and let the model fix them first.
                    for c in tool_calls:
                        content = (
                            {"errors": preview_errors, "please_fix_and_resubmit": True}
                            if c.id == call.id
                            else {"skipped": "A sibling tool call needs fixing first."}
                        )
                        messages.append(
                            {"role": "tool", "tool_call_id": c.id, "content": json.dumps(content)}
                        )
                    continue

            action_id = uuid4().hex
            # What the user is shown, with any secrets redacted. Kept beside
            # `arguments` (which stays intact, since it's what executes) so a
            # reopened thread can re-render the card without the raw secret.
            display_preview = {**_mask_secret_arguments(arguments), **(preview or {})}
            pending_action = {
                "action_id": action_id,
                "tool_call_id": call.id,
                "action_type": call.function.name,
                "arguments": arguments,
                "preview": display_preview,
                # Every other tool_call in this same assistant turn (if any)
                # still needs a response once resolved — carried along so
                # execute_confirmed_action can satisfy the whole batch.
                "sibling_call_ids": [c.id for c in tool_calls if c.id != call.id],
            }
            summary = _summarize_mutating_call(call.function.name, arguments, preview)
            yield LoopStep(
                {
                    "type": "approval",
                    "data": {
                        "action_id": action_id,
                        "action_type": call.function.name,
                        "summary": summary,
                        "definition_preview": display_preview,
                    },
                },
                list(messages),
                pending_action=pending_action,
            )
            return

        for call in tool_calls:
            status_msg = _STATUS_BEFORE_TOOL.get(call.function.name)
            if status_msg:
                yield LoopStep(_status(status_msg), list(messages))
            arguments, parse_error = _parse_arguments(call.function.arguments)
            result: Any = {"error": parse_error} if parse_error else await _dispatch_safe_tool(
                toolbox, call.function.name, arguments
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)}
            )

    logger.warning("workflow_gen agent loop hit MAX_TOOL_ITERATIONS without finishing")
    messages.append(
        {
            "role": "assistant",
            "content": (
                "I've made several tool calls but haven't finished yet. "
                "Here's where things stand — let me know how you'd like to proceed."
            ),
        }
    )
    yield LoopStep(_assistant(messages[-1]["content"]), list(messages))
    yield LoopStep(_done(), list(messages))


def _summarize_mutating_call(
    tool_name: str, arguments: dict[str, Any], preview: dict[str, Any] | None = None
) -> str:
    """`preview` comes from parsing the proposed source, so the counts are
    what will actually be built — not a guess from the raw arguments."""
    shape = preview or {}
    counts = f"{shape.get('node_count', 0)} node(s), {shape.get('edge_count', 0)} edge(s)"
    if tool_name == "create_workflow":
        return f'Ready to create "{shape.get("name") or "a new workflow"}" — {counts}.'
    if tool_name == "save_workflow":
        return f"Ready to save changes as a draft — {counts}."
    if tool_name == "create_tool":
        name = (arguments.get("tool_definition") or {}).get("name")
        return f'Ready to create the tool "{name}".' if name else "Ready to create a new reusable tool."
    if tool_name == "update_tool":
        name = arguments.get("name")
        return f'Ready to update the tool "{name}".' if name else "Ready to update an existing tool."
    if tool_name == "test_tool":
        return "Ready to send a real test request to this tool's endpoint."
    if tool_name == "create_credential":
        name = arguments.get("name") or "a new credential"
        return f'Ready to securely store "{name}" — the secret is saved once and referenced by tools, never shown again.'
    return f"Ready to run {tool_name}."


def _system_prompt(workflow_id: int | None) -> str:
    """The base prompt, plus which workflow this session is attached to.

    Without this the assistant opens inside a workflow's editor and still
    asks which workflow to change — it can list them, but has no idea which
    one the user is looking at.
    """
    from api.services.workflow_gen.system_prompt import WORKFLOW_GEN_SYSTEM_PROMPT

    if workflow_id is None:
        return WORKFLOW_GEN_SYSTEM_PROMPT
    return (
        WORKFLOW_GEN_SYSTEM_PROMPT
        + f"""
## The workflow you are working on

This conversation is open inside workflow **{workflow_id}**'s editor. The user \
is looking at it right now, so "this workflow", "the flow", "this agent", or a \
bare node name always means workflow {workflow_id}.

Never ask which workflow to change, and never list workflows to choose from — \
you already know. Call `get_workflow_code({workflow_id})` to see its current \
state, including the real node names, instead of asking the user to confirm \
them. Only touch a different workflow if the user names one explicitly.
"""
    )


async def run_turn(
    *,
    organization_id: int,
    user_id: int | None,
    prior_messages: list[dict[str, Any]],
    user_message: str,
    workflow_id: int | None = None,
) -> AsyncIterator[LoopStep]:
    toolbox = WorkflowGenToolbox(organization_id, user_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(workflow_id)},
        *prior_messages,
        {"role": "user", "content": user_message},
    ]
    async for step in _run_loop(messages, toolbox=toolbox):
        step.messages = step.messages[1:]  # drop the leading system message before persisting
        yield step


async def _persist_mutating_action(
    toolbox: WorkflowGenToolbox,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    attempt_number: int,
) -> AsyncIterator[dict[str, Any]]:
    """One validate-then-persist attempt for create_workflow/save_workflow
    (or a direct persist for create_tool, which has no JSON-shape validation
    step of its own — CreateToolRequest already validates it).

    The bounded repair loop itself lives in `execute_confirmed_action`, which
    calls this once per attempt with a fresh (LLM-corrected) `arguments` each
    time — this function does not retry internally.
    """
    if tool_name == "create_credential":
        yield {"event": _status("Storing the credential…")}
        try:
            result = await toolbox.create_credential(
                name=arguments.get("name", ""),
                credential_type=arguments.get("credential_type", ""),
                credential_data=arguments.get("credential_data", {}),
                description=arguments.get("description"),
            )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("Credential stored."),
            "result": {"kind": "create_credential", **result},
        }
        return

    if tool_name == "test_tool":
        yield {"event": _status("Calling the endpoint…")}
        try:
            result = await toolbox.test_tool(
                tool_uuid=arguments.get("tool_uuid", ""),
                llm_params=arguments.get("llm_params"),
                preset_params=arguments.get("preset_params"),
            )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        # A non-2xx is a real answer about the tool's configuration, not a
        # failure of this action — hand it back so the model can diagnose it.
        outcome = "responded" if result.get("status") == "success" else "returned an error"
        yield {
            "event": _status(f"Endpoint {outcome}."),
            "result": {"kind": "test_tool", **result},
        }
        return

    if tool_name == "update_tool":
        yield {
            "event": _status(
                "Updating the tool…"
                if attempt_number == 0
                else f"Fixing the tool definition (attempt {attempt_number + 1} of {MAX_REPAIR_ATTEMPTS})…"
            )
        }
        try:
            result = await toolbox.update_tool(
                tool_uuid=arguments.get("tool_uuid", ""),
                tool_definition=arguments.get("tool_definition"),
                name=arguments.get("name"),
                description=arguments.get("description"),
            )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {"event": _status("Tool updated."), "result": {"kind": "update_tool", **result}}
        return

    if tool_name == "create_tool":
        yield {
            "event": _status(
                "Creating the tool…"
                if attempt_number == 0
                else f"Fixing the tool definition (attempt {attempt_number + 1} of {MAX_REPAIR_ATTEMPTS})…"
            )
        }
        try:
            result = await toolbox.create_tool(arguments.get("tool_definition", {}))
        except WorkflowGenToolboxError as e:
            # Repairable, not terminal: a bad field shape, a duplicate name or
            # a bad credential reference are all things the model can correct
            # from the error text. Routing this through the `errors` channel
            # (rather than yielding a user-visible `error` event) hands it to
            # the same bounded repair loop create_workflow/save_workflow use.
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {"event": _status("Tool created."), "result": {"kind": "create_tool", **result}}
        return

    code = arguments.get("code", "")
    workflow_id = arguments.get("workflow_id")

    yield {
        "event": _status(
            "Saving the workflow…"
            if attempt_number == 0
            else f"Fixing the reported issues (attempt {attempt_number + 1} of {MAX_REPAIR_ATTEMPTS})…"
        )
    }
    try:
        if tool_name == "create_workflow":
            result = await toolbox.create_workflow(code)
        else:
            result = await toolbox.save_workflow(workflow_id, code)
    except WorkflowGenToolboxError as e:
        yield {"event": None, "result": None, "errors": e.errors}
        return

    # The shared MCP tools report failure as data, not exceptions — any
    # failure here is something the model can resubmit a fix for.
    if not (result.get("created") or result.get("saved")):
        yield {"event": None, "result": None, "errors": _mcp_result_errors(result)}
        return

    yield {"event": None, "result": {"kind": tool_name, **_normalize_workflow_result(result)}}


async def execute_confirmed_action(
    *,
    organization_id: int,
    user_id: int | None,
    prior_messages: list[dict[str, Any]],
    pending_action: dict[str, Any],
    approve: bool,
    workflow_id: int | None = None,
) -> AsyncIterator[LoopStep]:
    toolbox = WorkflowGenToolbox(organization_id, user_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(workflow_id)},
        *prior_messages,
    ]
    tool_call_id = pending_action["tool_call_id"]
    tool_name = pending_action["action_type"]
    arguments = pending_action["arguments"]

    def _respond_siblings(reason: str) -> None:
        for sibling_id in pending_action.get("sibling_call_ids", []):
            messages.append(
                {"role": "tool", "tool_call_id": sibling_id, "content": json.dumps({"skipped": reason})}
            )

    def _step(event: dict[str, Any], *, workflow_id: int | None = None) -> LoopStep:
        # `messages[0]` is always the system prompt injected at the top of
        # this function — strip it before it reaches the caller, matching
        # what `_run_loop`-sourced yields already do.
        return LoopStep(event, list(messages)[1:], workflow_id=workflow_id)

    if not approve:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps({"declined": True, "message": "The user reviewed this and chose not to proceed."}),
            }
        )
        _respond_siblings("A related action was declined by the user.")
        async for step in _run_loop(messages, toolbox=toolbox):
            step.messages = step.messages[1:]
            yield step
        return

    built_workflow_id: int | None = None
    succeeded = False
    last_errors: list[str] = []
    # Whether `tool_call_id`'s current value already has a `tool`-role
    # response appended to `messages`. Every branch below must leave this
    # True before the next `llm_client.complete()` call — Azure/OpenAI
    # rejects a transcript with an assistant tool_call left unanswered.
    primary_responded = False
    # Whether a branch already yielded a specific `error` event for this
    # failure (tool_failure/llm_failure) — avoids also yielding a generic
    # `validation_failed` for the same failure at the bottom.
    error_already_yielded = False

    for attempt_number in range(MAX_REPAIR_ATTEMPTS):
        outcome: dict[str, Any] | None = None
        hard_failure_message: str | None = None
        async for event in _persist_mutating_action(
            toolbox, tool_name, arguments, attempt_number=attempt_number
        ):
            if event.get("event") is not None:
                yield _step(event["event"])
                if event["event"]["type"] == "error":
                    hard_failure_message = event["event"]["data"]["message"]
            if "result" in event:
                outcome = event

        if outcome is not None and outcome.get("result") is not None:
            result = outcome["result"]
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(result, default=str)})
            primary_responded = True
            _respond_siblings("Handled as part of the confirmed action.")
            if result.get("kind") in ("create_workflow", "save_workflow"):
                built_workflow_id = result["id"]
                yield _step(_workflow_ready(result), workflow_id=built_workflow_id)
            succeeded = True
            break

        if outcome is not None and "errors" in outcome:
            last_errors = outcome["errors"] or []
            if attempt_number == MAX_REPAIR_ATTEMPTS - 1:
                break  # exhausted — fall through to the terminal-failure handling below

            # Re-prompt the LLM with the validation errors so it can repair
            # nodes/edges, then re-extract its corrected create_workflow/
            # save_workflow call for the next attempt.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"validation_errors": last_errors, "please_fix_and_resubmit": True}),
                }
            )
            primary_responded = True

            # The model often can't fix the payload without looking something
            # up first (told not to inline a secret, it goes to
            # `list_credentials`). Serve those read-only calls and ask again
            # instead of reading "hasn't resubmitted yet" as giving up — and
            # answer every call it makes, since one left dangling breaks the
            # next completion and poisons the saved transcript.
            repaired = None
            llm_failed = False
            for _ in range(MAX_REPAIR_TOOL_HOPS):
                try:
                    completion = await llm_client.complete(messages, TOOL_SCHEMAS)
                except llm_client.WorkflowGenLLMError as e:
                    yield _step(_error("llm_failure", str(e)))
                    error_already_yielded = True
                    llm_failed = True
                    break
                choice_message = completion.choices[0].message
                messages.append(_message_to_dict(choice_message))
                calls = list(choice_message.tool_calls or [])
                repaired = next((c for c in calls if c.function.name == tool_name), None)
                lookups = [c for c in calls if c is not repaired]
                for lookup in lookups:
                    status_msg = _STATUS_BEFORE_TOOL.get(lookup.function.name)
                    if status_msg:
                        yield _step(_status(status_msg))
                    lookup_args, lookup_err = _parse_arguments(lookup.function.arguments)
                    lookup_result = (
                        {"error": lookup_err}
                        if lookup_err
                        else await _dispatch_safe_tool(toolbox, lookup.function.name, lookup_args)
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": lookup.id,
                            "content": json.dumps(lookup_result, default=str),
                        }
                    )
                if repaired is not None or not lookups:
                    break

            if llm_failed:
                break
            if repaired is None:
                last_errors = ["The model did not resubmit a corrected definition."]
                break
            arguments, parse_error = _parse_arguments(repaired.function.arguments)
            if parse_error:
                # `repaired` is itself an unanswered tool call — settle it
                # before this transcript reaches another completion.
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": repaired.id,
                        "content": json.dumps({"error": parse_error}),
                    }
                )
                last_errors = [parse_error]
                break
            tool_call_id = repaired.id
            primary_responded = False  # a fresh, not-yet-answered tool_call_id from the repair turn
            continue

        # A hard tool failure (WorkflowGenToolboxError raised by create_tool/
        # create_workflow/save_workflow itself, not a validation-shape
        # error) — its `error` event was already yielded above, but that's
        # just an SSE frame, not a transcript entry. Still must answer
        # tool_call_id here or the next completion call violates the
        # OpenAI/Azure tool-call contract.
        last_errors = [hard_failure_message] if hard_failure_message else ["The action failed."]
        messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps({"error": last_errors[0]})}
        )
        primary_responded = True
        error_already_yielded = True
        break

    if not succeeded:
        _respond_siblings("The primary action failed.")
        if not primary_responded:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"validation_errors": last_errors, "gave_up": True}),
                }
            )
        if last_errors and not error_already_yielded:
            yield _step(_error("validation_failed", "; ".join(filter(None, last_errors))))

    async for step in _run_loop(messages, toolbox=toolbox):
        step.messages = step.messages[1:]
        if built_workflow_id and step.workflow_id is None:
            step.workflow_id = built_workflow_id
        yield step
