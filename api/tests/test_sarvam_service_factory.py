from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger
from pipecat.services.sarvam.llm import SarvamLLMService as RealSarvamLLMService
from pipecat.transcriptions.language import Language

from api.services.configuration.registry import (
    SarvamLLMConfiguration,
    SarvamTTSConfiguration,
    ServiceProviders,
)
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.sarvam_llm import DograhSarvamLLMService
from api.services.pipecat.service_factory import (
    create_llm_service,
    create_llm_service_from_provider,
    create_stt_service,
    create_tts_service,
)


class TestSarvamLLMConfiguration:
    def test_default_values(self):
        config = SarvamLLMConfiguration(api_key="test-key")
        assert config.provider == ServiceProviders.SARVAM
        assert config.model == "sarvam-105b"
        assert config.base_url == "https://api.sarvam.ai/v1"
        assert config.temperature == 0.5

    def test_custom_model(self):
        config = SarvamLLMConfiguration(
            api_key="test-key", model="sarvam-105b-conversations"
        )
        assert config.model == "sarvam-105b-conversations"

    def test_the_dropdown_offers_only_models_the_service_accepts(self):
        """A listed model the service rejects fails every call at start."""
        schema = SarvamLLMConfiguration.model_json_schema()["properties"]["model"]
        assert schema["examples"] == ["sarvam-105b", "sarvam-105b-conversations"]
        assert "sarvam-30b" not in schema["examples"]  # withdrawn by Sarvam
        for model in schema["examples"]:
            assert model in DograhSarvamLLMService._SUPPORTED_MODELS

    def test_a_saved_config_with_a_withdrawn_model_still_loads(self):
        config = SarvamLLMConfiguration(api_key="test-key", model="sarvam-30b")
        assert config.model == "sarvam-30b"  # kept; replaced when the call runs


class TestSarvamLLMServiceFactory:
    def test_create_sarvam_llm_service(self):
        with patch(
            "api.services.pipecat.service_factory.DograhSarvamLLMService"
        ) as mock_service:
            mock_service.Settings = RealSarvamLLMService.Settings
            create_llm_service_from_provider(
                provider=ServiceProviders.SARVAM.value,
                model="sarvam-105b",
                api_key="test-key",
            )

        assert mock_service.call_count == 1
        kwargs = mock_service.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["settings"].model == "sarvam-105b"
        assert kwargs["settings"].temperature == 0.5
        assert kwargs["base_url"] == "https://api.sarvam.ai/v1"

    @pytest.mark.parametrize("model", ["sarvam-105b", "sarvam-105b-conversations"])
    def test_real_sarvam_llm_service_instantiation(self, model):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.SARVAM.value,
            model=model,
            api_key="test-key",
        )
        assert isinstance(service, RealSarvamLLMService)
        assert service._settings.model == model
        assert str(service._client.base_url).rstrip("/") == "https://api.sarvam.ai/v1"

    def test_real_sarvam_llm_service_instantiation_with_custom_base_url(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.SARVAM.value,
            model="sarvam-105b-conversations",
            api_key="test-key",
            base_url="https://custom.sarvam.ai/v1",
        )
        assert str(service._client.base_url).rstrip("/") == "https://custom.sarvam.ai/v1"

    @pytest.mark.parametrize("withdrawn", ["sarvam-30b", "sarvam-30b-16k"])
    def test_a_withdrawn_model_runs_as_sarvam_105b(self, withdrawn):
        """Agents saved with sarvam-30b keep working instead of failing."""
        lines = []
        sink = logger.add(lines.append, format="{message}")
        try:
            service = create_llm_service_from_provider(
                provider=ServiceProviders.SARVAM.value,
                model=withdrawn,
                api_key="test-key",
            )
        finally:
            logger.remove(sink)
        assert service._settings.model == "sarvam-105b"
        assert any(withdrawn in line and "withdrawn" in line for line in lines)

    def test_an_unknown_model_is_still_refused(self):
        with pytest.raises(ValueError, match="Unsupported Sarvam LLM model"):
            create_llm_service_from_provider(
                provider=ServiceProviders.SARVAM.value,
                model="sarvam-999b",
                api_key="test-key",
            )

    def test_the_conversations_model_never_gets_reasoning_options(self):
        """sarvam-105b-conversations rejects reasoning_effort and wiki_grounding."""
        service = DograhSarvamLLMService(
            api_key="test-key",
            settings=DograhSarvamLLMService.Settings(
                model="sarvam-105b-conversations",
                reasoning_effort="low",
                wiki_grounding=True,
            ),
        )
        params = service.build_chat_completion_params(
            {"messages": [{"role": "user", "content": "namaste"}]}
        )
        assert params["model"] == "sarvam-105b-conversations"
        assert "reasoning_effort" not in params and "wiki_grounding" not in params

    def test_sarvam_105b_keeps_its_options(self):
        service = DograhSarvamLLMService(
            api_key="test-key",
            settings=DograhSarvamLLMService.Settings(
                model="sarvam-105b", reasoning_effort="low"
            ),
        )
        params = service.build_chat_completion_params(
            {"messages": [{"role": "user", "content": "namaste"}]}
        )
        assert params["reasoning_effort"] == "low"

    def test_the_sarvam_auth_header_is_sent(self):
        service = create_llm_service_from_provider(
            provider=ServiceProviders.SARVAM.value,
            model="sarvam-105b-conversations",
            api_key="test-key",
        )
        assert service._client.default_headers["api-subscription-key"] == "test-key"

    def test_create_sarvam_llm_service_passes_user_temperature(self):
        with patch(
            "api.services.pipecat.service_factory.DograhSarvamLLMService"
        ) as mock_service:
            mock_service.Settings = RealSarvamLLMService.Settings
            create_llm_service_from_provider(
                provider=ServiceProviders.SARVAM.value,
                model="sarvam-105b",
                api_key="test-key",
                temperature=0.8,
            )

        kwargs = mock_service.call_args.kwargs
        assert kwargs["settings"].temperature == 0.8

    def test_create_llm_service_extracts_sarvam_config(self):
        user_config = SimpleNamespace(
            llm=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                model="sarvam-105b-conversations",
                api_key="test-key",
                base_url="https://api.sarvam.ai/v1",
                temperature=0.7,
            )
        )

        with patch(
            "api.services.pipecat.service_factory.DograhSarvamLLMService"
        ) as mock_service:
            mock_service.Settings = RealSarvamLLMService.Settings
            create_llm_service(user_config)

        kwargs = mock_service.call_args.kwargs
        assert kwargs["base_url"] == "https://api.sarvam.ai/v1"
        assert kwargs["settings"].model == "sarvam-105b-conversations"
        assert kwargs["settings"].temperature == 0.7

    def test_a_config_saved_before_base_url_existed_still_works(self):
        user_config = SimpleNamespace(
            llm=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                model="sarvam-30b",
                api_key="test-key",
                temperature=0.5,
            )
        )
        service = create_llm_service(user_config)
        assert service._settings.model == "sarvam-105b"
        assert str(service._client.base_url).rstrip("/") == "https://api.sarvam.ai/v1"


