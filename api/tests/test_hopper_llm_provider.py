"""Hopper (Gemma 4 31B) as an LLM provider, ported from upstream dograh
33798b83 (#831).

Hopper serves an OpenAI-compatible API at a fixed endpoint, so it is built on
pipecat's OpenAILLMService and validated with the OpenAI client pointed there.
The first six tests are upstream's; the rest cover this fork's paths (QA,
voicemail and variable extraction, which build the LLM from a provider name;
the model settings form; and OpenAI behaving as before).
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter

from api.services.configuration import check_validity
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    HOPPER_API_BASE_URL,
    REGISTRY,
    HopperLLMConfiguration,
    LLMConfig,
    OpenAILLMService,
    ServiceProviders,
    ServiceType,
)
from api.services.pipecat.service_factory import (
    create_llm_service,
    create_llm_service_from_provider,
)


def test_hopper_llm_configuration_defaults_and_registry():
    config = HopperLLMConfiguration(api_key="sk_hopper_test")

    assert config.provider == ServiceProviders.HOPPER
    assert config.model == "gemma-4-31b"
    assert config.base_url == HOPPER_API_BASE_URL  # editable, Hopper's own by default
    assert REGISTRY[ServiceType.LLM][ServiceProviders.HOPPER] is HopperLLMConfiguration


def test_hopper_llm_discriminator_parses_llm_config():
    config = TypeAdapter(LLMConfig).validate_python(
        {
            "provider": "hopper",
            "api_key": "sk_hopper_test",
            "model": "gemma-4-31b",
        }
    )

    assert isinstance(config, HopperLLMConfiguration)
    assert config.model == "gemma-4-31b"


def test_create_hopper_llm_service_uses_openai_service_at_hopper_endpoint():
    user_config = SimpleNamespace(llm=HopperLLMConfiguration(api_key="sk_hopper_test"))

    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service(user_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "sk_hopper_test"
    assert kwargs["base_url"] == HOPPER_API_BASE_URL
    assert kwargs["settings"].model == "gemma-4-31b"
    assert kwargs["settings"].temperature == 0.1


def test_create_hopper_llm_service_uses_a_custom_endpoint():
    """A dedicated or self-hosted Hopper endpoint set in the settings form."""
    custom = "https://dedicated.withhopper.com/v1"
    user_config = SimpleNamespace(
        llm=HopperLLMConfiguration(api_key="sk_hopper_test", base_url=custom)
    )
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service(user_config)

    kwargs = mock_service.call_args.kwargs
    assert kwargs["base_url"] == custom
    assert kwargs["api_key"] == "sk_hopper_test"
    assert kwargs["settings"].model == "gemma-4-31b"


def test_a_blocked_custom_endpoint_is_refused(monkeypatch):
    """In hosted mode a Hopper URL pointing inside the network is refused, as
    for every other provider's URL."""
    from fastapi import HTTPException

    from api.utils import url_security

    monkeypatch.setattr(url_security, "DEPLOYMENT_MODE", "saas")
    with patch("api.services.pipecat.service_factory.OpenAILLMService"):
        with pytest.raises(HTTPException):
            create_llm_service_from_provider(
                ServiceProviders.HOPPER.value, "gemma-4-31b", "k",
                base_url="http://localhost:8000/v1",
            )


def test_an_old_saved_config_without_base_url_still_reaches_hopper():
    """Configs saved before the field existed have no base_url key."""
    config = TypeAdapter(LLMConfig).validate_python(
        {"provider": "hopper", "api_key": "sk_old", "model": "gemma-4-31b"}
    )
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service(SimpleNamespace(llm=config))
    assert mock_service.call_args.kwargs["base_url"] == HOPPER_API_BASE_URL


def _fake_openai(monkeypatch, *, raise_on_list=None):
    captured = {}

    class FakeModels:
        def list(self):
            if raise_on_list:
                raise raise_on_list
            return []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.models = FakeModels()

    monkeypatch.setattr(check_validity.openai, "OpenAI", FakeOpenAI)
    return captured


def test_hopper_api_key_validation_lists_models_at_hopper_endpoint(monkeypatch):
    captured = _fake_openai(monkeypatch)

    config = HopperLLMConfiguration(api_key="sk_hopper_test")
    is_valid = UserConfigurationValidator()._check_api_key(
        ServiceProviders.HOPPER.value, "sk_hopper_test", config
    )

    assert is_valid is True
    assert captured == {"api_key": "sk_hopper_test", "base_url": HOPPER_API_BASE_URL}


def test_hopper_api_key_validation_reports_hopper_error(monkeypatch):
    class FakeAuthenticationError(Exception):
        pass

    _fake_openai(monkeypatch, raise_on_list=FakeAuthenticationError())
    monkeypatch.setattr(
        check_validity.openai, "AuthenticationError", FakeAuthenticationError
    )

    config = HopperLLMConfiguration(api_key="sk_hopper_bad")
    with pytest.raises(ValueError) as exc_info:
        UserConfigurationValidator()._check_api_key(
            ServiceProviders.HOPPER.value, "sk_hopper_bad", config
        )

    message = str(exc_info.value)
    assert "Invalid Hopper API key" in message
    assert "Invalid OpenAI API key" not in message


# ─── this fork's paths ─────────────────────────────────────────────────────


