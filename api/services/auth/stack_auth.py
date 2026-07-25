import os
from typing import Any

import aiohttp
from loguru import logger


class StackAuthUserSearchError(Exception):
    """Raised when Stack Auth user search fails unexpectedly."""


class StackAuthSessionError(Exception):
    """Raised when Stack Auth cannot create an impersonation session."""


class StackAuthTeamError(Exception):
    """Raised when Stack Auth team creation or invitation fails."""


class StackAuth:
    def __init__(self):
        self.project_id = os.environ.get("STACK_AUTH_PROJECT_ID")
        self.secret_server_key = os.environ.get("STACK_SECRET_SERVER_KEY")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _strip_bearer(self, access_token: str | None) -> str | None:
        """Remove the leading "Bearer " prefix from the token if present."""
        if not access_token:
            return None
        if access_token.startswith("Bearer "):
            return access_token.split(" ", 1)[1]
        return access_token

    async def get_user(self, access_token: str):
        if not access_token:
            return None

        access_token = self._strip_bearer(access_token)

        url = os.environ.get("STACK_AUTH_API_URL") + "/api/v1/users/me"
        headers = {
            "x-stack-access-type": "server",
            "x-stack-project-id": self.project_id,
            "x-stack-secret-server-key": self.secret_server_key,
            "x-stack-access-token": access_token,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response = await response.json()
                if "id" in response:
                    return response
                else:
                    return None

    async def impersonate(self, stack_user_id: str):
        url = os.environ.get("STACK_AUTH_API_URL") + "/api/v1/auth/sessions"
        headers = {
            "x-stack-access-type": "server",
            "x-stack-project-id": self.project_id,
            "x-stack-secret-server-key": self.secret_server_key,
        }

        data = {
            "user_id": stack_user_id,
            "expires_in_millis": 3600000,
            "is_impersonation": True,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status >= 400:
                        raise StackAuthSessionError(
                            "Stack Auth session creation failed"
                        )

                    return await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise StackAuthSessionError("Stack Auth session creation failed") from exc

    async def find_users_by_email(self, email: str) -> list[dict[str, Any]]:
        """Return Stack Auth users whose primary email exactly matches."""
        normalized_email = email.strip().lower()
        url = os.environ.get("STACK_AUTH_API_URL") + "/api/v1/users"
        headers = {
            "x-stack-access-type": "server",
            "x-stack-project-id": self.project_id,
            "x-stack-secret-server-key": self.secret_server_key,
        }
        params = {
            "query": normalized_email,
            "limit": "10",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status >= 400:
                        raise StackAuthUserSearchError("Stack Auth user search failed")

                    payload = await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise StackAuthUserSearchError("Stack Auth user search failed") from exc

        users = payload.get("items", []) if isinstance(payload, dict) else []
        if not isinstance(users, list):
            return []

        return [
            user
            for user in users
            if isinstance(user, dict)
            and self._stack_user_has_email(user, normalized_email)
        ]

    def _stack_user_has_email(self, user: dict[str, Any], email: str) -> bool:
        primary_email = user.get("primary_email")
        return isinstance(primary_email, str) and primary_email.lower() == email

    # ------------------------------------------------------------------
    # Team & user management helpers
    # ------------------------------------------------------------------

    def _server_headers(self) -> dict[str, str]:
        return {
            "x-stack-access-type": "server",
            "x-stack-project-id": self.project_id,
            "x-stack-secret-server-key": self.secret_server_key,
            "Content-Type": "application/json",
        }

    async def create_team(
        self,
        display_name: str,
        creator_user_id: str | None = None,
        client_metadata: dict | None = None,
    ) -> dict:
        """Create a team server-side and return the API response (includes ``id``).

        Uses server auth, so Stack skips the client-only
        ``allowClientTeamCreation`` check and no user access token is required.
        When ``creator_user_id`` (a Stack user UUID) is supplied that user is
        added to the team as a member — used to add the superadmin so they can
        build the client's workflows before the client accepts their invite.
        """
        url = os.environ.get("STACK_AUTH_API_URL") + "/api/v1/teams"
        payload: dict = {"display_name": display_name}
        if creator_user_id is not None:
            payload["creator_user_id"] = creator_user_id
        if client_metadata is not None:
            payload["client_metadata"] = client_metadata

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self._server_headers(), json=payload
                ) as response:
                    if response.status >= 400:
                        raise StackAuthTeamError(
                            f"Stack Auth team creation failed ({response.status})"
                        )
                    data = await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise StackAuthTeamError("Stack Auth team creation failed") from exc

        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise StackAuthTeamError("Stack Auth team creation returned no team id")
        return data

    async def set_user_selected_team(self, user_id: str, team_id: str) -> dict:
        """Force a user's currently-selected team to ``team_id`` (server auth).

        Stack validates real membership before allowing this (a user can't be
        pointed at a team they don't belong to). Used before impersonating so
        the resulting session reliably lands in the intended organization
        instead of inheriting whatever the target's account last had selected.
        """
        url = os.environ.get("STACK_AUTH_API_URL") + f"/api/v1/users/{user_id}"
        payload = {"selected_team_id": team_id}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.patch(
                    url, headers=self._server_headers(), json=payload
                ) as response:
                    if response.status >= 400:
                        body = await response.text()
                        logger.warning(
                            "Stack Auth set_user_selected_team failed ({}): {}",
                            response.status,
                            body,
                        )
                        raise StackAuthTeamError(
                            f"Stack Auth set_user_selected_team failed ({response.status}): {body}"
                        )
                    return await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise StackAuthTeamError("Stack Auth set_user_selected_team failed") from exc

    async def send_team_invitation(
        self,
        team_id: str,
        email: str,
        callback_url: str,
    ) -> dict:
        """Email a team invitation link to ``email`` for the given team.

        Server auth skips the ``$invite_members`` permission check (that check
        only applies to client-auth callers), so this works without any inviting
        user context.
        """
        url = (
            os.environ.get("STACK_AUTH_API_URL")
            + "/api/v1/team-invitations/send-code"
        )
        payload = {
            "team_id": team_id,
            "email": email,
            "callback_url": callback_url,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self._server_headers(), json=payload
                ) as response:
                    if response.status >= 400:
                        # Capture the response body so the real reason (untrusted
                        # callback domain, email not configured, etc.) is visible
                        # in logs instead of a bare status code.
                        body = await response.text()
                        logger.warning(
                            "Stack Auth team invitation failed ({}): {}",
                            response.status,
                            body,
                        )
                        raise StackAuthTeamError(
                            f"Stack Auth team invitation failed ({response.status}): {body}"
                        )
                    return await response.json()
        except (aiohttp.ClientError, ValueError) as exc:
            raise StackAuthTeamError("Stack Auth team invitation failed") from exc


stackauth = StackAuth()
