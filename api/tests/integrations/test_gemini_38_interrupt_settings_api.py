"""Gemini 3.8 barge-in settings, saved through the real API and read by calls.

interrupt_sensitivity / interrupt_min_speech_ms let short backchannels
("mhm", "haan") stop cutting the bot off. These tests save them the ways a
user can (organization model settings, a per-agent v2 override, the legacy
per-agent overlay), then build the call's Gemini service the way a call does
and check what Google is actually asked for.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from google.genai.types import StartSensitivity

from api.routes.organization import router as organization_router
from api.routes.workflow import router as workflow_router
from api.services.auth.depends import get_user
from api.services.configuration.ai_model_configuration import (
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
    get_effective_ai_model_configuration_for_workflow,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.pipecat.service_factory import create_realtime_llm_service
from api.tests.integrations._run_pipeline_helpers import create_workflow_run_rows

from .test_run_pipeline_greeting_interrupt import _definition

pytestmark = pytest.mark.asyncio


def _app(user):
    app = FastAPI()
    app.include_router(organization_router)
    app.include_router(workflow_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=user.id,
        is_superuser=False,
        selected_organization_id=user.selected_organization_id,
        provider_id=user.provider_id,
    )
    return app


async def _call(user, method, path, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(user))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


def _gemini_38(**extra) -> dict:
    return {
        "provider": "gemini_live_38",
        "api_key": "gemini-test-key",
        "model": "gemini-3.8-live",
        "voice": "Callirrhoe",
        "language": "en-IN",
        **extra,
    }


def _byok_realtime(**extra) -> dict:
    return {
        "mode": "byok",
        "byok": {
            "mode": "realtime",
            "realtime": {
                "realtime": _gemini_38(**extra),
                "llm": {
                    "provider": "google",
                    "api_key": "google-llm-key",
                    "model": "gemini-2.5-flash",
                },
            },
        },
    }


async def _detection_for(organization_id, workflow_configurations):
    """What the call's Gemini session would ask Google for, or None."""
    effective = await get_effective_ai_model_configuration_for_workflow(
        organization_id=organization_id,
        workflow_configurations=workflow_configurations,
    )
    service = create_realtime_llm_service(effective, None)
    with (
        patch.object(service, "_connection_task_handler", MagicMock()) as handler,
        patch.object(service, "create_task", MagicMock()),
        patch.object(service, "push_error", AsyncMock()) as push_error,
    ):
        await service._connect()
    push_error.assert_not_awaited()
    config = handler.call_args.kwargs["config"]
    if config.realtime_input_config is None:
        return None
    return config.realtime_input_config.automatic_activity_detection


@pytest.fixture
def no_key_check():
    # Saving validates the API keys against the providers; the keys here are fake.
    with patch.object(
        UserConfigurationValidator,
        "validate",
        AsyncMock(return_value={"status": [{"model": "all", "message": "ok"}]}),
    ):
        yield


async def _rows(db_session, async_session, suffix):
    return await create_workflow_run_rows(
        db_session,
        async_session,
        workflow_definition=_definition(False),
        name_prefix=f"Interrupt Settings {suffix}",
        provider_id_suffix=f"interrupt-settings-{suffix}",
    )


async def _draft_configs(db_session, workflow, user):
    run = await db_session.create_workflow_run(
        name="interrupt settings check",
        workflow_id=workflow.id,
        mode="smallwebrtc",
        user_id=user.id,
        use_draft=True,
        organization_id=workflow.organization_id,
    )
    run = await db_session.get_workflow_run(
        run.id, organization_id=workflow.organization_id
    )
    return run.definition.workflow_configurations or {}


# ── Organization model settings ──────────────────────────────────────────


