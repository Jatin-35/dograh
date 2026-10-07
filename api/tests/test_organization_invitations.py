"""Inviting people into an existing organization.

Invites used to be sent only while creating an organization, from
PUBLIC_BASE_URL, so emails sent before the server move pointed at the retired
server (103-209-146-197.sslip.io) and Think Gas had no way to get a working
one. Now a superadmin can send, list and revoke invitations, links open on
CLIENT_APP_URL, and resending replaces the dead link.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from api.services import organization_invitations as inv

TEAM = "60bc70b4-4d4f-48b3-be9d-3dcf899b5b8b"
SUPERUSER = SimpleNamespace(
    id=1, provider_id="jatin", is_superuser=True, selected_organization_id=1, email=None
)
CLIENT_URL = "https://voicedashboard.botrixai.com"


def _stack(pending=None):
    """A fake Stack client holding `pending` invitations."""
    state = {"pending": list(pending or []), "sent": [], "revoked": []}

    async def list_team_invitations(team_id):
        return [i for i in state["pending"] if i["team_id"] == team_id]

    async def send_team_invitation(team_id, email, callback_url):
        state["sent"].append((team_id, email, callback_url))

    async def revoke_team_invitation(team_id, invitation_id):
        state["revoked"].append(invitation_id)
        state["pending"] = [i for i in state["pending"] if i["id"] != invitation_id]

    fake = SimpleNamespace(
        list_team_invitations=list_team_invitations,
        send_team_invitation=send_team_invitation,
        revoke_team_invitation=revoke_team_invitation,
    )
    return fake, state


def _pending(id_, email, millis=1791788672230):
    return {"id": id_, "recipient_email": email, "expires_at_millis": millis, "team_id": TEAM}


def test_links_open_on_the_client_domain_when_set():
    with patch.object(inv, "CLIENT_APP_URL", CLIENT_URL + "/"), patch.object(
        inv, "PUBLIC_BASE_URL", "https://voice-app.botrixai.com"
    ):
        assert inv.invite_callback_url() == f"{CLIENT_URL}/handler/team-invitation"


def test_links_fall_back_to_the_public_address():
    with patch.object(inv, "CLIENT_APP_URL", None), patch.object(
        inv, "PUBLIC_BASE_URL", "https://voice-app.botrixai.com"
    ):
        assert inv.invite_callback_url() == "https://voice-app.botrixai.com/handler/team-invitation"


@pytest.mark.parametrize(
    "bad", ["", "not-an-email", "a@b", "a b@c.com", "x" * 250 + "@a.com"]
)
def test_bad_emails_are_refused(bad):
    with pytest.raises(inv.InvitationError):
        inv.normalize_email(bad)


@pytest.mark.asyncio
async def test_resending_replaces_the_dead_invitation():
    fake, state = _stack(
        [_pending("old-1", "rahulkpandey2003@gmail.com"), _pending("x", "flamepersonal1310@gmail.com")]
    )
    with patch.object(inv, "stackauth", fake), patch.object(inv, "CLIENT_APP_URL", CLIENT_URL):
        result = await inv.send_invitation(TEAM, "  RahulKPandey2003@gmail.com ")
    assert state["revoked"] == ["old-1"]  # only that person's old link
    assert state["sent"] == [
        (TEAM, "rahulkpandey2003@gmail.com", f"{CLIENT_URL}/handler/team-invitation")
    ]
    assert result["link_opens_at"].startswith(CLIENT_URL)


@pytest.mark.asyncio
async def test_listing_shows_email_and_expiry():
    fake, _ = _stack(
        [_pending("a", "one@x.com", 1791788672230), _pending("b", "two@x.com", 1791986269269)]
    )
    with patch.object(inv, "stackauth", fake):
        invitations = await inv.list_invitations(TEAM)
    assert [i["email"] for i in invitations] == ["two@x.com", "one@x.com"]
    assert invitations[0]["expires_at"].startswith("2026-")


@pytest.mark.asyncio
async def test_revoking_only_a_pending_invitation():
    fake, state = _stack([_pending("a", "one@x.com")])
    with patch.object(inv, "stackauth", fake):
        await inv.revoke_invitation(TEAM, "a")
        with pytest.raises(inv.InvitationError):
            await inv.revoke_invitation(TEAM, "a")  # already gone
    assert state["revoked"] == ["a"]


@pytest.mark.asyncio
async def test_the_endpoints_end_to_end():
    from api.app import app
    from api.services.auth.stack_auth import StackAuthTeamError

    fake, state = _stack([_pending("old-1", "kailash.pant@think-gas.com")])
    org = SimpleNamespace(id=5, provider_id=TEAM)

    async def org_by_id(organization_id):
        return org if organization_id == 5 else None

    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer t"}
    base = "/api/v1/superuser/organizations"
    with patch("api.services.auth.depends.get_user", AsyncMock(return_value=SUPERUSER)), patch.object(
        inv, "stackauth", fake
    ), patch.object(inv, "CLIENT_APP_URL", CLIENT_URL), patch(
        "api.routes.superuser.db_client.get_organization_by_id", org_by_id
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get(f"{base}/5/invitations", headers=headers)
            sent = await client.post(
                f"{base}/5/invitations", json={"email": "kailash.pant@think-gas.com"}, headers=headers
            )
            bad = await client.post(f"{base}/5/invitations", json={"email": "nope"}, headers=headers)
            missing_org = await client.get(f"{base}/999/invitations", headers=headers)
            gone = await client.delete(f"{base}/5/invitations/old-1", headers=headers)

            async def refuse(*args, **kwargs):
                raise StackAuthTeamError(
                    "Stack Auth team invitation failed (400): callback URL is not a trusted domain"
                )

            fake.send_team_invitation = refuse
            refused = await client.post(
                f"{base}/5/invitations", json={"email": "new@think-gas.com"}, headers=headers
            )

    assert listed.status_code == 200
    assert listed.json()["invitations"][0]["email"] == "kailash.pant@think-gas.com"
    assert listed.json()["link_opens_at"] == f"{CLIENT_URL}/handler/team-invitation"
    assert sent.status_code == 200 and state["revoked"] == ["old-1"]
    assert bad.status_code == 400
    assert missing_org.status_code == 404
    assert gone.status_code == 404  # replaced by the resend, so no longer pending
    # The provider's reason reaches the superadmin.
    assert refused.status_code == 502 and "trusted domain" in refused.json()["detail"]
