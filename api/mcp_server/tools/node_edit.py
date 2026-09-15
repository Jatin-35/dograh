"""Reading and editing one node at a time.

`get_workflow_code` / `save_workflow` are a whole-document round-trip: read the
entire workflow as TypeScript, edit it, send the entire thing back. That works
well until a workflow gets big, and then it fails from both ends at once —

* the source comes back shrunk, because a tool result is capped
  (`MAX_TOOL_RESULT_CHARS`), so the model is editing a document it cannot
  actually see all of;
* and re-emitting the whole workflow exceeds what a model will write in one
  response, so the request either runs past the timeout or returns partial
  source.

Both are fatal for the most common request there is — "change the prompt on
this node" — which touches one field and needs none of that. These tools take
that path instead: the stored workflow JSON is edited in place, server-side,
and only the changed node ever crosses the wire.

Safety is not traded away for it. A targeted edit runs the *same* validation
chain as a full save (`ReactFlowDTO` → trigger paths → `WorkflowGraph`) and
lands as a draft the same way, so the published version serving live calls is
untouched either way. What it removes is the class of failure where a truncated
document is written back over a complete one.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from fastapi import HTTPException
from loguru import logger
from pydantic import ValidationError as PydanticValidationError

from api.db import db_client
from api.db.models import UserModel
from api.mcp_server.auth import authenticate_mcp_request
from api.mcp_server.tools._workflow_projection import (
    select_workflow_projection_source,
)
from api.mcp_server.tracing import traced_tool
from api.services.workflow.dto import ReactFlowDTO
from api.services.workflow.trigger_paths import validate_trigger_paths
from api.services.workflow.workflow_graph import WorkflowGraph

# How much of a node's prompt `list_nodes` shows. Enough to recognise which
# node is which without turning the listing into the very whole-document dump
# these tools exist to avoid.
_PROMPT_PREVIEW_CHARS = 200


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"saved": False, "error_code": code, "error": message, **extra}


async def _load(workflow_id: int, user: UserModel) -> tuple[Any, dict[str, Any]]:
    """The workflow row plus the working copy both tools operate on.

    Same source selection as the read and save tools (draft over published over
    legacy), so a targeted edit never silently works from a different version
    than `get_workflow_code` showed.
    """
    workflow = await db_client.get_workflow(
        workflow_id, organization_id=user.selected_organization_id
    )
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
    source = await select_workflow_projection_source(workflow)
    payload = copy.deepcopy(source.payload or {"nodes": [], "edges": []})
    payload.setdefault("nodes", [])
    payload.setdefault("edges", [])
    return workflow, payload


def _node_name(node: dict[str, Any]) -> str:
    data = node.get("data")
    if isinstance(data, dict):
        name = data.get("name")
        if isinstance(name, str):
            return name
    return ""


def _find_node(
    payload: dict[str, Any], node_id: str
) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Locate a node by id, falling back to its display name.

    The name fallback is deliberate: the model has just read a listing where
    both are shown, and a workflow's node ids are opaque strings a person would
    never type. Matching the name it can actually see avoids a round of
    "unknown node" for what is plainly the right node. Returns the known ids
    alongside, so a miss can say what *was* available rather than just "no".
    """
    nodes = payload.get("nodes") or []
    known = [str(n.get("id")) for n in nodes if isinstance(n, dict)]
    for node in nodes:
        if isinstance(node, dict) and str(node.get("id")) == str(node_id):
            return node, known
    matches = [
        node
        for node in nodes
        if isinstance(node, dict) and _node_name(node).strip() == str(node_id).strip()
    ]
    # Only when unambiguous. Two nodes sharing a display name is legal, and
    # picking one of them at random would edit the wrong half of a workflow.
    if len(matches) == 1:
        return matches[0], known
    return None, known


