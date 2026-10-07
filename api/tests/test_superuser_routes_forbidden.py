"""Every superadmin-only route refuses a signed-in user who isn't a superuser.

A client (Kailash, Think Gas, 7 Oct 2026) was shown the Super Admin menu by a
stale sidebar check. The menu is fixed in the UI; this pins the part that
actually protects the data: the server answers 403 to every one of these, so a
client clicking a stale menu, or calling the API directly, gets nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

# (method, path, json body) — every route behind get_superuser.
SUPERUSER_ONLY = [
    ("POST", "/api/v1/superuser/impersonate", {"provider_user_id": "x"}),
    ("GET", "/api/v1/superuser/workflow-runs", None),
    ("GET", "/api/v1/superuser/organizations", None),
    ("POST", "/api/v1/superuser/organizations", {"name": "Evil", "email": "a@b.c"}),
    ("PATCH", "/api/v1/superuser/organizations/5/status", {"status": "active"}),
    ("PATCH", "/api/v1/superuser/organizations/5/scout", {"enabled": True}),
    ("GET", "/api/v1/superuser/workflows", None),
    ("GET", "/api/v1/superuser/organizations/5/invitations", None),
    ("POST", "/api/v1/superuser/organizations/5/invitations", {"email": "a@b.co"}),
    ("DELETE", "/api/v1/superuser/organizations/5/invitations/inv-1", None),
    ("GET", "/api/v1/wallet/organizations/5", None),
    ("PATCH", "/api/v1/wallet/organizations/5", {}),
    ("POST", "/api/v1/wallet/organizations/5/topup", {"amount": 1000}),
    ("POST", "/api/v1/wallet/organizations/5/adjust", {"amount": 1000}),
    ("GET", "/api/v1/wallet/organizations/5/transactions", None),
    ("PATCH", "/api/v1/wallet/workflows/1/billing-settings", {}),
]

CLIENT = SimpleNamespace(
    id=6,
    provider_id="8c7d58be-ca97-49d2-b67d-5abfd8310963",
    is_superuser=False,
    selected_organization_id=5,
    email=None,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", SUPERUSER_ONLY, ids=[f"{m} {p}" for m, p, _ in SUPERUSER_ONLY])
async def test_a_signed_in_non_superuser_is_refused(method, path, body):
    from api.app import app

    transport = httpx.ASGITransport(app=app)
    with patch("api.services.auth.depends.get_user", AsyncMock(return_value=CLIENT)):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.request(
                method, path, json=body, headers={"Authorization": "Bearer client-token"}
            )
    # 422 would mean the body was rejected before the superuser check ran; the
    # bodies above are shaped so the check is what answers.
    assert response.status_code == 403, (path, response.status_code, response.text[:200])
    assert "Superuser" in response.text


@pytest.mark.asyncio
async def test_every_get_superuser_route_is_covered():
    """A new superadmin route must be added to the list above."""
    from api.app import app
    from api.services.auth.depends import get_superuser

    def uses_superuser(route) -> bool:
        dependant = getattr(route, "dependant", None)
        stack = list(dependant.dependencies) if dependant else []
        while stack:
            dep = stack.pop()
            if dep.call is get_superuser:
                return True
            stack.extend(dep.dependencies)
        return False

    protected = {
        (method, route.path)
        for route in app.routes
        if uses_superuser(route)
        for method in route.methods
    }
    listed = {
        (
            m,
            p.replace("/5", "/{organization_id}")
            .replace("/workflows/1/", "/workflows/{workflow_id}/")
            .replace("/inv-1", "/{invitation_id}"),
        )
        for m, p, _ in SUPERUSER_ONLY
    }
    assert protected <= listed, sorted(protected - listed)
