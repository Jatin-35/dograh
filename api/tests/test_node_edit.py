"""Editing one node without round-tripping the whole workflow.

Written against a production failure. Asked to rewrite a large agent's global
prompt, the assistant fetched the workflow as TypeScript, got it back
*shortened* (a tool result is capped), and correctly refused to save — because
`save_workflow` replaces the entire draft, and saving from source it had only
partly seen would have deleted the parts it never read. The same size also blew
the LLM read timeout from the other direction, since re-emitting the whole
workflow is more output than one response holds.

The request underneath all that touched one field on one node. These tools do
exactly that much: the stored workflow JSON is edited server-side, only the
changed node crosses the wire, and neither ceiling is in the path.

What must stay true, and is pinned below: a targeted edit is validated exactly
as a full save is, it cannot delete a field by omission, it lands as a draft so
live calls are untouched, and it is scoped to the caller's organization.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.mcp_server.tools.node_edit import (
    get_node_for_user,
    list_nodes_for_user,
    update_node_for_user,
)

ORG_ID = 5
WORKFLOW_ID = 7

LONG_PROMPT = "You are Aastha. " + ("Water tank product data. " * 2000)


def _workflow_json() -> dict:
    return {
        "nodes": [
            {
                "id": "node-start",
                "type": "startCall",
                "position": {"x": 0, "y": 0},
                "data": {"name": "Greeting", "prompt": "Hi there!"},
            },
            {
                "id": "node-agenda",
                "type": "agentNode",
                "position": {"x": 200, "y": 0},
                "data": {
                    "name": "Main Agenda",
                    "prompt": LONG_PROMPT,
                    "is_start": False,
                },
            },
            {
                "id": "node-end",
                "type": "endCall",
                "position": {"x": 400, "y": 0},
                "data": {"name": "Done", "prompt": "Goodbye."},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "node-start",
                "target": "node-agenda",
                "data": {"label": "start", "condition": "greeting done"},
            },
            {
                "id": "e2",
                "source": "node-agenda",
                "target": "node-end",
                "data": {"label": "done", "condition": "conversation complete"},
            },
        ],
    }


def _user(organization_id: int = ORG_ID) -> MagicMock:
    user = MagicMock()
    user.selected_organization_id = organization_id
    user.id = 1
    return user


def _db(*, workflow_found: bool = True, payload: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.get_workflow = AsyncMock(
        return_value=(
            SimpleNamespace(id=WORKFLOW_ID, name="Vectus Smart Care")
            if workflow_found
            else None
        )
    )
    db.save_workflow_draft = AsyncMock(
        return_value=SimpleNamespace(version_number=12, status="draft")
    )
    db._payload = payload if payload is not None else _workflow_json()
    return db


def _patched(db):
    """Patch the db and the shared source-selection helper together."""
    return (
        patch("api.mcp_server.tools.node_edit.db_client", db),
        patch(
            "api.mcp_server.tools.node_edit.select_workflow_projection_source",
            AsyncMock(
                return_value=SimpleNamespace(
                    payload=db._payload, version="draft", version_number=11
                )
            ),
        ),
    )


async def _call(db, coro_factory):
    db_patch, source_patch = _patched(db)
    with db_patch, source_patch:
        return await coro_factory()


# ---------------------------------------------------------------------------
# Reading without the whole document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_nodes_stays_small_however_large_the_workflow_is():
    """The listing must not become the whole-document dump it exists to avoid."""
    db = _db()
    result = await _call(db, lambda: list_nodes_for_user(WORKFLOW_ID, _user()))

    assert result["node_count"] == 3
    names = [n["name"] for n in result["nodes"]]
    assert names == ["Greeting", "Main Agenda", "Done"]

    agenda = next(n for n in result["nodes"] if n["name"] == "Main Agenda")
    # The real length is reported so the model knows what it is dealing with…
    assert agenda["prompt_length"] == len(LONG_PROMPT)
    # …while the preview stays short.
    assert len(agenda["prompt_preview"]) <= 200
    assert len(str(result)) < 2_000


@pytest.mark.asyncio
async def test_a_single_node_is_returned_complete():
    """The point of reading one node: its prompt arrives whole, where the same
    prompt inside a full-workflow fetch came back shortened."""
    db = _db()
    result = await _call(db, lambda: get_node_for_user(WORKFLOW_ID, "node-agenda", _user()))

    assert result["data"]["prompt"] == LONG_PROMPT
    assert len(result["data"]["prompt"]) > 24_000  # over MAX_TOOL_RESULT_CHARS


@pytest.mark.asyncio
async def test_a_node_can_be_addressed_by_its_display_name():
    """Node ids are opaque strings; the name is what the model just read."""
    db = _db()
    result = await _call(db, lambda: get_node_for_user(WORKFLOW_ID, "Main Agenda", _user()))
    assert result["id"] == "node-agenda"


@pytest.mark.asyncio
async def test_an_ambiguous_name_is_refused_rather_than_guessed():
    """Two nodes may legitimately share a display name. Picking one at random
    would edit the wrong half of someone's workflow."""
    payload = _workflow_json()
    payload["nodes"][0]["data"]["name"] = "Main Agenda"  # now two of them
    db = _db(payload=payload)

    result = await _call(db, lambda: get_node_for_user(WORKFLOW_ID, "Main Agenda", _user()))
    assert result["error_code"] == "node_not_found"