def _validate(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Run the full save-path validation. Returns an error result, or None."""
    issues = validate_trigger_paths(payload)
    if issues:
        return _error("validation_error", "\n".join(i.message for i in issues))
    try:
        dto = ReactFlowDTO.model_validate(payload)
    except PydanticValidationError as e:
        return _error("validation_error", str(e))
    try:
        WorkflowGraph(dto)
    except Exception as e:  # WorkflowGraph raises ValueError
        return _error("graph_validation", str(e))
    return None


@traced_tool
async def list_nodes(workflow_id: int) -> dict[str, Any]:
    """List a workflow's nodes without fetching the whole workflow.

    Returns each node's `id`, `type`, `name`, a short `prompt_preview` and the
    full `prompt_length`. Use this to find the node you want, then
    `get_node` to read it and `update_node` to change it.

    Prefer this over `get_workflow_code` when you intend to change specific
    nodes rather than restructure the graph: a large workflow's source may be
    too big to return in full, while this stays small whatever the size.
    """
    user = await authenticate_mcp_request()
    return await list_nodes_for_user(workflow_id, user)


async def list_nodes_for_user(workflow_id: int, user: UserModel) -> dict[str, Any]:
    _, payload = await _load(workflow_id, user)
    nodes = []
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        prompt = data.get("prompt")
        prompt = prompt if isinstance(prompt, str) else ""
        nodes.append(
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "name": data.get("name") or "",
                "prompt_preview": prompt[:_PROMPT_PREVIEW_CHARS],
                "prompt_length": len(prompt),
            }
        )
    return {"workflow_id": workflow_id, "nodes": nodes, "node_count": len(nodes)}


@traced_tool
async def get_node(workflow_id: int, node_id: str) -> dict[str, Any]:
    """Read one node's full data, by id or by its unique display name.

    The whole point of reading a single node is that it fits: a node's prompt
    is returned complete, where the same prompt inside a large workflow's full
    source may have been shrunk to fit a tool result.
    """
    user = await authenticate_mcp_request()
    return await get_node_for_user(workflow_id, node_id, user)


async def get_node_for_user(
    workflow_id: int, node_id: str, user: UserModel
) -> dict[str, Any]:
    _, payload = await _load(workflow_id, user)
    node, known = _find_node(payload, node_id)
    if node is None:
        return _error(
            "node_not_found",
            f"No node {node_id!r} in workflow {workflow_id}. Known ids: "
            + (", ".join(known) if known else "(none)"),
        )
    return {
        "workflow_id": workflow_id,
        "id": node.get("id"),
        "type": node.get("type"),
        "data": node.get("data"),
    }


@traced_tool
async def update_node(
    workflow_id: int, node_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Change specific fields on one node and save the workflow as a draft.

    `fields` is merged into the node's existing data — pass only what changes
    (`{"prompt": "..."}` to rewrite a prompt, `{"name": "..."}` to rename).
    Anything not named is left exactly as it was, so this cannot drop a field
    by omission the way re-sending a whole document can.

    Use this in preference to `get_workflow_code` + `save_workflow` whenever
    the change is confined to node data. It is the only way to edit a workflow
    whose full source is too large to return or to rewrite in one response.
    Reach for `save_workflow` when the *structure* changes — adding or removing
    nodes, or rewiring edges.

    The edit is validated exactly as a full save is, and saved as a draft; the
    published version continues serving live calls untouched. On failure the
    result has `saved: false` and an `error_code`:
    - `node_not_found` — no such node id or unique name; the message lists the
      ids that do exist.
    - `validation_error` — the resulting node data broke its spec (unknown
      field, missing required, wrong type), or a trigger path is now invalid.
    - `graph_validation` — the graph no longer satisfies a structural rule.
    """
    user = await authenticate_mcp_request()
    return await update_node_for_user(workflow_id, node_id, fields, user)


@traced_tool
async def replace_in_node(
    workflow_id: int,
    node_id: str,
    field: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Replace an exact piece of text inside one of a node's fields.

    For changing part of a long prompt — removing a paragraph, correcting a
    line — without reading or rewriting the whole thing. Nothing outside
    `old_text` is touched, so the parts you never saw cannot be lost. Pass an
    empty `new_text` to delete the matched text.

    `old_text` must appear exactly once, or nothing is written and the result
    says whether it was absent or ambiguous. That is the safety property: an
    edit that could land in two places is refused rather than guessed at. Pass
    `replace_all` only when you intend every occurrence to change.

    Prefer this over `update_node` whenever the change is a part of a long
    field rather than a wholesale rewrite: it is the only edit that is safe on
    a field too large to have read in full.

    Match on the text exactly as `get_node` returned it, whitespace included.
    On failure the result has `saved: false` and an `error_code`:
    - `node_not_found` — no such node id or unique name.
    - `text_not_found` — `old_text` does not appear in that field.
    - `text_not_unique` — it appears more than once and `replace_all` is false.
    - `validation_error` / `graph_validation` — the result broke a rule.
    """
    user = await authenticate_mcp_request()
    return await replace_in_node_for_user(
        workflow_id, node_id, field, old_text, new_text, user, replace_all=replace_all
    )


async def replace_in_node_for_user(
    workflow_id: int,
    node_id: str,
    field: str,
    old_text: str,
    new_text: str,
    user: UserModel,
    *,
    replace_all: bool = False,
) -> dict[str, Any]:
    if not old_text:
        return _error("validation_error", "`old_text` must not be empty.")

    workflow, payload = await _load(workflow_id, user)
    node, known = _find_node(payload, node_id)
    if node is None:
        return _error(
            "node_not_found",
            f"No node {node_id!r} in workflow {workflow_id}. Known ids: "
            + (", ".join(known) if known else "(none)"),
        )

    data = node.get("data")
    if not isinstance(data, dict):
        return _error("validation_error", f"Node {node.get('id')!r} has no editable data.")

    current = data.get(field)
    if not isinstance(current, str):
        return _error(
            "validation_error",
            f"Field {field!r} on node {node.get('id')!r} is not text"
            + (" (it is unset)." if current is None else f" (it is {type(current).__name__})."),
        )

    occurrences = current.count(old_text)
    if occurrences == 0:
        return _error(
            "text_not_found",
            f"That text does not appear in {field!r} on node {node.get('id')!r}. "
            "Match it exactly as get_node returned it, whitespace included.",
        )
    if occurrences > 1 and not replace_all:
        return _error(
            "text_not_unique",
            f"That text appears {occurrences} times in {field!r} on node "
            f"{node.get('id')!r}. Include enough surrounding text to make it "
            "unique, or pass replace_all to change every occurrence.",
        )

    updated = (
        current.replace(old_text, new_text)
        if replace_all
        else current.replace(old_text, new_text, 1)
    )
    node["data"] = {**data, field: updated}

    invalid = _validate(payload)
    if invalid:
        return invalid

    draft = await db_client.save_workflow_draft(
        workflow_id=workflow_id, workflow_definition=payload
    )
    logger.info(
        f"replace_in_node: workflow={workflow_id} node={node.get('id')} "
        f"field={field} replaced={occurrences if replace_all else 1} "
        f"version={draft.version_number}"
    )
    return {
        "saved": True,
        "workflow_id": workflow_id,
        "node_id": node.get("id"),
        "field": field,
        "replacements": occurrences if replace_all else 1,
        "length_before": len(current),
        "length_after": len(updated),
        "version_number": draft.version_number,
        "status": draft.status,
    }


async def update_node_for_user(
    workflow_id: int, node_id: str, fields: dict[str, Any], user: UserModel
) -> dict[str, Any]:
    if not isinstance(fields, dict) or not fields:
        return _error(
            "validation_error",
            "`fields` must be a non-empty object of the node data to change.",
        )

    workflow, payload = await _load(workflow_id, user)
    node, known = _find_node(payload, node_id)
    if node is None:
        return _error(
            "node_not_found",
            f"No node {node_id!r} in workflow {workflow_id}. Known ids: "
            + (", ".join(known) if known else "(none)"),
        )

    existing = node.get("data")
    if not isinstance(existing, dict):
        return _error(
            "validation_error",
            f"Node {node.get('id')!r} has no editable data.",
        )

    # Merged, not replaced. A model that omits a field it didn't mean to touch
    # must not thereby delete it — that is the same failure mode as saving
    # from truncated source, just smaller.
    node["data"] = {**existing, **fields}

    invalid = _validate(payload)
    if invalid:
        return invalid

    draft = await db_client.save_workflow_draft(
        workflow_id=workflow_id, workflow_definition=payload
    )
    logger.info(
        f"update_node: workflow={workflow_id} node={node.get('id')} "
        f"fields={sorted(fields)} version={draft.version_number}"
    )
    return {
        "saved": True,
        "workflow_id": workflow_id,
        "node_id": node.get("id"),
        "updated_fields": sorted(fields),
        "version_number": draft.version_number,
        "status": draft.status,
        "node_count": len(payload.get("nodes") or []),
    }