def test_a_real_service_object_talks_to_hopper():
    """Not mocked: the pipecat service the factory returns really points at
    Hopper, with the caller's key."""
    service = create_llm_service_from_provider(
        ServiceProviders.HOPPER.value, "gemma-4-31b", "sk_hopper_test"
    )
    client = service._client
    assert str(client.base_url).rstrip("/") == HOPPER_API_BASE_URL
    assert client.api_key == "sk_hopper_test"


def test_qa_and_voicemail_build_hopper_from_the_provider_name():
    """QA analysis, node summaries and voicemail detection build their LLM
    with create_llm_service_from_provider and no base_url; Hopper's key must
    still go to Hopper, never to OpenAI's default endpoint."""
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service_from_provider(
            provider="hopper", model="gemma-4-31b", api_key="sk_hopper_test"
        )
    assert mock_service.call_args.kwargs["base_url"] == HOPPER_API_BASE_URL


@pytest.mark.asyncio
async def test_qa_using_the_workflow_llm_resolves_hopper():
    from api.services.workflow.qa import llm_config

    async def fake_effective(**kwargs):
        return SimpleNamespace(
            model_dump=lambda exclude_none: {
                "llm": {
                    "provider": "hopper",
                    "api_key": ["sk_a", "sk_b"],  # key rotation
                    "model": "gemma-4-31b",
                }
            }
        )

    run = SimpleNamespace(
        initial_context={},
        definition=None,
        workflow=SimpleNamespace(organization_id=1, workflow_configurations={}),
    )
    with patch(
        "api.services.configuration.ai_model_configuration."
        "get_effective_ai_model_configuration_for_workflow",
        fake_effective,
    ):
        provider, model, api_key, kwargs = await llm_config.resolve_user_llm_config(run)
    # no base_url saved: None, which the factory turns into Hopper's endpoint
    assert (provider, model, kwargs) == ("hopper", "gemma-4-31b", {"base_url": None})
    assert api_key in ("sk_a", "sk_b")


def test_several_keys_are_accepted_for_rotation():
    """Like every provider: both keys are stored, and each use picks one."""
    config = HopperLLMConfiguration(api_key=["sk_a", "sk_b"])
    assert config.model_dump()["api_key"] == ["sk_a", "sk_b"]
    assert {config.api_key for _ in range(50)} == {"sk_a", "sk_b"}


def test_the_settings_form_gets_a_create_a_key_link_and_the_model_list():
    schema = HopperLLMConfiguration.model_json_schema()
    assert schema["title"] == "Hopper"
    assert schema["provider_docs_url"] == "https://docs.withhopper.com"
    api_key = schema["properties"]["api_key"]
    assert api_key["docs_url"] == "https://withhopper.com/console/keys"
    assert api_key["docs_label"] == "Create a key"
    model = schema["properties"]["model"]
    assert model["examples"] == ["gemma-4-31b"] and model["allow_custom_input"] is True
    base_url = schema["properties"]["base_url"]  # shown in the form, like OpenAI's
    assert base_url["default"] == HOPPER_API_BASE_URL


def test_saving_a_hopper_config_checks_the_key_and_reports_a_bad_one(monkeypatch):
    validator = UserConfigurationValidator()
    _fake_openai(monkeypatch)
    assert (
        validator._validate_service(HopperLLMConfiguration(api_key="sk_good"), "llm")
        == []
    )

    class FakeAuthenticationError(Exception):
        pass

    _fake_openai(monkeypatch, raise_on_list=FakeAuthenticationError())
    monkeypatch.setattr(
        check_validity.openai, "AuthenticationError", FakeAuthenticationError
    )
    statuses = validator._validate_service(
        HopperLLMConfiguration(api_key="sk_bad"), "llm"
    )
    assert statuses and "Invalid Hopper API key" in statuses[0]["message"]


def test_openai_is_unchanged(monkeypatch):
    captured = _fake_openai(monkeypatch)
    UserConfigurationValidator()._check_api_key(
        ServiceProviders.OPENAI.value,
        "sk-openai",
        OpenAILLMService(api_key="sk-openai"),
    )
    assert captured["base_url"] == "https://api.openai.com/v1"

    class FakeAuthenticationError(Exception):
        pass

    _fake_openai(monkeypatch, raise_on_list=FakeAuthenticationError())
    monkeypatch.setattr(
        check_validity.openai, "AuthenticationError", FakeAuthenticationError
    )
    with pytest.raises(ValueError, match="Invalid OpenAI API key"):
        UserConfigurationValidator()._check_api_key(
            ServiceProviders.OPENAI.value, "sk-bad", None
        )

    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_service:
        create_llm_service_from_provider(ServiceProviders.OPENAI.value, "gpt-4.1", "k")
    assert "base_url" not in mock_service.call_args.kwargs


def test_custom_endpoint_reaches_qa_and_the_key_check(monkeypatch):
    """Post-call QA and the save-time key check use the same custom endpoint
    as live calls, so the Hopper key never goes anywhere else."""
    from api.services.pipecat.service_factory import llm_provider_kwargs

    custom = "https://dedicated.withhopper.com/v1"
    saved = HopperLLMConfiguration(api_key="sk_x", base_url=custom).model_dump()
    assert llm_provider_kwargs("hopper", saved.get) == {"base_url": custom}

    captured = _fake_openai(monkeypatch)
    UserConfigurationValidator()._check_api_key(
        ServiceProviders.HOPPER.value, "sk_x",
        HopperLLMConfiguration(api_key="sk_x", base_url=custom),
    )
    assert captured == {"api_key": "sk_x", "base_url": custom}