class TestSarvamSTTServiceFactory:
    @pytest.mark.parametrize(
        "input_language,expected_language",
        [
            ("unknown", None),
            (None, None),
            ("hi-IN", Language.HI_IN),
            ("ne-IN", "ne-IN"),
        ],
    )
    def test_stt_language_mapping(self, input_language, expected_language):
        user_config = SimpleNamespace(
            stt=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                model="saaras:v3",
                api_key="test-key",
                language=input_language,
            )
        )
        audio_config = AudioConfig(
            transport_in_sample_rate=16000, transport_out_sample_rate=16000
        )

        with patch(
            "api.services.pipecat.service_factory.DograhSarvamSTTService"
        ) as mock_service:
            create_stt_service(user_config, audio_config)

        kwargs = mock_service.call_args.kwargs
        assert kwargs["settings"].language == expected_language


class TestSarvamTTSServiceFactory:
    def test_sarvam_tts_configuration_defaults(self):
        config = SarvamTTSConfiguration(api_key="test-key")

        assert config.provider == ServiceProviders.SARVAM
        assert config.model == "bulbul:v2"
        assert config.voice == "anushka"
        assert config.language == "hi-IN"
        assert config.speed == 1.0

    def test_sarvam_tts_voice_schema_allows_custom_model_specific_options(self):
        voice_schema = SarvamTTSConfiguration.model_json_schema()["properties"]["voice"]

        assert voice_schema["allow_custom_input"] is True
        assert "bulbul:v2" in voice_schema["model_options"]
        assert "bulbul:v3" in voice_schema["model_options"]

    def test_create_sarvam_tts_service_maps_speed_to_pace(self):
        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                api_key="test-key",
                model="bulbul:v2",
                voice="anushka",
                language="hi-IN",
                speed=1.25,
            )
        )
        audio_config = AudioConfig(
            transport_in_sample_rate=16000, transport_out_sample_rate=16000
        )

        with patch(
            "api.services.pipecat.service_factory.SarvamTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        kwargs = mock_service.call_args.kwargs
        assert kwargs["api_key"] == "test-key"
        assert kwargs["settings"].model == "bulbul:v2"
        assert kwargs["settings"].voice == "anushka"
        assert kwargs["settings"].language == Language.HI
        assert kwargs["settings"].pace == 1.25

    def test_create_sarvam_tts_service_normalizes_custom_voice_id(self):
        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                api_key="test-key",
                model="bulbul:v2",
                voice=" Rehan ",
                language="hi-IN",
                speed=1.0,
            )
        )
        audio_config = AudioConfig(
            transport_in_sample_rate=16000, transport_out_sample_rate=16000
        )

        with patch(
            "api.services.pipecat.service_factory.SarvamTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        kwargs = mock_service.call_args.kwargs
        assert kwargs["settings"].voice == "rehan"

    def test_create_sarvam_tts_service_defaults_blank_voice_id(self):
        user_config = SimpleNamespace(
            tts=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                api_key="test-key",
                model="bulbul:v2",
                voice="   ",
                language="hi-IN",
                speed=1.0,
            )
        )
        audio_config = AudioConfig(
            transport_in_sample_rate=16000, transport_out_sample_rate=16000
        )

        with patch(
            "api.services.pipecat.service_factory.SarvamTTSService"
        ) as mock_service:
            create_tts_service(user_config, audio_config)

        kwargs = mock_service.call_args.kwargs
        assert kwargs["settings"].voice == "anushka"
