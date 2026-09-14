"""Tenant isolation on the Code Editor routes.

The Code Editor holds an organization's source code, its deployment history and
its decrypted environment variables — secrets included. A route that resolves
the organization from anything the caller controls would let one client read
another's code and credentials.

`AGENTS.md` states the rule for the whole backend: org-scoped reads and writes
resolve the organization from the authenticated user, never from the request.
These tests assert it by inspection rather than trusting that each handler was
written correctly, because the way this regresses is a *new* route added later
by someone who did not know the rule.
"""

from __future__ import annotations

import inspect
import re

from api.routes import code_editor

# Every path operation on the router.
HANDLER_DECORATOR = re.compile(
    r"@router\.(get|post|put|delete|patch)\([^)]*\)\s*\n"
    r"(?:@[^\n]*\n)*"
    r"async def (\w+)\(",
)


def _handlers() -> list[tuple[str, str]]:
    """Each route handler's name and its source, taken from the live module."""
    source = inspect.getsource(code_editor)
    found = []
    for match in HANDLER_DECORATOR.finditer(source):
        name = match.group(2)
        start = match.end()
        rest = source[start:]
        end = rest.find("\n@router.")
        found.append((name, rest if end == -1 else rest[:end]))
    return found


def test_the_router_actually_has_routes():
    """A regex that silently matches nothing would make every test below pass
    vacuously, which is worse than no test at all."""
    names = [name for name, _ in _handlers()]
    assert len(names) >= 10, f"only found {names}"
    # Spot-check both ends of the file so a partial match is caught too.
    assert "list_files" in names
    assert "run_deployed_function" in names


# The editor's own routes are called by a signed-in person in a browser. The
# runtime route is called by a *deployed tool* during a live call, which carries
# the organization's API key and no session — so it authenticates with
# `get_user` instead. It is still org-scoped through the same `_org(user)`.
API_KEY_AUTHENTICATED = {"run_deployed_function"}


def test_every_route_is_authenticated():
    """No anonymous access to an organization's source code."""
    for name, body in _handlers():
        expected = (
            "Depends(get_user)"
            if name in API_KEY_AUTHENTICATED
            else "Depends(get_user_with_selected_organization)"
        )
        assert expected in body, (
            f"{name} does not depend on {expected} — every Code Editor route "
            "must be authenticated, because the workspace holds source code "
            "and decrypted secrets."
        )


def test_only_the_runtime_route_skips_the_session_dependency():
    """`get_user` accepts an API key, so it is the weaker of the two. Exactly
    one route is allowed it, and this fails if a browser-facing route is ever
    quietly dropped down to it — or if the runtime route disappears and this
    allowance is left behind pointing at nothing."""
    relaxed = {
        name
        for name, body in _handlers()
        if "Depends(get_user_with_selected_organization)" not in body
    }
    assert relaxed == API_KEY_AUTHENTICATED


def test_every_route_resolves_the_org_from_the_user():
    """`_org(user)` reads `user.selected_organization_id`. Any other source —
    a path parameter, a query string, a request body field — would be caller
    controlled, and the caller is who we are isolating."""
    for name, body in _handlers():
        assert "_org(user)" in body, (
            f"{name} does not resolve its organization with _org(user). "
            "Resolving it from the request instead would let a client read "
            "another organization's workspace."
        )


def test_no_route_accepts_an_organization_id_from_the_caller():
    """The strongest form of the rule: the parameter must not exist at all.
    A handler that merely ignores a supplied `organization_id` today is one
    edit away from honouring it."""
    for name, body in _handlers():
        signature = body.split(")", 1)[0]
        assert "organization_id" not in signature, (
            f"{name} takes an organization_id parameter. It must be derived "
            "from the authenticated user, never accepted from the caller."
        )
