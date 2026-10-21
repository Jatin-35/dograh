"""Soniox settings, saved through the real API and read by calls.

Saves Soniox the ways a user can (organization model settings, a per-agent v2
override, the legacy per-agent overlay that the override form sends), reads
them back, then builds the call's STT the way a call does and checks the
configuration message Soniox would receive.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.routes.organization import router as organization_router
from api.routes.workflow import router as workflow_router
from api.services.auth.depends import get_user
from api.services.configuration.ai_model_configuration import (
    WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY,
    get_effective_ai_model_configuration_for_workflow,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.pipecat.service_factory import create_stt_service, stt_uses_external_turns
from api.tests.integrations._run_pipeline_helpers import create_workflow_run_rows
from api.tests.integrations.test_run_pipeline_turn_taking import _definition

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


def _soniox(**extra) -> dict:
    return {"provider": "soniox", "api_key": "sk-soniox-test", "model": "stt-rt-v5", **extra}


def _pipeline(stt: dict) -> dict:
    return {
        "mode": "byok",
        "byok": {
            "mode": "pipeline",
            "pipeline": {
                "llm": {"provider": "openai", "api_key": "sk-llm", "model": "gpt-4.1"},
                "tts": {"provider": "cartesia", "api_key": "ct", "model": "sonic-2", "voice": "v"},
                "stt": stt,
            },
        },
    }


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
        workflow_definition=_definition(),
        name_prefix=f"Soniox Settings {suffix}",
        provider_id_suffix=f"soniox-settings-{suffix}",
    )


async def _draft_configs(db_session, workflow, user):
    """The agent's saved configurations, as a call on its draft reads them."""
    run = await db_session.create_workflow_run(
        name="soniox settings check",
        workflow_id=workflow.id,
        mode="smallwebrtc",
        user_id=user.id,
        use_draft=True,
        organization_id=workflow.organization_id,
    )
    run = await db_session.get_workflow_run(run.id, organization_id=workflow.organization_id)
    return run.definition.workflow_configurations or {}


async def _soniox_config_for(organization_id, workflow_configurations, *, keyterms=None):
    """The configuration message Soniox would receive on this agent's calls."""
    effective = await get_effective_ai_model_configuration_for_workflow(
        organization_id=organization_id,
        workflow_configurations=workflow_configurations,
    )
    assert effective.stt.provider == "soniox", effective.stt.provider
    svc = create_stt_service(
        effective,
        SimpleNamespace(transport_in_sample_rate=8000),
        keyterms=keyterms,
        call_reference="run-1",
    )
    svc._sample_rate = 8000

    class _Socket:
        sent = []

        async def send(self, data):
            self.sent.append(data)

    socket = _Socket()
    connect = AsyncMock(return_value=socket)
    with patch("pipecat.services.soniox.stt.websocket_connect", connect):
        await svc._connect_websocket()
    config = json.loads(socket.sent[0])
    # The key goes in the header, as Soniox requires from January 2027.
    config["_auth_header"] = connect.await_args.kwargs["additional_headers"]["Authorization"]
    return config, stt_uses_external_turns(effective)


# ── Organization model settings ──────────────────────────────────────────


async def test_org_settings_save_read_back_and_reach_soniox(db_session, async_session, no_key_check):
    _, user, _ = await _rows(db_session, async_session, "org")
    resp = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_pipeline(
            _soniox(
                languages=["mr", "hi", "en"],
                strict_languages=True,
                domain_context="Think Gas PNG customer support",
            )
        ),
    )
    assert resp.status_code == 200, resp.text

    got = await _call(user, "GET", "/organizations/model-configurations/v2")
    assert got.status_code == 200, got.text
    stt = got.json()["configuration"]["byok"]["pipeline"]["stt"]
    assert stt["provider"] == "soniox"
    assert stt["languages"] == ["mr", "hi", "en"]
    assert stt["strict_languages"] is True
    assert stt["domain_context"] == "Think Gas PNG customer support"
    assert "sk-soniox-test" not in got.text  # the key comes back masked

    config, external = await _soniox_config_for(
        user.selected_organization_id, {}, keyterms=["Think Gas", "BP number"]
    )
    assert config["_auth_header"] == "Bearer sk-soniox-test"
    assert "api_key" not in config
    assert sorted(config["language_hints"]) == ["en", "hi", "mr"]  # Soniox hints are a set
    assert config["language_hints_strict"] is True
    assert config["context"]["general"] == [{"key": "domain", "value": "Think Gas PNG customer support"}]
    assert config["context"]["terms"] == ["Think Gas", "BP number"]
    assert config["sample_rate"] == 8000
    assert config["enable_endpoint_detection"] is False
    assert external is False