@pytest.mark.asyncio
async def test_an_unknown_node_says_what_does_exist():
    db = _db()
    result = await _call(db, lambda: get_node_for_user(WORKFLOW_ID, "nope", _user()))

    assert result["error_code"] == "node_not_found"
    assert "node-agenda" in result["error"]


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prompt_is_replaced_and_saved_as_a_draft():
    db = _db()
    result = await _call(
        db,
        lambda: update_node_for_user(
            WORKFLOW_ID, "node-agenda", {"prompt": "A much shorter prompt."}, _user()
        ),
    )

    assert result["saved"] is True
    assert result["node_id"] == "node-agenda"
    assert result["updated_fields"] == ["prompt"]

    saved = db.save_workflow_draft.await_args.kwargs["workflow_definition"]
    agenda = next(n for n in saved["nodes"] if n["id"] == "node-agenda")
    assert agenda["data"]["prompt"] == "A much shorter prompt."
    # Every other node is exactly as it was — this is the property the
    # whole-document round-trip could not guarantee.
    assert len(saved["nodes"]) == 3
    assert saved["edges"] == _workflow_json()["edges"]


@pytest.mark.asyncio
async def test_fields_are_merged_so_omission_never_deletes():
    """A model that names only `prompt` must not thereby drop `name` — the
    same failure as saving truncated source, just smaller."""
    db = _db()
    await _call(
        db,
        lambda: update_node_for_user(
            WORKFLOW_ID, "node-agenda", {"prompt": "new"}, _user()
        ),
    )

    saved = db.save_workflow_draft.await_args.kwargs["workflow_definition"]
    agenda = next(n for n in saved["nodes"] if n["id"] == "node-agenda")
    assert agenda["data"]["name"] == "Main Agenda"
    assert agenda["data"]["is_start"] is False


@pytest.mark.asyncio
async def test_an_edit_that_breaks_the_node_spec_is_rejected_and_saves_nothing():
    """Same validation as a full save. An empty prompt on a node that requires
    one must not reach the database."""
    db = _db()
    result = await _call(
        db,
        lambda: update_node_for_user(WORKFLOW_ID, "node-agenda", {"prompt": ""}, _user()),
    )

    assert result["saved"] is False
    assert result["error_code"] in ("validation_error", "graph_validation")
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_stored_workflow_is_never_mutated_when_validation_fails():
    """The working copy is a deep copy; a rejected edit must leave the source
    object untouched, not half-applied."""
    payload = _workflow_json()
    original = copy.deepcopy(payload)
    db = _db(payload=payload)

    await _call(
        db,
        lambda: update_node_for_user(WORKFLOW_ID, "node-agenda", {"prompt": ""}, _user()),
    )

    assert payload == original


