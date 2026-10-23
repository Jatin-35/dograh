"""Sarvam STT kept in step with Sarvam's current release (sarvam_stt.py)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.transcriptions.language import Language

from api.services.configuration.registry import SarvamSTTConfiguration, ServiceProviders
from api.services.pipecat.audio_config import AudioConfig
from api.services.pipecat.sarvam_stt import (
    DograhSarvamSTTService,
    clean_keyterms,
    resolve_sarvam_stt_model,
)
from api.services.pipecat.service_factory import create_stt_service
from pipecat.services.sarvam.stt import SarvamSTTService, SarvamSTTSettings


def _service(model="saaras:v4", keyterms=None, language=None, mode=None):
    return DograhSarvamSTTService(
        api_key="test-key",
        settings=SarvamSTTSettings(model=model, language=language),
        sample_rate=8000,
        keyterms=keyterms,
        mode=mode,
    )


def _connect_with_mock(service):
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=MagicMock())
    connect = MagicMock(return_value=context)
    service._sarvam_client = SimpleNamespace(
        speech_to_text_streaming=SimpleNamespace(connect=connect)
    )
    service.create_task = MagicMock()
    service._create_keepalive_task = MagicMock()
    service._receive_task_handler = MagicMock()
    return connect


class TestModels:
    def test_new_agents_default_to_saaras_v4(self):
        assert SarvamSTTConfiguration(api_key="k").model == "saaras:v4"

    def test_the_dropdown_offers_only_current_models(self):
        schema = SarvamSTTConfiguration.model_json_schema()["properties"]
        assert schema["model"]["examples"] == ["saaras:v4", "saaras:v3"]
        assert set(schema["language"]["model_options"]) == {"saaras:v4", "saaras:v3"}

    def test_a_config_saved_with_a_retired_model_still_loads(self):
        config = SarvamSTTConfiguration(api_key="k", model="saarika:v2.5")
        assert config.model == "saarika:v2.5"  # replaced when the call runs

    @pytest.mark.parametrize(
        "saved,runs",
        [
            ("saarika:v2.5", "saaras:v3"),
            ("saaras:v2.5", "saaras:v3"),
            ("saaras:v3", "saaras:v3"),
            ("saaras:v4", "saaras:v4"),
            (None, "saaras:v4"),
            ("", "saaras:v4"),
        ],
    )
    def test_retired_models_move_up(self, saved, runs):
        assert resolve_sarvam_stt_model(saved) == runs

    @pytest.mark.parametrize("model", ["saaras:v4", "saaras:v3"])
    def test_the_service_accepts_every_offered_model(self, model):
        assert _service(model=model)._settings.model == model

    def test_an_unknown_model_is_still_refused(self):
        with pytest.raises(ValueError):
            _service(model="saarika:v1")


class TestFactory:
    def _create(self, model, keyterms=None, **stt):
        user_config = SimpleNamespace(
            stt=SimpleNamespace(
                provider=ServiceProviders.SARVAM.value,
                model=model,
                api_key="test-key",
                language="hi-IN",
                **stt,
            )
        )
        audio = AudioConfig(transport_in_sample_rate=8000, transport_out_sample_rate=8000)
        return create_stt_service(user_config, audio, keyterms=keyterms)

    def test_an_agent_on_a_retired_model_runs_on_saaras_v3(self):
        service = self._create("saarika:v2.5")
        assert isinstance(service, DograhSarvamSTTService)
        assert service._settings.model == "saaras:v3"

    def test_dictionary_words_become_keyterms_on_v4(self):
        service = self._create("saaras:v4", keyterms=["Vectus", "OH-Tank", "टंकी"])
        assert service._keyterms == ["Vectus", "OH-Tank", "टंकी"]

    def test_the_transcript_style_reaches_the_service(self):
        assert self._create("saaras:v4", mode="codemix")._mode == "codemix"

    def test_an_agent_saved_before_the_style_existed_transcribes(self):
        assert self._create("saaras:v4")._mode == "transcribe"


class TestTranscriptStyle:
    def test_new_agents_transcribe(self):
        assert SarvamSTTConfiguration(api_key="k").mode == "transcribe"

    def test_the_dropdown_offers_the_four_styles(self):
        schema = SarvamSTTConfiguration.model_json_schema()["properties"]["mode"]
        assert schema["enum"] == ["transcribe", "codemix", "translit", "verbatim"]
        assert schema["label"] == "Transcript style"

    def test_translate_is_not_offered(self):
        """translate turns the caller's Hindi into English; not a transcript style."""
        with pytest.raises(ValueError):
            SarvamSTTConfiguration(api_key="k", mode="translate")

    def test_a_saved_config_without_a_style_still_loads(self):
        config = SarvamSTTConfiguration.model_validate(
            {"provider": "sarvam", "api_key": "k", "model": "saaras:v4", "language": "hi-IN"}
        )
        assert config.mode == "transcribe"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["saaras:v4", "saaras:v3"])
    @pytest.mark.parametrize("mode", ["transcribe", "codemix", "translit", "verbatim"])
    async def test_the_style_is_sent_when_connecting(self, model, mode):
        service = _service(model=model, language="hi-IN", mode=mode)
        connect = _connect_with_mock(service)
        await service._connect()
        assert connect.call_args.kwargs["mode"] == mode

    @pytest.mark.asyncio
    async def test_it_travels_alongside_the_keyterms(self):
        service = _service(model="saaras:v4", keyterms=["Vectus"], mode="codemix")
        connect = _connect_with_mock(service)
        await service._connect()
        kwargs = connect.call_args.kwargs
        assert kwargs["mode"] == "codemix"
        assert json.loads(kwargs["request_options"]["additional_query_parameters"]["keyterms"]) == ["Vectus"]