async def test_what_the_form_sends_with_turn_tuning_left_blank(db_session, async_session, no_key_check):
    _, user, _ = await _rows(db_session, async_session, "org-blank")
    # The form sends null for emptied number boxes and "" for an empty text box.
    resp = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_pipeline(
            _soniox(
                languages=["hi", "en"],
                strict_languages=False,
                domain_context="",
                use_soniox_turn_detection=True,
                max_endpoint_delay_ms=None,
                endpoint_sensitivity=None,
                endpoint_speed_level=None,
            )
        ),
    )
    assert resp.status_code == 200, resp.text
    config, external = await _soniox_config_for(user.selected_organization_id, {})
    assert config["context"] is None
    assert config["enable_endpoint_detection"] is True
    assert config["max_endpoint_delay_ms"] is None
    assert external is True


async def test_turn_tuning_saved_and_sent(db_session, async_session, no_key_check):
    _, user, _ = await _rows(db_session, async_session, "org-tuning")
    resp = await _call(
        user,
        "PUT",
        "/organizations/model-configurations/v2",
        json=_pipeline(
            _soniox(
                use_soniox_turn_detection=True,
                max_endpoint_delay_ms=1200,
                endpoint_sensitivity=0.25,
                endpoint_speed_level=2,
            )
        ),
    )
    assert resp.status_code == 200, resp.text
    config, _ = await _soniox_config_for(user.selected_organization_id, {})
    assert config["max_endpoint_delay_ms"] == 1200
    assert config["endpoint_sensitivity"] == 0.25
    assert config["endpoint_latency_adjustment_level"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"languages": ["hindi"]},
        {"strict_languages": True, "languages": []},
        {"max_endpoint_delay_ms": 100},
        {"endpoint_speed_level": 9},
        {"domain_context": "x" * 8001},
    ],
)
async def test_bad_values_are_refused_and_the_old_ones_kept(db_session, async_session, no_key_check, bad):
    _, user, _ = await _rows(db_session, async_session, f"org-bad-{len(json.dumps(bad))}")
    good = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_pipeline(_soniox(languages=["hi"]))
    )
    assert good.status_code == 200, good.text

    resp = await _call(user, "PUT", "/organizations/model-configurations/v2", json=_pipeline(_soniox(**bad)))
    assert resp.status_code == 422, resp.text

    config, _ = await _soniox_config_for(user.selected_organization_id, {})
    assert config["language_hints"] == ["hi"]


# ── Per-agent settings ────────────────────────────────────────────────────


async def test_agent_v2_override_reaches_its_calls_only(db_session, async_session, no_key_check):
    _, user, workflow = await _rows(db_session, async_session, "agent")
    org_id = user.selected_organization_id
    org = await _call(
        user, "PUT", "/organizations/model-configurations/v2", json=_pipeline(_soniox(languages=["en"]))
    )
    assert org.status_code == 200, org.text

    override = {WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY: _pipeline(_soniox(languages=["gu", "hi"]))}
    resp = await _call(user, "PUT", f"/workflow/{workflow.id}", json={"workflow_configurations": override})
    assert resp.status_code == 200, resp.text

    saved = await _draft_configs(db_session, workflow, user)
    assert saved[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY]["byok"]["pipeline"]["stt"]["languages"] == ["gu", "hi"]
    agent_config, _ = await _soniox_config_for(org_id, saved)
    assert sorted(agent_config["language_hints"]) == ["gu", "hi"]
    org_config, _ = await _soniox_config_for(org_id, {})
    assert org_config["language_hints"] == ["en"]


async def test_override_form_switching_an_agent_to_soniox(db_session, async_session, no_key_check):
    """The override form sends `model_overrides`; an agent on Deepgram moves to Soniox."""
    _, user, workflow = await _rows(db_session, async_session, "overlay")
    resp = await _call(
        user,
        "PUT",
        f"/workflow/{workflow.id}",
        json={
            "workflow_configurations": {
                "model_overrides": {"stt": _soniox(languages=["hi", "en"], domain_context="Vectus tanks")}
            }
        },
    )
    assert resp.status_code == 200, resp.text
    saved = await _draft_configs(db_session, workflow, user)
    # The API converts the form's model_overrides to the per-agent v2 override.
    stored = saved[WORKFLOW_MODEL_CONFIGURATION_V2_OVERRIDE_KEY]["byok"]["pipeline"]["stt"]
    assert (stored["provider"], stored["languages"]) == ("soniox", ["hi", "en"])
    config, _ = await _soniox_config_for(user.selected_organization_id, saved)
    assert sorted(config["language_hints"]) == ["en", "hi"]
    assert config["context"]["general"] == [{"key": "domain", "value": "Vectus tanks"}]