async def test_org_settings_save_read_back_and_reach_google(
    db_session, async_session, no_key_check
):
    _, user, _ = await _rows(db_session, async_session, "org")
    org_id = user.selected_organization_id

    resp = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_byok_realtime(interrupt_sensitivity="low", interrupt_min_speech_ms=400),
    )
    assert resp.status_code == 200, resp.text

    got = await _call(user, "GET", "/organizations/model-configurations/v2")
    assert got.status_code == 200, got.text
    saved = got.text
    assert '"interrupt_sensitivity":"low"' in saved
    assert '"interrupt_min_speech_ms":400' in saved
    # The key comes back masked, never in full.
    assert "gemini-test-key" not in saved

    detection = await _detection_for(org_id, {})
    assert detection.start_of_speech_sensitivity == StartSensitivity.START_SENSITIVITY_LOW
    assert detection.prefix_padding_ms == 400
    assert not detection.disabled


async def test_org_settings_with_the_form_left_blank_send_nothing(
    db_session, async_session, no_key_check
):
    _, user, _ = await _rows(db_session, async_session, "org-blank")
    # What the settings form sends when the boxes are untouched.
    resp = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_byok_realtime(interrupt_sensitivity="default", interrupt_min_speech_ms=""),
    )
    assert resp.status_code == 200, resp.text
    assert await _detection_for(user.selected_organization_id, {}) is None


async def test_org_settings_turned_back_off_stop_reaching_google(
    db_session, async_session, no_key_check
):
    _, user, _ = await _rows(db_session, async_session, "org-off")
    org_id = user.selected_organization_id
    on = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_byok_realtime(interrupt_sensitivity="low", interrupt_min_speech_ms=400),
    )
    assert on.status_code == 200, on.text
    assert (await _detection_for(org_id, {})) is not None

    off = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_byok_realtime(interrupt_sensitivity="default", interrupt_min_speech_ms=None),
    )
    assert off.status_code == 200, off.text
    assert await _detection_for(org_id, {}) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"interrupt_sensitivity": "high"},
        {"interrupt_sensitivity": "LOW"},
        {"interrupt_min_speech_ms": 50},
        {"interrupt_min_speech_ms": 5000},
        {"interrupt_min_speech_ms": "abc"},
    ],
)
async def test_org_settings_refuse_bad_values_and_keep_the_old_ones(
    db_session, async_session, no_key_check, bad
):
    _, user, _ = await _rows(db_session, async_session, f"org-bad-{len(str(bad))}")
    org_id = user.selected_organization_id
    good = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_byok_realtime(interrupt_sensitivity="low", interrupt_min_speech_ms=400),
    )
    assert good.status_code == 200, good.text

    resp = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_byok_realtime(**bad)
    )
    assert resp.status_code == 422, resp.text

    detection = await _detection_for(org_id, {})
    assert detection.prefix_padding_ms == 400


# ── Per-agent v2 override (the agent's own model settings) ───────────────


async def test_agent_override_saved_via_workflow_api_reaches_its_calls_only(
    db_session, async_session, no_key_check
):
    _, user, workflow = await _rows(db_session, async_session, "agent")
    org_id = user.selected_organization_id
    # Organization default: no tuning.
    org = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_byok_realtime()
    )
    assert org.status_code == 200, org.text

    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_configurations": {
                WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: _byok_realtime(
                    interrupt_sensitivity="low", interrupt_min_speech_ms=450
                )
            }
        },
    )
    assert resp.status_code == 200, resp.text

    configs = await _draft_configs(db_session, workflow, user)
    stored = configs[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY]
    realtime = stored["byok"]["realtime"]["realtime"]
    assert realtime["interrupt_sensitivity"] == "low"
    assert realtime["interrupt_min_speech_ms"] == 450

    detection = await _detection_for(org_id, configs)
    assert detection.start_of_speech_sensitivity == StartSensitivity.START_SENSITIVITY_LOW
    assert detection.prefix_padding_ms == 450

    # Other agents in the organization keep Google's defaults.
    assert await _detection_for(org_id, {}) is None


