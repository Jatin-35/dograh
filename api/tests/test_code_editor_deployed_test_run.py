"""Two things added alongside the Test Latest / Test Deployed / Docs panel:

* `/run/{function_name}` (the real runtime route, called by a live tool) now
  pulls a real call's identity out of the request body and hands it to the
  router as `context`, instead of always synthesizing an org-only context.
* `/test-run-deployed` is new: a developer testing the *live* snapshot from
  the editor, as distinct from `/test-run` (the draft) and `/run/{function_name}`
  (the runtime entry point, which collapses failures into a speakable message
  for the model rather than the full envelope a human debugging a run wants).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.code_editor import TestRunRequest as CodeEditorTestRunRequest
from api.routes.code_editor import run_deployed_function
# Aliased on import: a bare `test_run_deployed` name here would make pytest's
# default collection pick up the route function itself as a test case.
from api.routes.code_editor import test_run_deployed as run_deployed_test_route

ORG_ID = 5


def _user(organization_id: int = ORG_ID) -> MagicMock:
    user = MagicMock()
    user.selected_organization_id = organization_id
    user.id = 1
    return user


def _deployed_version(files: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(files=files or {"router.py": "..."})


def _db(
    *,
    files: dict[str, str] | None = None,
    deployed: bool = True,
    workflow_org: int | None = ORG_ID,
    run_of_workflow: int | None = 7,
) -> MagicMock:
    """A db_client whose ownership lookups say the injected identity is the
    caller's own — the ordinary case. Tests that care about the opposite pass
    a different `workflow_org` / `run_of_workflow`."""
    db = MagicMock()
    db.get_deployed_code_editor_version = AsyncMock(
        return_value=_deployed_version(files) if deployed else None
    )
    db.get_workflow_organization_id = AsyncMock(return_value=workflow_org)
    db.get_workflow_run = AsyncMock(
        return_value=(
            None
            if run_of_workflow is None
            else SimpleNamespace(workflow_id=run_of_workflow)
        )
    )
    return db


# ---------------------------------------------------------------------------
# run_deployed_function — pulling a real call's identity out of the body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_keys_are_forwarded_as_context_not_left_in_event():
    """The keys a live tool call injects (see `_inject_code_editor_context` in
    `services/workflow/tools/custom_tool.py`) must reach `build_context`, and
    must not leak into `event` where they could be mistaken for one of the
    model's own declared parameters."""
    db = _db()
    run_test = AsyncMock(
        return_value={"statusCode": 200, "result": {"ok": True}}
    )

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function(
            "check_order_status",
            {
                "order_id": "ORD-1",
                "_dograh_workflow_id": 7,
                "_dograh_workflow_run_id": 99,
            },
            user=_user(),
        )

    run_test.assert_awaited_once()
    _, kwargs = run_test.call_args
    assert kwargs["workflow_id"] == 7
    assert kwargs["workflow_run_id"] == 99
    # The reserved keys must not have survived into the model-facing event.
    event = kwargs.get("event") or run_test.call_args.args[1]
    assert "_dograh_workflow_id" not in event
    assert "_dograh_workflow_run_id" not in event
    assert event["order_id"] == "ORD-1"


@pytest.mark.asyncio
async def test_missing_reserved_keys_default_to_none():
    """A tool deployed before this change (or any caller that omits them)
    must not crash — the context is honestly null, not a KeyError."""
    db = _db()
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function("check_order_status", {}, user=_user())

    _, kwargs = run_test.call_args
    assert kwargs["workflow_id"] is None
    assert kwargs["workflow_run_id"] is None


@pytest.mark.asyncio
async def test_the_organization_still_comes_from_the_user_not_the_body():
    """Unchanged by this addition: a body cannot claim another org's identity
    just because it can now claim a workflow_id."""
    db = _db()
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function(
            "x", {"organization_id": 999}, user=_user(organization_id=ORG_ID)
        )

    db.get_deployed_code_editor_version.assert_awaited_once_with(ORG_ID)
    args, _ = run_test.call_args
    assert args[0] == ORG_ID


