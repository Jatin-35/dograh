"""Tests for the Inception (Mercury) LLM provider."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from pipecat.frames.frames import LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.inception.llm import InceptionLLMService
from pydantic import TypeAdapter, ValidationError

from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.registry import (
    INCEPTION_API_BASE_URL,
    InceptionLLMConfiguration,
    LLMConfig,
    ServiceProviders,
)
from api.services.pipecat.service_factory import (
    create_llm_service,
    create_llm_service_from_provider,
    inception_reasoning_effort,
    llm_provider_kwargs,
)

# ── Settings / dropdown ───────────────────────────────────────────────


def test_inception_is_an_llm_provider_with_fast_defaults():
    cfg = TypeAdapter(LLMConfig).validate_python(
        {"provider": "inception", "api_key": "sk-inc-test"}
    )
    assert isinstance(cfg, InceptionLLMConfiguration)
    assert cfg.model == "mercury-3"
    assert cfg.reasoning_effort == "instant"
    assert cfg.base_url == INCEPTION_API_BASE_URL == "https://api.inceptionlabs.ai/v1"
    schema = InceptionLLMConfiguration.model_json_schema()
    assert schema["properties"]["model"]["examples"] == [
        "mercury-3",
        "mercury-2.5",
        "mercury-2",
    ]
    assert schema["properties"]["reasoning_effort"]["enum"] == [
        "instant",
        "low",
        "medium",
        "high",
    ]


def test_inception_accepts_a_custom_model_id_for_mercury_voice():
    cfg = InceptionLLMConfiguration(api_key="k", model="mercury-voice")
    assert cfg.model == "mercury-voice"


def test_inception_rejects_an_unknown_reasoning_effort():
    with pytest.raises(ValidationError):
        InceptionLLMConfiguration(api_key="k", reasoning_effort="none")


# ── Reasoning effort ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,saved,sent",
    [
        ("mercury-3", None, "instant"),
        ("mercury-3", "instant", "instant"),
        ("mercury-2.5", "medium", "medium"),
        ("mercury-voice", "instant", "low"),  # Voice has no "instant"
        ("Mercury-Voice-1", None, "low"),
        ("mercury-voice", "high", "high"),
    ],
)
def test_reasoning_effort_sent_for_each_model(model, saved, sent):
    assert inception_reasoning_effort(model, saved) == sent


# ── Factory ───────────────────────────────────────────────────────────


def test_factory_builds_pipecats_inception_service():
    svc = create_llm_service_from_provider(
        ServiceProviders.INCEPTION.value, "mercury-3", "sk-inc-test"
    )
    assert isinstance(svc, InceptionLLMService)
    assert svc._settings.model == "mercury-3"
    assert svc._settings.reasoning_effort == "instant"
    assert svc._settings.realtime is True
    assert str(svc._client.base_url).rstrip("/") == INCEPTION_API_BASE_URL
    assert svc.supports_developer_role is False


def test_saved_settings_reach_the_service_for_calls_and_qa():
    cfg = InceptionLLMConfiguration(
        api_key="k",
        model="mercury-2.5",
        reasoning_effort="low",
        base_url="https://dedicated.inceptionlabs.ai/v1",
    )
    kwargs = llm_provider_kwargs("inception", lambda name: getattr(cfg, name, None))
    assert kwargs == {
        "base_url": "https://dedicated.inceptionlabs.ai/v1",
        "reasoning_effort": "low",
    }

    class _UserConfig:
        llm = cfg

    svc = create_llm_service(_UserConfig())
    assert svc._settings.model == "mercury-2.5"
    assert svc._settings.reasoning_effort == "low"
    assert str(svc._client.base_url).startswith("https://dedicated.inceptionlabs.ai/v1")


@pytest.mark.parametrize("api_key", [None, ""])
def test_a_missing_key_never_falls_back_to_the_servers_openai_key(api_key, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-our-own-openai-key")
    with pytest.raises(Exception, match="Inception needs an API key"):
        create_llm_service_from_provider("inception", "mercury-3", api_key)


def test_the_base_url_goes_through_the_shared_url_check():
    # The check only blocks in hosted (non-OSS) mode; it is the same one every
    # other provider's base_url goes through.
    with patch("api.utils.url_security.DEPLOYMENT_MODE", "saas"):
        with pytest.raises(Exception, match="base_url"):
            create_llm_service_from_provider(
                "inception", "mercury-3", "k", base_url="http://localhost:9000/v1"
            )


# ── The request that goes over the wire ───────────────────────────────


def _mock_inception(svc, handler):
    """Point the service's OpenAI client at an in-process fake Inception API."""
    svc._client = openai.AsyncOpenAI(
        api_key="sk-inc-test",
        base_url=INCEPTION_API_BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def _context():
    return LLMContext(
        messages=[
            {"role": "system", "content": "You are Priya from Vectus. Reply in Hinglish."},
            {"role": "user", "content": "Mujhe 1000 litre ka tank chahiye"},
        ]
    )


@pytest.mark.asyncio
async def test_request_body_has_realtime_and_effort_but_no_temperature():
    svc = create_llm_service_from_provider("inception", "mercury-3", "sk-inc-test")
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "mercury-3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Ji bilkul!"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    _mock_inception(svc, handler)
    reply = await svc.run_inference(_context())

    assert reply == "Ji bilkul!"
    assert seen["url"] == "https://api.inceptionlabs.ai/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-inc-test"
    body = seen["body"]
    assert body["model"] == "mercury-3"
    assert body["reasoning_effort"] == "instant"
    assert body["realtime"] is True
    assert "temperature" not in body  # Mercury rejects < 0.5
    assert all(m["role"] != "developer" for m in body["messages"])


def _sse(*chunks):
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def _chunk(delta, finish=None):
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mercury-3",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


@pytest.mark.asyncio
async def test_streamed_reply_and_tool_call_are_handled():
    """A live turn: Mercury streams a short sentence then calls a tool."""
    svc = create_llm_service_from_provider("inception", "mercury-3", "sk-inc-test")
    bodies = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                _chunk({"role": "assistant", "content": "Ek second, "}),
                _chunk({"content": "main check karti hoon."}),
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "vectus_area_manager_lookup",
                                    "arguments": '{"city": "Jai',
                                },
                            }
                        ]
                    }
                ),
                _chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": 'pur"}'}}]},
                    finish="tool_calls",
                ),
            ),
        )

    _mock_inception(svc, handler)
    pushed = []

    async def capture(frame, *args, **kwargs):
        pushed.append(frame)

    context = _context()
    with (
        patch.object(svc, "push_frame", side_effect=capture),
        patch.object(svc, "run_function_calls", AsyncMock()) as run_calls,
    ):
        await svc._process_context(context)

    text = "".join(f.text for f in pushed if isinstance(f, LLMTextFrame))
    assert text == "Ek second, main check karti hoon."
    [calls] = run_calls.await_args.args
    assert [(c.function_name, c.arguments, c.tool_call_id) for c in calls] == [
        ("vectus_area_manager_lookup", {"city": "Jaipur"}, "call_1")
    ]
    assert bodies[0]["stream"] is True and bodies[0]["realtime"] is True


# ── Key check on save ─────────────────────────────────────────────────


def _validate(api_key, model_list_status, chat_status=200):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            if model_list_status == 200:
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(model_list_status, json={"error": {"message": "x"}})
        return httpx.Response(
            chat_status,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "mercury-3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    real_client = openai.OpenAI

    def fake_client(**kwargs):
        return real_client(
            **kwargs,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )

    cfg = InceptionLLMConfiguration(api_key=api_key)
    with patch("api.services.configuration.check_validity.openai.OpenAI", fake_client):
        result = UserConfigurationValidator()._check_api_key("inception", api_key, cfg)
    return result, requests


def test_a_good_key_passes_the_save_check():
    ok, requests = _validate("sk-good", 200)
    assert ok is True
    assert str(requests[0].url) == "https://api.inceptionlabs.ai/v1/models"


def test_a_bad_key_is_refused_on_save_naming_inception():
    with pytest.raises(ValueError, match="Invalid Inception API key"):
        _validate("sk-bad", 401)


def test_a_missing_models_endpoint_falls_back_to_a_tiny_chat():
    ok, requests = _validate("sk-good", 404)
    assert ok is True
    assert requests[-1].url.path.endswith("/chat/completions")
