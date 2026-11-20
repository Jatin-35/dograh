"""Custom Tools (the function editor) can be shown or hidden per organization.

An org admin flips ``show_custom_tools`` in Settings → Preferences; the UI
reads ``custom_tools_visible`` from /organizations/context to decide whether
the navigation entry appears. Hidden is the default, and hiding is
presentation only: tools built with it keep running on calls.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.organization import save_preferences
from api.schemas.organization_preferences import OrganizationPreferences
from api.services.organization_context import get_organization_context

ORG_ID = 7


def _user():
    user = MagicMock()
    user.selected_organization_id = ORG_ID
    return user


async def _context_for(preferences: OrganizationPreferences):
    db = MagicMock()
    db.get_configuration_value = AsyncMock(return_value=None)
    db.get_organization_by_id = AsyncMock(return_value=MagicMock(provider_id="t"))
    resolved = MagicMock(source="empty")
    resolved.effective.managed_service_version = None
    with patch("api.services.organization_context.db_client", db), patch(
        "api.services.organization_context.get_resolved_ai_model_configuration",
        AsyncMock(return_value=resolved),
    ), patch(
        "api.services.organization_context.get_organization_preferences",
        AsyncMock(return_value=preferences),
    ):
        return await get_organization_context(_user())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "show,expected", [(None, False), (False, False), (True, True)]
)
async def test_context_reports_the_orgs_choice_hidden_by_default(show, expected):
    context = await _context_for(OrganizationPreferences(show_custom_tools=show))
    assert context.custom_tools_visible is expected


_PREFS = "api.services.organization_preferences"


@pytest.mark.asyncio
async def test_saving_other_preferences_keeps_the_custom_tools_choice():
    """An older Settings form that only sends the timezone must not switch
    Custom Tools back off."""
    stored = OrganizationPreferences(
        timezone="Asia/Kolkata", test_phone_number="+919018737669", show_custom_tools=True
    )
    upsert = AsyncMock()
    with patch(f"{_PREFS}.get_organization_preferences", AsyncMock(return_value=stored)), patch(
        f"{_PREFS}.upsert_organization_preferences", upsert
    ), patch(f"{_PREFS}.get_organization_preferences_response", AsyncMock()):
        await save_preferences(request=OrganizationPreferences(timezone="UTC"), user=_user())

    upsert.assert_awaited_once_with(
        ORG_ID,
        OrganizationPreferences(
            timezone="UTC", test_phone_number="+919018737669", show_custom_tools=True
        ),
    )


@pytest.mark.asyncio
async def test_a_field_sent_as_null_is_still_cleared():
    stored = OrganizationPreferences(test_phone_number="+919018737669")
    upsert = AsyncMock()
    with patch(f"{_PREFS}.get_organization_preferences", AsyncMock(return_value=stored)), patch(
        f"{_PREFS}.upsert_organization_preferences", upsert
    ), patch(f"{_PREFS}.get_organization_preferences_response", AsyncMock()):
        await save_preferences(
            request=OrganizationPreferences(test_phone_number=None, show_custom_tools=False),
            user=_user(),
        )

    saved = upsert.await_args.args[1]
    assert saved.test_phone_number is None
    assert saved.show_custom_tools is False
