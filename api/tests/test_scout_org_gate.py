"""Scout's per-organization gate.

Scout (the in-editor AI assistant) is off for every organization until a
superadmin turns it on. Two independent conditions have to hold before a user
can reach it: the deployment must carry assistant credentials at all, and the
signed-in org must be switched on. These tests pin both halves, and pin that
the *route* enforces them — hiding the button in the UI is presentation, not
access control.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.enums import OrganizationConfigurationKey
from api.routes.workflow_gen_chat import _require_scout_enabled
from api.services.organization_context import (
    get_organization_context,
    is_scout_enabled_for_organization,
    is_scout_enabled_in_configuration,
    scout_enabled_from_configuration_value,
    set_scout_enabled_for_organization,
)

ORG_ID = 7


def _user(organization_id=ORG_ID):
    user = MagicMock()
    user.selected_organization_id = organization_id
    return user


def _patch_db(value):
    """Patch the org-configuration read that backs the Scout flag."""
    db = MagicMock()
    db.get_configuration_value = AsyncMock(return_value=value)
    db.upsert_configuration = AsyncMock()
    return patch("api.services.organization_context.db_client", db), db


# --------------------------------------------------------------------------
# The stored value
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"enabled": False},
        {"enabled": None},
        {"something_else": True},
        "true",  # a string, not the dict we write
        [],
        True,  # a bare bool, not the dict we write
    ],
)
def test_unrecognised_configuration_values_mean_off(value):
    """Anything we didn't write ourselves reads as off.

    Defaulting an unparseable value to *on* would hand Scout to an org because
    of a malformed row, which is exactly the accident this gate exists to stop.
    """
    assert scout_enabled_from_configuration_value(value) is False


def test_enabled_true_is_the_only_on_state():
    assert scout_enabled_from_configuration_value({"enabled": True}) is True


# --------------------------------------------------------------------------
# is_scout_enabled_for_organization — both gates
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_organization_means_off():
    with patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=True,
    ):
        assert await is_scout_enabled_for_organization(None) is False


@pytest.mark.asyncio
async def test_unconfigured_deployment_overrides_an_enabled_org():
    """An org switched on but a deployment with no assistant credentials is
    still off — there is nothing for the assistant to talk to."""
    patcher, db = _patch_db({"enabled": True})
    with patcher, patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=False,
    ):
        assert await is_scout_enabled_for_organization(ORG_ID) is False
    # And it short-circuits: no pointless DB read on an unconfigured box.
    db.get_configuration_value.assert_not_called()


@pytest.mark.asyncio
async def test_configured_deployment_with_no_org_setting_is_off():
    patcher, _ = _patch_db(None)
    with patcher, patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=True,
    ):
        assert await is_scout_enabled_for_organization(ORG_ID) is False


@pytest.mark.asyncio
async def test_both_gates_open_is_on():
    patcher, db = _patch_db({"enabled": True})
    with patcher, patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=True,
    ):
        assert await is_scout_enabled_for_organization(ORG_ID) is True
    db.get_configuration_value.assert_awaited_once_with(
        ORG_ID, OrganizationConfigurationKey.SCOUT_ENABLED.value
    )


@pytest.mark.asyncio
async def test_org_half_ignores_deployment_configuration():
    """What the superadmin panel shows is the decision that was made, not the
    combined gate — otherwise the switch would read 'off' on a deployment
    missing credentials and a superadmin would flip it pointlessly."""
    patcher, _ = _patch_db({"enabled": True})
    with patcher, patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=False,
    ):
        assert await is_scout_enabled_in_configuration(ORG_ID) is True


# --------------------------------------------------------------------------
# Writing the flag
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_set_writes_the_canonical_shape(enabled):
    patcher, db = _patch_db(None)
    with patcher:
        assert await set_scout_enabled_for_organization(ORG_ID, enabled) is enabled
    db.upsert_configuration.assert_awaited_once_with(
        ORG_ID,
        OrganizationConfigurationKey.SCOUT_ENABLED.value,
        {"enabled": enabled},
    )


# --------------------------------------------------------------------------
# The flag reaches the client through /organizations/context
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [({"enabled": True}, True), (None, False)])
async def test_organization_context_carries_the_flag(stored, expected):
    db = MagicMock()
    db.get_configuration_value = AsyncMock(return_value=stored)
    db.get_organization_by_id = AsyncMock(
        return_value=MagicMock(provider_id="team_abc")
    )

    resolved = MagicMock()
    resolved.source = "organization_v2"
    resolved.effective.managed_service_version = 2

    with patch("api.services.organization_context.db_client", db), patch(
        "api.services.organization_context.is_workflow_gen_configured",
        return_value=True,
    ), patch(
        "api.services.organization_context.get_resolved_ai_model_configuration",
        AsyncMock(return_value=resolved),
    ):
        context = await get_organization_context(_user())

    assert context.scout_enabled is expected


@pytest.mark.asyncio
async def test_organization_context_defaults_to_off():
    """The Pydantic default matters on its own: a response built without the
    field must never claim Scout is available."""
    from api.services.organization_context import (
        OrganizationContextResponse,
        OrganizationModelServicesContext,
    )

    response = OrganizationContextResponse(
        model_services=OrganizationModelServicesContext(
            config_source="empty",
            has_model_configuration_v2=False,
            uses_managed_service_v2=False,
        )
    )
    assert response.scout_enabled is False


# --------------------------------------------------------------------------
# The route gate — the one that actually protects anything
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_rejects_unconfigured_deployment_with_503():
    with patch(
        "api.routes.workflow_gen_chat.is_workflow_gen_configured", return_value=False
    ):
        with pytest.raises(HTTPException) as exc:
            await _require_scout_enabled(_user())
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_route_rejects_a_disabled_organization_with_403():
    """The UI hides the button, but the endpoint is the control: an org that
    was never switched on must be refused even if it calls the URL directly."""
    with patch(
        "api.routes.workflow_gen_chat.is_workflow_gen_configured", return_value=True
    ), patch(
        "api.routes.workflow_gen_chat.is_scout_enabled_for_organization",
        AsyncMock(return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await _require_scout_enabled(_user())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_route_allows_an_enabled_organization():
    with patch(
        "api.routes.workflow_gen_chat.is_workflow_gen_configured", return_value=True
    ), patch(
        "api.routes.workflow_gen_chat.is_scout_enabled_for_organization",
        AsyncMock(return_value=True),
    ) as gate:
        await _require_scout_enabled(_user())
    gate.assert_awaited_once_with(ORG_ID)


# --------------------------------------------------------------------------
# The superadmin switch
# --------------------------------------------------------------------------


def _organization(org_id=ORG_ID):
    from datetime import datetime

    org = MagicMock()
    org.id = org_id
    org.provider_id = "team_abc"
    org.name = "Acme"
    org.primary_contact_email = "ops@acme.test"
    org.status = "active"
    org.created_at = datetime(2026, 1, 1)
    return org


@pytest.mark.asyncio
async def test_superadmin_list_reports_each_org_setting():
    """One batched read, not one per row — and an org with no row at all shows
    as off rather than being dropped from the listing."""
    from api.routes.superuser import list_organizations

    db = MagicMock()
    db.list_organizations_for_superadmin = AsyncMock(
        return_value=[
            {
                "id": 1,
                "provider_id": "team_1",
                "name": "Enabled Co",
                "primary_contact_email": None,
                "status": "active",
                "created_at": _organization().created_at,
                "user_count": 3,
            },
            {
                "id": 2,
                "provider_id": "team_2",
                "name": "Never Considered Co",
                "primary_contact_email": None,
                "status": "active",
                "created_at": _organization().created_at,
                "user_count": 1,
            },
            {
                "id": 3,
                "provider_id": "team_3",
                "name": "Turned Off Co",
                "primary_contact_email": None,
                "status": "active",
                "created_at": _organization().created_at,
                "user_count": 1,
            },
        ]
    )
    db.get_all_configurations_by_key = AsyncMock(
        return_value=[
            {"organization_id": 1, "value": {"enabled": True}},
            {"organization_id": 3, "value": {"enabled": False}},
        ]
    )

    with patch("api.routes.superuser.db_client", db):
        response = await list_organizations(user=_user())

    assert [o.scout_enabled for o in response.organizations] == [True, False, False]
    db.get_all_configurations_by_key.assert_awaited_once_with(
        OrganizationConfigurationKey.SCOUT_ENABLED.value
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_superadmin_toggle_persists_and_echoes_the_new_state(enabled):
    from api.routes.superuser import UpdateOrganizationScoutRequest, update_organization_scout

    db = MagicMock()
    db.get_organization_by_id = AsyncMock(return_value=_organization())
    db.get_organization_users = AsyncMock(return_value=[object(), object()])

    setter = AsyncMock(return_value=enabled)
    with patch("api.routes.superuser.db_client", db), patch(
        "api.routes.superuser.set_scout_enabled_for_organization", setter
    ):
        response = await update_organization_scout(
            ORG_ID, UpdateOrganizationScoutRequest(enabled=enabled), user=_user()
        )

    setter.assert_awaited_once_with(ORG_ID, enabled)
    assert response.scout_enabled is enabled
    assert response.id == ORG_ID
    assert response.user_count == 2


@pytest.mark.asyncio
async def test_superadmin_toggle_on_a_missing_org_is_404_and_writes_nothing():
    from api.routes.superuser import UpdateOrganizationScoutRequest, update_organization_scout

    db = MagicMock()
    db.get_organization_by_id = AsyncMock(return_value=None)

    setter = AsyncMock()
    with patch("api.routes.superuser.db_client", db), patch(
        "api.routes.superuser.set_scout_enabled_for_organization", setter
    ):
        with pytest.raises(HTTPException) as exc:
            await update_organization_scout(
                999, UpdateOrganizationScoutRequest(enabled=True), user=_user()
            )

    assert exc.value.status_code == 404
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_chat_route_is_gated():
    """A new endpoint added to this router without the gate is the obvious way
    this protection regresses, so assert the gate by inspection rather than
    trusting that each handler was remembered."""
    import inspect

    from api.routes import workflow_gen_chat

    source = inspect.getsource(workflow_gen_chat)
    handlers = [
        "create_workflow_gen_session",
        "ensure_workflow_gen_session_for_workflow",
        "list_workflow_gen_sessions",
        "get_workflow_gen_session",
        "append_workflow_gen_message",
        "confirm_workflow_gen_action",
    ]
    for name in handlers:
        start = source.index(f"async def {name}(")
        # The gate must appear before the next handler definition.
        rest = source[start:]
        end = rest.find("\n@router.", 1)
        body = rest if end == -1 else rest[:end]
        assert "_require_scout_enabled(user)" in body, (
            f"{name} does not call _require_scout_enabled — every workflow-gen "
            "route must enforce the org gate server-side."
        )