@pytest.mark.asyncio
async def test_an_unknown_node_is_a_repairable_result_not_an_exception():
    """The agent loop's repair path reads `saved: false` — raising here would
    show the user a red card for something the model can simply correct."""
    db = _db()
    result = await _call(
        db, lambda: update_node_for_user(WORKFLOW_ID, "ghost", {"prompt": "x"}, _user())
    )

    assert result["saved"] is False
    assert result["error_code"] == "node_not_found"
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_fields_is_refused():
    db = _db()
    result = await _call(
        db, lambda: update_node_for_user(WORKFLOW_ID, "node-agenda", {}, _user())
    )
    assert result["saved"] is False
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_organizations_workflow_is_not_reachable():
    """Tenant isolation: the lookup is org-scoped, and a miss is a 404 rather
    than an edit against somebody else's agent."""
    db = _db(workflow_found=False)

    db_patch, source_patch = _patched(db)
    with db_patch, source_patch:
        with pytest.raises(HTTPException) as exc:
            await update_node_for_user(WORKFLOW_ID, "node-agenda", {"prompt": "x"}, _user())

    assert exc.value.status_code == 404
    assert db.get_workflow.await_args.kwargs["organization_id"] == ORG_ID
    db.save_workflow_draft.assert_not_awaited()


# ---------------------------------------------------------------------------
# Replacing part of a field, for a prompt too large to have read in full
# ---------------------------------------------------------------------------


PRICING_LINE = (
    'If asked about pricing, then acknowledge and reply. Use this exactly: '
    '"Actually, hamare prices region to region vary karta h."'
)


def _payload_with_long_prompt() -> dict:
    payload = _workflow_json()
    node = next(n for n in payload["nodes"] if n["id"] == "node-agenda")
    node["data"]["prompt"] = (
        "## Persona\nYou are Aastha.\n\n"
        + PRICING_LINE
        + "\n\nYe details WhatsApp par bhi share ho jayengi.\n\n"
        + "## Product database\n"
        + ("Tank model row. " * 2000)
    )
    return payload


@pytest.mark.asyncio
async def test_a_line_is_removed_without_touching_the_rest():
    """The production case: a 29,000-character prompt where three specific
    lines must go. Nothing outside the matched text may change — which is what
    makes this safe on a field nobody could read in full."""
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    payload = _payload_with_long_prompt()
    before = payload["nodes"][1]["data"]["prompt"]
    db = _db(payload=payload)

    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-agenda", "prompt", PRICING_LINE + "\n\n", "", _user()
        ),
    )

    assert result["saved"] is True
    assert result["replacements"] == 1
    saved = db.save_workflow_draft.await_args.kwargs["workflow_definition"]
    after = next(n for n in saved["nodes"] if n["id"] == "node-agenda")["data"]["prompt"]

    assert PRICING_LINE not in after
    # Everything else is byte-for-byte intact, product database included.
    assert "## Product database" in after
    assert after.count("Tank model row. ") == 2000
    assert len(after) == len(before) - len(PRICING_LINE) - 2


@pytest.mark.asyncio
async def test_text_that_appears_twice_is_refused_not_guessed():
    """An edit that could land in two places must not pick one. This is the
    property that makes a blind edit safe."""
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    payload = _payload_with_long_prompt()
    db = _db(payload=payload)

    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-agenda", "prompt", "Tank model row. ", "", _user()
        ),
    )

    assert result["saved"] is False
    assert result["error_code"] == "text_not_unique"
    assert "2000" in result["error"]
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_all_changes_every_occurrence_when_asked():
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    payload = _payload_with_long_prompt()
    db = _db(payload=payload)

    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-agenda", "prompt", "Tank model row. ", "X. ", _user(),
            replace_all=True,
        ),
    )

    assert result["saved"] is True
    assert result["replacements"] == 2000


@pytest.mark.asyncio
async def test_text_that_is_not_there_writes_nothing():
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    db = _db(payload=_payload_with_long_prompt())
    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-agenda", "prompt", "text that is absent", "x", _user()
        ),
    )

    assert result["saved"] is False
    assert result["error_code"] == "text_not_found"
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replacement_that_breaks_the_node_is_rejected():
    """Same validation as any other write — emptying a required prompt fails."""
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    payload = _workflow_json()
    db = _db(payload=payload)
    whole = payload["nodes"][0]["data"]["prompt"]

    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-start", "prompt", whole, "", _user()
        ),
    )

    assert result["saved"] is False
    db.save_workflow_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_text_field_is_refused_clearly():
    from api.mcp_server.tools.node_edit import replace_in_node_for_user

    db = _db(payload=_payload_with_long_prompt())
    result = await _call(
        db,
        lambda: replace_in_node_for_user(
            WORKFLOW_ID, "node-agenda", "is_start", "False", "True", _user()
        ),
    )

    assert result["saved"] is False
    assert result["error_code"] == "validation_error"
    db.save_workflow_draft.assert_not_awaited()