async def test_agent_override_keeps_the_turn_end_silence_alongside(
    db_session, async_session, no_key_check
):
    _, user, workflow = await _rows(db_session, async_session, "agent-all")
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_configurations": {
                WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: _byok_realtime(
                    turn_end_silence_ms=700,
                    interrupt_sensitivity="low",
                    interrupt_min_speech_ms=400,
                )
            }
        },
    )
    assert resp.status_code == 200, resp.text
    configs = await _draft_configs(db_session, workflow, user)
    detection = await _detection_for(user.selected_organization_id, configs)
    assert detection.silence_duration_ms == 700
    assert detection.start_of_speech_sensitivity == StartSensitivity.START_SENSITIVITY_LOW
    assert detection.prefix_padding_ms == 400


async def test_agent_override_with_a_bad_value_is_refused(
    db_session, async_session, no_key_check
):
    _, user, workflow = await _rows(db_session, async_session, "agent-bad")
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_configurations": {
                WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: _byok_realtime(
                    interrupt_min_speech_ms=5000
                )
            }
        },
    )
    assert resp.status_code == 422, resp.text


async def test_agent_override_cleared_drops_the_keys(
    db_session, async_session, no_key_check
):
    _, user, workflow = await _rows(db_session, async_session, "agent-clear")
    for extra in (
        {"interrupt_sensitivity": "low", "interrupt_min_speech_ms": 400},
        {"interrupt_sensitivity": "default", "interrupt_min_speech_ms": None},
    ):
        resp = await _call(
            user,
            "PUT",
            f"/workflow/{workflow.id}",
            json={
                "workflow_configurations": {
                    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: _byok_realtime(**extra)
                }
            },
        )
        assert resp.status_code == 200, resp.text
    configs = await _draft_configs(db_session, workflow, user)
    realtime = configs[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY]["byok"]["realtime"][
        "realtime"
    ]
    assert "interrupt_min_speech_ms" not in realtime
    assert await _detection_for(user.selected_organization_id, configs) is None


# ── Legacy per-agent overlay (model_overrides, merged without validation) ─


async def test_legacy_overlay_turns_the_tuning_on(
    db_session, async_session, no_key_check
):
    _, user, _ = await _rows(db_session, async_session, "legacy")
    org_id = user.selected_organization_id
    org = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_byok_realtime()
    )
    assert org.status_code == 200, org.text
    detection = await _detection_for(
        org_id,
        {
            "model_overrides": {
                "realtime": {"interrupt_sensitivity": "low", "interrupt_min_speech_ms": 400}
            }
        },
    )
    assert detection.start_of_speech_sensitivity == StartSensitivity.START_SENSITIVITY_LOW
    assert detection.prefix_padding_ms == 400


@pytest.mark.parametrize(
    "overlay",
    [
        {"interrupt_min_speech_ms": ""},
        {"interrupt_min_speech_ms": "abc"},
        {"interrupt_min_speech_ms": 5000},
        {"interrupt_sensitivity": "LOW"},
        {"interrupt_sensitivity": None},
        {"turn_end_silence_ms": "abc"},
        {"turn_end_silence_ms": 50},
    ],
)
async def test_legacy_overlay_bad_values_never_break_the_call(
    db_session, async_session, no_key_check, overlay
):
    """The legacy overlay is merged unvalidated; a bad value must fall back to
    Google's default rather than fail the call at connect time."""
    _, user, _ = await _rows(db_session, async_session, f"legacy-bad-{len(str(overlay))}")
    org_id = user.selected_organization_id
    org = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_byok_realtime()
    )
    assert org.status_code == 200, org.text
    detection = await _detection_for(org_id, {"model_overrides": {"realtime": overlay}})
    assert detection is None


async def test_legacy_overlay_numbers_typed_as_text_still_work(
    db_session, async_session, no_key_check
):
    _, user, _ = await _rows(db_session, async_session, "legacy-text")
    org = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_byok_realtime()
    )
    assert org.status_code == 200, org.text
    detection = await _detection_for(
        user.selected_organization_id,
        {
            "model_overrides": {
                "realtime": {"interrupt_min_speech_ms": "400", "turn_end_silence_ms": "700"}
            }
        },
    )
    assert detection.prefix_padding_ms == 400
    assert detection.silence_duration_ms == 700