class TestKeyterms:
    def test_trimmed_deduplicated_and_capped(self):
        terms = [" Vectus ", "Vectus", "", "x" * 80] + [f"term{i}" for i in range(60)]
        cleaned = clean_keyterms(terms)
        assert cleaned[0] == "Vectus"
        assert cleaned[1] == "x" * 64
        assert len(cleaned) == 50

    def test_none_when_empty(self):
        assert clean_keyterms(None) is None
        assert clean_keyterms(["  ", ""]) is None

    def test_only_sent_with_saaras_v4(self):
        assert _service(model="saaras:v3", keyterms=["Vectus"])._keyterms is None
        assert _service(model="saaras:v4", keyterms=["Vectus"])._keyterms == ["Vectus"]

    @pytest.mark.asyncio
    async def test_keyterms_travel_on_the_connection_query_string(self):
        service = _service(model="saaras:v4", keyterms=["Vectus", "टंकी"], language="hi-IN")
        service._sample_rate = 8000  # set from the StartFrame on a real call
        socket = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        connect = MagicMock(return_value=context)
        service._sarvam_client = SimpleNamespace(
            speech_to_text_streaming=SimpleNamespace(connect=connect)
        )
        service.create_task = MagicMock()
        service._create_keepalive_task = MagicMock()
        service._receive_task_handler = MagicMock()

        await service._connect()

        kwargs = connect.call_args.kwargs
        assert kwargs["model"] == "saaras:v4"
        assert kwargs["sample_rate"] == "8000"
        assert kwargs["language_code"] == "hi-IN"
        assert kwargs["flush_signal"] == "true"
        query = kwargs["request_options"]["additional_query_parameters"]
        assert json.loads(query["keyterms"]) == ["Vectus", "टंकी"]
        assert "additional_headers" in kwargs["request_options"]
        assert service._socket_client is socket

    @pytest.mark.asyncio
    async def test_no_keyterms_no_query_parameter(self):
        service = _service(model="saaras:v4")
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=MagicMock())
        connect = MagicMock(return_value=context)
        service._sarvam_client = SimpleNamespace(
            speech_to_text_streaming=SimpleNamespace(connect=connect)
        )
        service.create_task = MagicMock()
        service._create_keepalive_task = MagicMock()
        service._receive_task_handler = MagicMock()

        await service._connect()

        assert "additional_query_parameters" not in connect.call_args.kwargs["request_options"]


class TestTranscripts:
    @pytest.mark.asyncio
    async def test_every_transcript_is_marked_final(self):
        service = _service()
        frame = TranscriptionFrame("haan ji", "user", "2026-10-10T00:00:00Z", Language.HI_IN)
        assert frame.finalized is False
        with patch.object(SarvamSTTService, "push_frame", new=AsyncMock()) as pushed:
            await service.push_frame(frame)
        assert frame.finalized is True
        pushed.assert_awaited_once()

    def test_languages_map_without_guessing_hindi(self):
        service = _service()
        assert service._map_language_code_to_enum("ur-IN") == Language.UR_IN
        assert service._map_language_code_to_enum("od-IN") == Language.OR_IN
        assert service._map_language_code_to_enum("unknown") is None
        assert service._map_language_code_to_enum("sat-IN") is None
