from typing import Literal, Optional

from pydantic import BaseModel

from api.db import db_client
from api.db.models import UserModel
from api.enums import OrganizationConfigurationKey
from api.services.configuration.ai_model_configuration import (
    get_resolved_ai_model_configuration,
)
from api.services.workflow_gen.config import is_workflow_gen_configured


class OrganizationModelServicesContext(BaseModel):
    config_source: Literal["organization_v2", "legacy_user_v1", "empty"]
    has_model_configuration_v2: bool
    managed_service_version: Optional[int] = None
    uses_managed_service_v2: bool


class OrganizationContextResponse(BaseModel):
    organization_id: Optional[int] = None
    organization_provider_id: Optional[str] = None
    model_services: OrganizationModelServicesContext
    # Whether this organization may use Scout, the in-editor AI assistant.
    # Both conditions must hold: the deployment has assistant credentials, and
    # a superadmin has turned it on for this org. Off unless explicitly
    # enabled, so a new client never gets it by default.
    scout_enabled: bool = False


async def is_scout_enabled_for_organization(organization_id: Optional[int]) -> bool:
    """Whether Scout is available to this organization.

    Two independent gates, both required. The deployment must have assistant
    credentials at all (there is nothing to talk to otherwise), and a
    superadmin must have switched it on for this org. The org gate defaults
    to off: a client that has never been considered for Scout doesn't get it
    by accident.
    """
    if organization_id is None or not is_workflow_gen_configured():
        return False
    return await is_scout_enabled_in_configuration(organization_id)


async def is_scout_enabled_in_configuration(organization_id: int) -> bool:
    """The org-level half of the gate on its own, ignoring deployment config.

    This is what a superadmin toggled. The superadmin panel shows *this*, so
    the switch reflects the decision that was made rather than flipping back
    to off on a deployment that happens to lack assistant credentials.
    """
    value = await db_client.get_configuration_value(
        organization_id, OrganizationConfigurationKey.SCOUT_ENABLED.value
    )
    return scout_enabled_from_configuration_value(value)


def scout_enabled_from_configuration_value(value: object) -> bool:
    """Read the stored SCOUT_ENABLED value. Anything unrecognised means off."""
    if not isinstance(value, dict):
        return False
    return bool(value.get("enabled", False))


async def set_scout_enabled_for_organization(
    organization_id: int, enabled: bool
) -> bool:
    """Turn Scout on or off for one organization. Returns the stored state."""
    await db_client.upsert_configuration(
        organization_id,
        OrganizationConfigurationKey.SCOUT_ENABLED.value,
        {"enabled": bool(enabled)},
    )
    return bool(enabled)


async def get_organization_context(user: UserModel) -> OrganizationContextResponse:
    organization_id = user.selected_organization_id
    organization = (
        await db_client.get_organization_by_id(organization_id)
        if organization_id
        else None
    )

    resolved = await get_resolved_ai_model_configuration(
        organization_id=organization_id,
    )
    managed_service_version = resolved.effective.managed_service_version

    return OrganizationContextResponse(
        organization_id=organization_id,
        organization_provider_id=organization.provider_id if organization else None,
        scout_enabled=await is_scout_enabled_for_organization(organization_id),
        model_services=OrganizationModelServicesContext(
            config_source=resolved.source,
            has_model_configuration_v2=resolved.source == "organization_v2",
            managed_service_version=managed_service_version,
            uses_managed_service_v2=(
                resolved.source == "organization_v2" and managed_service_version == 2
            ),
        ),
    )
