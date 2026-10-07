"""Inviting people into an existing organization (superadmin).

An organization is a Stack team, so an invitation is a Stack team invitation:
Stack emails a link to ``callback_url`` + a code, and accepting it adds the
person to the team (Dograh then adds them to the organization on their first
request). Invites were only ever sent while creating an organization, from
PUBLIC_BASE_URL, so emails sent before a server move kept pointing at the old
server with no way to send a fresh one.
"""

import re
from datetime import UTC, datetime
from typing import Any, Optional

from api.constants import CLIENT_APP_URL, PUBLIC_BASE_URL, UI_APP_URL
from api.services.auth.stack_auth import stackauth

INVITATION_PATH = "/handler/team-invitation"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvitationError(ValueError):
    """A request the caller can fix (bad email, unknown invitation)."""


def invite_callback_url() -> str:
    """Where an invitation link opens: the client-dashboard domain when set."""
    base = CLIENT_APP_URL or PUBLIC_BASE_URL or UI_APP_URL
    return f"{base.rstrip('/')}{INVITATION_PATH}"


def normalize_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if len(cleaned) > 254 or not _EMAIL.match(cleaned):
        raise InvitationError("Enter a valid email address.")
    return cleaned


def _expires_at(item: dict[str, Any]) -> Optional[str]:
    millis = item.get("expires_at_millis")
    if not isinstance(millis, (int, float)):
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()


async def list_invitations(team_id: str) -> list[dict[str, Any]]:
    """Pending invitations, newest expiry first."""
    items = await stackauth.list_team_invitations(team_id)
    invitations = [
        {
            "id": str(item.get("id")),
            "email": item.get("recipient_email"),
            "expires_at": _expires_at(item),
        }
        for item in items
        if item.get("id")
    ]
    invitations.sort(key=lambda i: i["expires_at"] or "", reverse=True)
    return invitations


async def send_invitation(team_id: str, email: str) -> dict[str, Any]:
    """Email a fresh invitation. Sending to someone already invited replaces
    their pending invitation, so an old link (e.g. one pointing at a retired
    server) stops working and only the new one is live."""
    address = normalize_email(email)
    for existing in await list_invitations(team_id):
        if (existing["email"] or "").lower() == address:
            await stackauth.revoke_team_invitation(team_id, existing["id"])
    callback_url = invite_callback_url()
    await stackauth.send_team_invitation(
        team_id=team_id, email=address, callback_url=callback_url
    )
    return {"email": address, "link_opens_at": callback_url}


async def revoke_invitation(team_id: str, invitation_id: str) -> None:
    pending = {i["id"] for i in await list_invitations(team_id)}
    if invitation_id not in pending:
        raise InvitationError("That invitation is no longer pending.")
    await stackauth.revoke_team_invitation(team_id, invitation_id)
