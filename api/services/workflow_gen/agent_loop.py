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
from api.services.workflow_gen.transcript import (
    compact_tool_result,
    result_cap_for,
    sanitize_transcript,
    trim_to_budget,
)

MAX_TOOL_ITERATIONS = 10
MAX_REPAIR_ATTEMPTS = 3
# Read-only lookups the model may make *while* repairing a rejected payload
# (e.g. `list_credentials` after being told not to inline a secret) before it
# resubmits. Bounded so a model that never resubmits can't loop forever.
MAX_REPAIR_TOOL_HOPS = 3

_WORKFLOW_SOURCE_TOOLS = ("create_workflow", "save_workflow")

# Tools whose arguments carry a chunk of a document. Their approval card is
# built from sizes, never the text — see where shown_arguments is assembled.
_REPLACEMENT_TOOLS = ("replace_in_node", "replace_in_code_file")

_STATUS_BEFORE_TOOL = {
    "list_node_types": "Checking available node types…",
    "get_node_type": "Checking available node types…",
    "list_workflows": "Looking up existing workflows…",
    "get_workflow": "Looking up existing workflows…",
    "list_nodes": "Reading the agent's nodes…",
    "get_node": "Reading that node…",
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


def _tool_message(
    tool_call_id: str, payload: Any, *, tool_name: str | None = None
) -> dict[str, Any]:
    """Build a `tool`-role message with its result capped.

    Every tool reply in this module goes through here. A tool that returns a
    customer's raw HTTP response can produce megabytes (`test_tool` hands back
    `result.data` verbatim), and because the transcript is persisted and
    replayed, an uncapped one doesn't fail a turn — it kills the thread
    permanently. One choke point means a tool added later can't reintroduce
    that by forgetting to cap.

    The cap varies by tool. A handful of tools exist specifically to return a
    document the user asked to work on — a node's prompt, a workflow's source —
    and shrinking those to the generic cap defeats their whole purpose
    silently: the model then edits something it only saw the beginning of. Pass
    `tool_name` so those get the document budget; everything else, including
    anything added later that forgets to pass it, keeps the aggressive default.
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": compact_tool_result(payload, max_chars=result_cap_for(tool_name)),
    }


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
                rebuilt.append(_tool_message(call_id, {"skipped": reason}))
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
        "parsed": workflow,
    }, []


# Node fields whose contents are long prose; the card says *that* they changed
# rather than dumping both versions.
_PROSE_FIELDS = {"prompt", "greeting", "instructions"}


def _node_label(node: dict[str, Any]) -> str:
    data = node.get("data") or {}
    return str(data.get("name") or node.get("id") or "a node")


def _describe_workflow_changes(
    previous: dict[str, Any], proposed: dict[str, Any]
) -> list[str]:
    """Say what this save actually changes, in plain language.

    The approval card previously offered only raw TypeScript, which is a poor
    basis for deciding whether to approve something — you had to read a whole
    file to find the one edited line. Nodes are matched on `data.name`, the
    canonical identifier the authoring guide tells the model to keep stable.
    """
    before = {_node_label(n): (n.get("data") or {}) for n in previous.get("nodes") or []}
    after = {_node_label(n): (n.get("data") or {}) for n in proposed.get("nodes") or []}

    changes: list[str] = []
    for name in after:
        if name not in before:
            changes.append(f"Adds “{name}”")
    for name in before:
        if name not in after:
            changes.append(f"Removes “{name}”")

    for name, new_data in after.items():
        old_data = before.get(name)
        if old_data is None:
            continue
        edited = sorted(
            key
            for key in set(old_data) | set(new_data)
            if old_data.get(key) != new_data.get(key)
        )
        if not edited:
            continue
        shown = ", ".join(
            f"{key} (rewritten)" if key in _PROSE_FIELDS else key for key in edited[:4]
        )
        more = f" +{len(edited) - 4} more" if len(edited) > 4 else ""
        changes.append(f"Edits “{name}”: {shown}{more}")

    before_edges, after_edges = len(previous.get("edges") or []), len(proposed.get("edges") or [])
    if before_edges != after_edges:
        changes.append(f"Connections: {before_edges} → {after_edges}")

    return changes or ["No structural change — the source is equivalent."]


async def _run_loop(
    messages: list[dict[str, Any]],
    *,
    toolbox: WorkflowGenToolbox,
) -> AsyncIterator[LoopStep]:
    # Everything here repairs history rather than trusting it. A transcript is
    # persisted and replayed, so anything wrong with it is permanent until
    # fixed on load: a thread saved broken by an earlier version would
    # otherwise fail identically on every future turn, forever.
    #
    # sanitize_transcript shrinks oversized results (a tool that returned a
    # customer's whole API response), drops the oldest whole turns if the
    # history has outgrown the context window, and removes any tool reply left
    # stranded. _answer_dangling_tool_calls then covers the opposite orphan.
    repair = sanitize_transcript(messages)
    if repair.changed:
        logger.info(f"workflow_gen transcript repaired on load: {repair.describe()}")
    _answer_dangling_tool_calls(messages, "This step was interrupted and did not complete.")

    for _ in range(MAX_TOOL_ITERATIONS):
        # The loop can append up to MAX_TOOL_ITERATIONS results of its own
        # within this single turn, so a transcript that fit on entry can stop
        # fitting part-way through. Re-checked per iteration rather than only
        # on entry; it's a no-op (one pass of the transcript) while there's
        # room, and only ever drops turns older than the current one.
        dropped = trim_to_budget(messages)
        if dropped:
            logger.info(f"workflow_gen dropped {dropped} older turn(s) to stay within budget")

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
                    messages.append(_tool_message(c.id, content))
                continue

            preview: dict[str, Any] | None = None
            if call.function.name == "write_code_file":
                # Build the diff before the card is shown, so the user reviews
                # the change rather than a wall of proposed file content.
                preview = await _preview_code_file(
                    toolbox,
                    arguments.get("path", ""),
                    arguments.get("content", ""),
                )
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
                        messages.append(_tool_message(c.id, content))
                    continue

                # Describe the edit so the card can say what changes instead of
                # only offering the source.
                parsed_workflow = preview.pop("parsed", {}) if preview else {}
                target_id = arguments.get("workflow_id")
                if call.function.name == "save_workflow" and target_id and preview:
                    try:
                        # Compare like for like. The stored JSON and a parsed
                        # TypeScript tree normalize differently — fields sitting
                        # at their spec default are omitted from emitted source
                        # and re-added by the parser — so diffing one against
                        # the other reports edits that never happened. Putting
                        # the current workflow through the same
                        # generate-then-parse round-trip removes that entirely.
                        current_code = await toolbox.get_workflow_code(int(target_id))
                        baseline = await parse_code(current_code["code"])
                        if baseline.get("ok"):
                            preview["changes"] = _describe_workflow_changes(
                                baseline.get("workflow") or {}, parsed_workflow
                            )
                    except (WorkflowGenToolboxError, TsBridgeError, ValueError, TypeError) as e:
                        # A summary is a nicety; never block the approval on it.
                        logger.warning(f"workflow_gen change summary unavailable: {e}")

            action_id = uuid4().hex
            # What the user is shown, with any secrets redacted. Kept beside
            # `arguments` (which stays intact, since it's what executes) so a
            # reopened thread can re-render the card without the raw secret.
            shown_arguments = _mask_secret_arguments(arguments)
            if call.function.name == "write_code_file":
                # The diff is the review surface; the full proposed content
                # would only bloat the card and the persisted transcript.
                shown_arguments = {
                    k: v for k, v in shown_arguments.items() if k != "content"
                }
            elif call.function.name in _REPLACEMENT_TOOLS:
                # Same reasoning, and the card never showed these anyway — it
                # renders their *sizes*. A replacement can be thousands of
                # characters, and this payload is persisted with the session
                # and re-sent every time the thread is reopened, so carrying
                # the text itself would grow the transcript for nothing. The
                # unmasked arguments stay in `pending_action`, which is what
                # actually executes.
                shown_arguments = {
                    **{
                        k: v
                        for k, v in shown_arguments.items()
                        if k not in ("old_text", "new_text")
                    },
                    "old_text_chars": len(arguments.get("old_text") or ""),
                    "new_text_chars": len(arguments.get("new_text") or ""),
                }
            display_preview = {**shown_arguments, **(preview or {})}
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
                _tool_message(call.id, result, tool_name=call.function.name)
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
    if tool_name == "update_node":
        # Named fields, not counts: the value of a targeted edit is that the
        # user can see it touches one node and exactly which parts of it.
        fields = ", ".join(sorted(arguments.get("fields") or {})) or "nothing"
        node = arguments.get("node_id") or "a node"
        return f'Ready to update {fields} on "{node}", saved as a draft.'
    if tool_name == "replace_in_node":
        node = arguments.get("node_id") or "a node"
        field = arguments.get("field") or "a field"
        removed = len(arguments.get("old_text") or "")
        added = len(arguments.get("new_text") or "")
        # Say which way it goes. "Removing" and "replacing" are different
        # enough decisions that the card should not make the user infer it.
        what = "Removing" if added == 0 else "Replacing"
        return (
            f'{what} {removed} characters in {field} on "{node}"'
            + (f", replaced by {added}." if added else ", deleting it.")
        )
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
    if tool_name == "write_code_file":
        path = arguments.get("path") or "a file"
        existing = shape.get("previous_chars")
        if existing is None:
            return f'Ready to create "{path}".'
        return (
            f'Ready to rewrite "{path}" '
            f"({existing} → {len(arguments.get('content') or '')} characters)."
        )
    if tool_name == "replace_in_code_file":
        path = arguments.get("path") or "a file"
        removed = len(arguments.get("old_text") or "")
        added = len(arguments.get("new_text") or "")
        what = "Removing" if added == 0 else "Replacing"
        return (
            f'{what} {removed} characters in "{path}"'
            + (f", replaced by {added}." if added else ", deleting it.")
        )
    if tool_name == "delete_code_file":
        return f'Ready to delete "{arguments.get("path")}".'
    if tool_name == "set_env_var":
        key = arguments.get("key") or "an environment variable"
        return f'Ready to set "{key}" (value hidden) for the Code Editor workspace.'
    if tool_name == "delete_env_var":
        return f'Ready to delete the environment variable "{arguments.get("key")}".'
    return f"Ready to run {tool_name}."


async def _preview_code_file(
    toolbox: WorkflowGenToolbox, path: str, content: str
) -> dict[str, Any]:
    """What the approval card shows for a file write.

    A unified diff rather than the whole new file: the spec requires the user
    accept the change, and they can only meaningfully do that if they can see
    what changed. Handing them a 200-line file and asking "ok?" is a rubber
    stamp, not a review.
    """
    import difflib

    previous: str | None = None
    try:
        existing = await toolbox.read_code_file(path)
        previous = existing.get("content")
    except WorkflowGenToolboxError:
        previous = None  # a new file

    preview: dict[str, Any] = {"path": path}
    if previous is None:
        preview["is_new"] = True
        preview["diff"] = "\n".join(f"+{line}" for line in content.splitlines()[:200])
        return preview

    preview["is_new"] = False
    preview["previous_chars"] = len(previous)
    diff = list(
        difflib.unified_diff(
            previous.splitlines(),
            content.splitlines(),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            lineterm="",
            n=3,
        )
    )
    if not diff:
        preview["diff"] = "No change — the proposed content is identical."
    else:
        # Bounded: this is persisted into the transcript and replayed on every
        # later turn, so an unbounded diff would grow the context permanently.
        preview["diff"] = "\n".join(diff[:200])
        if len(diff) > 200:
            preview["diff"] += f"\n… [{len(diff) - 200} more diff lines]"
    return preview


def _system_prompt(workflow_id: int | None, surface: str = "standalone") -> str:
    """The base prompt, plus where this conversation is open.

    Two short orientation blocks rather than two separate prompts. The toolset
    is deliberately identical everywhere: the requests worth answering often
    span both surfaces — "add complaint creation to this agent" needs a Python
    handler *and* a tool attached to a node — and splitting the prompt would
    leave the assistant able to do only half, with the user expected to know
    which half lives where. Everything shared (secrets, approvals, the repair
    loop, the voice constraints) then stays written once and cannot drift.
    """
    from api.services.workflow_gen.system_prompt import WORKFLOW_GEN_SYSTEM_PROMPT

    prompt = WORKFLOW_GEN_SYSTEM_PROMPT

    if workflow_id is not None:
        prompt += f"""
## The workflow you are working on

This conversation is open inside workflow **{workflow_id}**'s editor. The user is looking at it right now, so "this workflow", "the flow", "this agent", or a bare node name always means workflow {workflow_id}.

Never ask which workflow to change, and never list workflows to choose from — you already know. Call `list_nodes({workflow_id})` to see its real node names, instead of asking the user to confirm them. Only touch a different workflow if the user names one explicitly.

### Changing node content vs. changing structure

Most requests — rewriting a prompt, renaming a node, adjusting a node's settings — change **one node's data**. For those, use `list_nodes` → `get_node` → `update_node`. Pass only the fields that change; anything you omit is left as it was.

When the change is to *part* of a long field — removing a paragraph, fixing one line — prefer `replace_in_node`. It swaps an exact piece of text and leaves everything else byte for byte, so nothing outside what you matched can be lost. That makes it the only safe edit for a field too large to have read in full: if `get_node` came back marked truncated, do not rewrite that field with `update_node`, use `replace_in_node` or tell the user what you can't see.

Do **not** reach for `get_workflow_code` + `save_workflow` to do that. Those replace the entire workflow, which means re-emitting every node verbatim. On a large workflow — one with a long prompt or an embedded data table — the source may come back to you shortened, and rewriting it all in one response may not finish. Saving from source you were only shown part of would delete the parts you never saw.

Use `save_workflow` only when the **structure** changes: adding or removing nodes, or rewiring edges. If you ever find yourself about to save source you suspect is incomplete, stop and say so instead.
"""

    if surface == "code_editor":
        prompt += """
## Where you are

This conversation is open in the **Code Editor**, so the user is looking at the Python workspace, not a workflow graph. Read an ambiguous request as being about code: "add a tool for checking stock" here means write the function schema and the router branch, not create a Dograh HTTP tool.

You still have every workflow tool, and should use them when the request genuinely calls for it — attaching a finished function to a node, for example. Just don't reach for them by default, and don't offer to build a voice agent unless that is plainly what was asked for.
"""

    return prompt


async def run_turn(
    *,
    organization_id: int,
    user_id: int | None,
    prior_messages: list[dict[str, Any]],
    user_message: str,
    workflow_id: int | None = None,
    surface: str = "standalone",
) -> AsyncIterator[LoopStep]:
    toolbox = WorkflowGenToolbox(organization_id, user_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(workflow_id, surface)},
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
    if tool_name == "write_code_file":
        yield {"event": _status("Saving the file…")}
        try:
            result = await toolbox.write_code_file(
                arguments.get("path", ""), arguments.get("content", "")
            )
        except WorkflowGenToolboxError as e:
            # Validation failures are exactly what the repair loop is for: the
            # errors name the field, so the model fixes the file and resubmits
            # rather than the user seeing a red card for a fixable mistake.
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("File saved."),
            "result": {"kind": "write_code_file", **result},
        }
        return

    if tool_name in ("update_node", "replace_in_node"):
        yield {"event": _status("Updating the node…")}
        try:
            if tool_name == "replace_in_node":
                result = await toolbox.replace_in_node(
                    arguments.get("workflow_id"),
                    arguments.get("node_id", ""),
                    arguments.get("field", ""),
                    arguments.get("old_text", ""),
                    arguments.get("new_text", ""),
                    bool(arguments.get("replace_all")),
                )
            else:
                result = await toolbox.update_node(
                    arguments.get("workflow_id"),
                    arguments.get("node_id", ""),
                    arguments.get("fields") or {},
                )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        # Reports failure as data, like the other shared cores, so a rejected
        # edit goes to the repair loop rather than to the user as a red card.
        if not result.get("saved"):
            yield {"event": None, "result": None, "errors": _mcp_result_errors(result)}
            return
        yield {
            "event": _status("Node updated."),
            "result": {"kind": tool_name, **result},
        }
        return

    if tool_name == "replace_in_code_file":
        yield {"event": _status("Editing the file…")}
        try:
            result = await toolbox.replace_in_code_file(
                arguments.get("path", ""),
                arguments.get("old_text", ""),
                arguments.get("new_text", ""),
                bool(arguments.get("replace_all")),
            )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("File updated."),
            "result": {"kind": "replace_in_code_file", **result},
        }
        return

    if tool_name == "delete_code_file":
        yield {"event": _status("Deleting the file…")}
        try:
            result = await toolbox.delete_code_file(arguments.get("path", ""))
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("File deleted."),
            "result": {"kind": "delete_code_file", **result},
        }
        return

    if tool_name == "set_env_var":
        yield {"event": _status("Saving the environment variable…")}
        try:
            result = await toolbox.set_env_var(
                arguments.get("key", ""), arguments.get("value", "")
            )
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("Environment variable saved."),
            "result": {"kind": "set_env_var", **result},
        }
        return

    if tool_name == "delete_env_var":
        yield {"event": _status("Deleting the environment variable…")}
        try:
            result = await toolbox.delete_env_var(arguments.get("key", ""))
        except WorkflowGenToolboxError as e:
            yield {"event": None, "result": None, "errors": e.errors}
            return
        yield {
            "event": _status("Environment variable deleted."),
            "result": {"kind": "delete_env_var", **result},
        }
        return

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
    surface: str = "standalone",
) -> AsyncIterator[LoopStep]:
    toolbox = WorkflowGenToolbox(organization_id, user_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(workflow_id, surface)},
        *prior_messages,
    ]
    # This path reaches `llm_client.complete` on its own (the repair hops
    # below) before `_run_loop` ever runs, so it can't rely on the loop's
    # sanitize — a confirm on an already-poisoned thread would fail here.
    repair = sanitize_transcript(messages)
    if repair.changed:
        logger.info(f"workflow_gen transcript repaired on confirm: {repair.describe()}")

    tool_call_id = pending_action["tool_call_id"]
    tool_name = pending_action["action_type"]
    arguments = pending_action["arguments"]

    def _respond_siblings(reason: str) -> None:
        for sibling_id in pending_action.get("sibling_call_ids", []):
            messages.append(_tool_message(sibling_id, {"skipped": reason}))

    def _step(event: dict[str, Any], *, workflow_id: int | None = None) -> LoopStep:
        # `messages[0]` is always the system prompt injected at the top of
        # this function — strip it before it reaches the caller, matching
        # what `_run_loop`-sourced yields already do.
        return LoopStep(event, list(messages)[1:], workflow_id=workflow_id)

    if not approve:
        messages.append(
            _tool_message(
                tool_call_id,
                {"declined": True, "message": "The user reviewed this and chose not to proceed."},
            )
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
            # `test_tool` lands here carrying the endpoint's raw response body,
            # which is unbounded — this is the path that produced a 17MB
            # message and permanently bricked the thread it was in.
            messages.append(_tool_message(tool_call_id, result))
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
                _tool_message(
                    tool_call_id,
                    {"validation_errors": last_errors, "please_fix_and_resubmit": True},
                )
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
                        _tool_message(
                            lookup.id, lookup_result, tool_name=lookup.function.name
                        )
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
                messages.append(_tool_message(repaired.id, {"error": parse_error}))
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
        messages.append(_tool_message(tool_call_id, {"error": last_errors[0]}))
        primary_responded = True
        error_already_yielded = True
        break

    if not succeeded:
        _respond_siblings("The primary action failed.")
        if not primary_responded:
            messages.append(
                _tool_message(tool_call_id, {"validation_errors": last_errors, "gave_up": True})
            )
        if last_errors and not error_already_yielded:
            yield _step(_error("validation_failed", "; ".join(filter(None, last_errors))))

    async for step in _run_loop(messages, toolbox=toolbox):
        step.messages = step.messages[1:]
        if built_workflow_id and step.workflow_id is None:
            step.workflow_id = built_workflow_id
        yield step
