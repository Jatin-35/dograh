"""Shared credential creation, used by both the REST route and the
in-product AI assistant.

Mirrors `api/services/tool_management.py`: the rules for what a valid
credential looks like live here rather than in the route, so every caller
validates identically instead of each reimplementing it.

Note on storage: `credential_data` is persisted as plain JSON — nothing in
this codebase encrypts it. Callers must therefore never echo a stored secret
back to a caller or into a chat transcript; return only the identifying
fields (see `credential_summary`).
"""

from typing import Any

from api.db import db_client
from api.db.models import ExternalCredentialModel, UserModel
from api.enums import WebhookCredentialType

# Required `credential_data` keys per type. These key names are not cosmetic:
# `api/utils/credential_auth.py` reads them with `.get(key, "")`, so a
# misspelled key silently produces an empty secret and an unauthenticated
# request rather than an error.
REQUIRED_CREDENTIAL_FIELDS: dict[WebhookCredentialType, tuple[str, ...]] = {
    WebhookCredentialType.NONE: (),
    WebhookCredentialType.API_KEY: ("header_name", "api_key"),
    WebhookCredentialType.BEARER_TOKEN: ("token",),
    WebhookCredentialType.BASIC_AUTH: ("username", "password"),
    WebhookCredentialType.CUSTOM_HEADER: ("header_name", "header_value"),
}


class CredentialManagementError(Exception):
    """A credential couldn't be created, for a reason the caller can act on."""

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def missing_credential_fields(
    credential_type: WebhookCredentialType, credential_data: dict
) -> list[str]:
    """Which required keys for this credential type are absent."""
    required = REQUIRED_CREDENTIAL_FIELDS.get(credential_type, ())
    return [field for field in required if field not in (credential_data or {})]


def validate_credential_data(
    credential_type: WebhookCredentialType, credential_data: dict
) -> None:
    """Raise `CredentialManagementError` if required fields are missing."""
    missing = missing_credential_fields(credential_type, credential_data)
    if missing:
        raise CredentialManagementError(
            "invalid_credential_data",
            f"{credential_type.value} credentials require: {', '.join(missing)}.",
        )


def credential_summary(credential: ExternalCredentialModel) -> dict[str, Any]:
    """The safe-to-return view of a credential — never its secret."""
    return {
        "credential_uuid": credential.credential_uuid,
        "name": credential.name,
        "credential_type": credential.credential_type,
    }


async def create_credential_for_user(
    *,
    user: UserModel,
    name: str,
    credential_type: WebhookCredentialType,
    credential_data: dict,
    description: str | None = None,
) -> ExternalCredentialModel:
    """Validate and persist a credential owned by the user's organization."""
    if not user.selected_organization_id:
        raise CredentialManagementError(
            "no_organization", "No organization selected for the user."
        )

    validate_credential_data(credential_type, credential_data)

    try:
        return await db_client.create_credential(
            organization_id=user.selected_organization_id,
            user_id=user.id,
            name=name,
            description=description,
            credential_type=credential_type.value,
            credential_data=credential_data,
        )
    except Exception as e:
        # The DB enforces one credential name per organization.
        if "unique_org_credential_name" in str(e):
            raise CredentialManagementError(
                "duplicate_name",
                f"A credential named '{name}' already exists — pick a different name.",
                status_code=409,
            ) from e
        raise
