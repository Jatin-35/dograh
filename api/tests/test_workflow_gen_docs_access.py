"""The in-product assistant's access to the documentation.

Reported from production: Scout answered a question about template variables by
guessing, and said the docs tool had returned

    Missing API key — send X-API-Key or Authorization: Bearer <key>

Nothing was misconfigured. `toolbox.py` imported the **MCP entry points** for
`list_docs` / `read_doc` / `search_docs`, and each of those calls
`authenticate_mcp_request()`, which reads an API key off an HTTP request. There
is no HTTP request when the assistant calls a tool in-process, so every docs
lookup had always failed — the assistant simply never had the docs.

It failed silently, which is why it went unnoticed: the error came back as a
tool *result*, so the model read "docs unavailable" and reasoned from memory
instead of surfacing anything.

Every other tool on the toolbox avoids this by splitting an auth-free
`*_for_user` core out of the authenticating wrapper. These three were the only
ones never split. The existing MCP tests all use an `authed_user` fixture, so
none of them could have caught it — this file covers the other caller.
"""

from __future__ import annotations

import pytest

from api.services.workflow_gen.toolbox import WorkflowGenToolbox

pytestmark = pytest.mark.asyncio


def _toolbox() -> WorkflowGenToolbox:
    """Constructed exactly as the agent loop constructs it: an org and a user
    id, and no request context of any kind."""
    return WorkflowGenToolbox(organization_id=1, user_id=1)


async def test_search_docs_works_without_a_request_context():
    """The regression. This raised HTTPException(401) before the split."""
    results = await _toolbox().search_docs("template variables", limit=3)

    assert isinstance(results, list)
    assert results, "the assistant got no docs results at all"
    assert all("path" in hit for hit in results)


async def test_list_docs_works_without_a_request_context():
    entries = await _toolbox().list_docs()

    assert isinstance(entries, list)
    assert entries
    assert all("path" in entry for entry in entries)


async def test_read_doc_returns_a_real_page_without_a_request_context():
    box = _toolbox()
    hits = await box.search_docs("template variables", limit=5)
    path = hits[0]["path"]

    page = await box.read_doc(path)

    body = page.get("content") or page.get("body") or ""
    assert body, f"read_doc returned no content for {path!r}"


async def test_the_assistant_can_answer_the_question_it_had_to_guess_at():
    """The specific failure: asked which template variable carries the caller's
    number, it guessed `initial_context.phone_number`, which does not exist.
    The answer is in the docs it could not read."""
    box = _toolbox()
    hits = await box.search_docs("caller number template variable", limit=5)
    paths = [hit["path"] for hit in hits]

    assert "voice-agent/template-variables" in paths, (
        "the page that answers this was not reachable: " + ", ".join(paths)
    )
    page = await box.read_doc("voice-agent/template-variables")
    body = page.get("content") or page.get("body") or ""
    assert "initial_context" in body


async def test_the_mcp_surface_still_requires_a_key():
    """Splitting the core out must not have opened the external surface. The
    docs are not secret, but an unauthenticated MCP tool is a change to who can
    reach this deployment, and it is not one anybody asked for."""
    from fastapi import HTTPException

    from api.mcp_server.tools.docs_search import list_docs, read_doc, search_docs

    for call in (
        lambda: search_docs("anything"),
        lambda: list_docs(),
        lambda: read_doc("voice-agent/template-variables"),
    ):
        with pytest.raises((HTTPException, Exception)) as exc:
            await call()
        # Whatever it raises, it must not have returned docs content.
        assert not isinstance(getattr(exc, "value", None), (list, dict))