@pytest.mark.asyncio
async def test_a_workflow_id_from_another_org_is_dropped_not_trusted():
    """`/run/{function_name}` is reachable by anyone holding the org's runtime
    key, so the body is not a trusted channel — and the generated router's own
    docstring invites customers to branch on `context["workflow_id"]`.

    A workflow id the caller does not own must never reach that context: it
    would let a caller drive an organization's own code down a branch reserved
    for a different agent. Dropped rather than rejected, because a 4xx here
    would fail a live call over a field the customer's code may well ignore.
    """
    db = _db(workflow_org=999)  # the workflow belongs to somebody else
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function(
            "x",
            {"_dograh_workflow_id": 7, "_dograh_workflow_run_id": 99},
            user=_user(organization_id=ORG_ID),
        )

    _, kwargs = run_test.call_args
    assert kwargs["workflow_id"] is None
    # The run id goes with it: it was only ever meaningful as that workflow's.
    assert kwargs["workflow_run_id"] is None


@pytest.mark.asyncio
async def test_a_run_id_belonging_to_a_different_workflow_is_dropped():
    """Owning the workflow does not imply owning the run. Without this, a
    caller could pair their own workflow_id with someone else's run id and
    have their code stamp a record with it."""
    db = _db(run_of_workflow=4242)  # a run of a *different* workflow
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function(
            "x",
            {"_dograh_workflow_id": 7, "_dograh_workflow_run_id": 99},
            user=_user(),
        )

    _, kwargs = run_test.call_args
    assert kwargs["workflow_id"] == 7  # this half is legitimately the caller's
    assert kwargs["workflow_run_id"] is None


@pytest.mark.asyncio
async def test_a_run_id_with_no_workflow_id_cannot_be_checked_so_is_dropped():
    """An unverifiable identity is worth less than no identity."""
    db = _db()
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_function(
            "x", {"_dograh_workflow_run_id": 99}, user=_user()
        )

    _, kwargs = run_test.call_args
    assert kwargs["workflow_run_id"] is None
    db.get_workflow_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# /test-run-deployed — the new panel tab's backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_deployed_yet_is_a_clear_409_not_a_500():
    db = _db(deployed=False)

    with patch("api.routes.code_editor.db_client", db):
        with pytest.raises(HTTPException) as exc:
            await run_deployed_test_route(
                CodeEditorTestRunRequest(event={"function_name": "x"}), user=_user()
            )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_it_runs_the_deployed_files_never_the_draft():
    """The whole point of the tab: Test Deployed must be unaffected by
    whatever is currently unsaved in the editor."""
    deployed = _deployed_version({"router.py": "DEPLOYED VERSION"})
    db = MagicMock()
    db.get_deployed_code_editor_version = AsyncMock(return_value=deployed)
    run_test = AsyncMock(
        return_value={"statusCode": 200, "result": {"ok": True}, "logs": "hi"}
    )

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_test_route(
            CodeEditorTestRunRequest(event={"function_name": "check_order_status"}),
            user=_user(),
        )

    _, kwargs = run_test.call_args
    assert kwargs["files"] is deployed.files
    assert kwargs["files"] == {"router.py": "DEPLOYED VERSION"}


@pytest.mark.asyncio
async def test_it_returns_the_full_envelope_not_the_collapsed_runtime_shape():
    """Unlike `/run/{function_name}` (built for the model, which only ever
    wants a speakable result), this is for a developer debugging a run —
    statusCode/logs/error/traceback all have to reach the panel."""
    db = _db()
    envelope = {
        "statusCode": 500,
        "result": None,
        "logs": "printed something",
        "error": "boom",
        "traceback": "Traceback...",
    }
    run_test = AsyncMock(return_value=envelope)

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        outcome = await run_deployed_test_route(
            CodeEditorTestRunRequest(event={"function_name": "x"}), user=_user()
        )

    assert outcome == envelope


@pytest.mark.asyncio
async def test_no_workflow_context_leaks_into_an_interactive_test():
    """Test Deployed has no real call behind it — it must never claim one by
    forwarding a stray workflow_id/workflow_run_id."""
    db = _db()
    run_test = AsyncMock(return_value={"statusCode": 200, "result": {}})

    with patch("api.routes.code_editor.db_client", db), patch(
        "api.routes.code_editor.workspace.run_test", run_test
    ):
        await run_deployed_test_route(
            CodeEditorTestRunRequest(event={"function_name": "x"}), user=_user()
        )

    _, kwargs = run_test.call_args
    assert "workflow_id" not in kwargs
    assert "workflow_run_id" not in kwargs
