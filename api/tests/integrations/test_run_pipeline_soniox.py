"""End-to-end: a call on the real pipeline transcribed by Soniox.

The agent's model settings pick Soniox and its Dictionary holds brand words.
The real DograhSonioxSTTService is built by the real factory; only Soniox's
websocket is replaced by an in-process fake. The tests check what Soniox is
sent (settings, the caller's audio), that its transcript reaches the LLM, and
that a Soniox account out of credit pauses the call's campaign.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.tests import ContextCapturingMockLLM, MockLLMService, MockTTSService
from pipecat.tests.mock_transport import MockTransport
from pipecat.transports.base_transport import TransportParams
from sqlalchemy import select, update
from websockets.protocol import State

from api.db.models import CampaignModel, WorkflowRunModel
from api.enums import WorkflowRunMode
from api.routes.workflow import router as workflow_router
from api.services.auth.depends import get_user
from api.services.campaign import circuit_breaker as cb_module
from api.services.pipecat import run_pipeline as run_pipeline_module
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.run_pipeline import _run_pipeline
from api.services.pipecat.soniox_stt import DograhSonioxSTTService
from api.services.pipecat.worker_runner import wait_for_pipeline_worker_started
from api.tests.integrations import _run_pipeline_helpers as helpers
from api.tests.integrations.test_run_pipeline_turn_taking import _definition, _speech_frames

HARD_TIMEOUT_SECONDS = 45.0

SONIOX = {
    "provider": "soniox",
    "model": "stt-rt-v5",
    "api_key": "sk-soniox-e2e",
    "languages": ["hi", "en"],
    "domain_context": "Think Gas PNG customer support",
}


class FakeSoniox:
    """Soniox's websocket: records what arrives and answers like Soniox.

    With ``utterance`` set it behaves like Soniox transcribing the caller:
    interim tokens once audio arrives, then, when Pipecat asks it to finalize
    (the local VAD heard the caller stop), the final tokens and ``<fin>``.
    """

    def __init__(self, utterance: list[str] | None = None):
        self.sent: list = []
        self.state = State.OPEN
        self._replies: asyncio.Queue = asyncio.Queue()
        self._utterance = utterance
        self._interim_sent = False
        self.finalize_requests = 0
        self.finalized_by_soniox = False

    @property
    def config(self) -> dict:
        return json.loads(self.sent[0])

    @property
    def audio_bytes(self) -> int:
        return sum(len(item) for item in self.sent[1:] if isinstance(item, bytes))

    def reply(self, message):
        self._replies.put_nowait(message)

    async def send(self, data):
        self.sent.append(data)
        if not self._utterance or len(self.sent) == 1:
            return  # the first message is the configuration
        if isinstance(data, bytes) and not self._interim_sent:
            self._interim_sent = True
            self.reply({"tokens": [{"text": self._utterance[0], "is_final": False, "language": "hi"}]})
        elif (
            isinstance(data, bytes)
            and self.config.get("enable_endpoint_detection")
            and self.audio_bytes >= self.config["sample_rate"] * 2 * 2
            and not self.finalized_by_soniox
        ):
            # Soniox's own endpointing: after ~2 s of audio it ends the turn.
            self.finalized_by_soniox = True
            tokens = [{"text": t, "is_final": True, "language": "hi"} for t in self._utterance]
            self.reply({"tokens": tokens + [{"text": "<end>", "is_final": True}]})
        elif data == '{"type": "finalize"}':
            self.finalize_requests += 1
            tokens = [{"text": t, "is_final": True, "language": "hi"} for t in self._utterance]
            self.reply({"tokens": tokens + [{"text": "<fin>", "is_final": True}]})

    async def close(self):
        self.state = State.CLOSED
        self._replies.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._replies.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)


def _app(user):
    app = FastAPI()
    app.include_router(workflow_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=user.id,
        is_superuser=False,
        selected_organization_id=user.selected_organization_id,
        provider_id=user.provider_id,
    )
    return app


async def _soniox_agent(db_session, async_session, suffix, **stt_settings):
    """An agent on Soniox with a Dictionary, and a fresh run of it."""
    with patch.dict(helpers.USER_CONFIGURATION, {"stt": {**SONIOX, **stt_settings}}):
        _, user, workflow = await helpers.create_workflow_run_rows(
            db_session,
            async_session,
            workflow_definition=_definition(),
            name_prefix=f"Soniox E2E {suffix}",
            provider_id_suffix=f"soniox-e2e-{suffix}",
        )
    transport = httpx.ASGITransport(app=_app(user))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            f"/workflow/{workflow.id}",
            json={"workflow_configurations": {"dictionary": "Think Gas, BP number, PNG"}},
        )
    assert resp.status_code == 200, resp.text
    run = await db_session.create_workflow_run(
        name=f"Soniox E2E {suffix} run",
        workflow_id=workflow.id,
        mode=WorkflowRunMode.SMALLWEBRTC.value,
        user_id=user.id,
        use_draft=True,
        organization_id=workflow.organization_id,
    )
    return run, user, workflow


async def _run_call(run, user, workflow, llm, fake, script, mode=WorkflowRunMode.SMALLWEBRTC.value):
    captured_task: list = []
    built: list = []
    real_create_stt = run_pipeline_module.create_stt_service.__wrapped__ if hasattr(
        run_pipeline_module.create_stt_service, "__wrapped__"
    ) else run_pipeline_module.create_stt_service

    def create_stt(*args, **kwargs):
        service = real_create_stt(*args, **kwargs)
        built.append((service, kwargs))
        return service

    connect = AsyncMock(return_value=fake)
    audio_config = create_audio_config(mode)
    transport = MockTransport(
        TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_config.transport_in_sample_rate,
            audio_out_sample_rate=audio_config.transport_out_sample_rate,
        )
    )
    tts = MockTTSService(mock_audio_duration_ms=300, frame_delay=0)
    task = None
    with helpers.patch_run_pipeline_externals(captured_task, llm=llm, tts=tts):
        with (
            # The real Soniox service, built by the real factory...
            patch("api.services.pipecat.run_pipeline.create_stt_service", create_stt),
            # ...talking to the fake instead of Soniox.
            patch("pipecat.services.soniox.stt.websocket_connect", connect),
        ):
            run_task = asyncio.create_task(
                _run_pipeline(
                    transport=transport,
                    workflow_id=workflow.id,
                    workflow_run_id=run.id,
                    user_id=user.id,
                    audio_config=audio_config,
                    user_provider_id=user.provider_id,
                )
            )
            try:
                for _ in range(80):
                    if captured_task or run_task.done():
                        break
                    await asyncio.sleep(0.05)
                task = captured_task[0]
                await wait_for_pipeline_worker_started(task, timeout=3.0, run_task=run_task)
                await asyncio.sleep(1.5)  # greeting
                await script(task)
                await asyncio.wait_for(run_task, timeout=15.0)
            finally:
                if task is not None and not task.has_finished():
                    await task.cancel()
    fake.auth_header = connect.await_args.kwargs["additional_headers"]["Authorization"]
    return built


async def _speak(task, rate=16000):
    """The caller talks for 2.5 s, then pauses: real speech, real-time pacing."""
    for frame in _speech_frames(rate):
        await task.queue_frame(frame)
        await asyncio.sleep(0.02)


def _end_call_step():
    return MockLLMService.create_function_call_chunks(
        function_name="end_call", arguments={}, tool_call_id="call_end_1"
    )


@pytest.mark.parametrize(
    "mode,rate",
    [(WorkflowRunMode.SMALLWEBRTC.value, 16000), ("twilio", 8000)],
    ids=["browser-16k", "phone-8k"],
)
@pytest.mark.asyncio
async def test_a_call_transcribed_by_soniox(db_session, async_session, mode, rate):
    run, user, workflow = await _soniox_agent(db_session, async_session, f"call-{rate}")
    fake = FakeSoniox(utterance=["Mera BP number", " 7000 2382 hai,", " call end kar dijiye"])
    llm = ContextCapturingMockLLM(mock_steps=[_end_call_step()], chunk_delay=0.001)

    async def script(task):
        await _speak(task, rate=rate)

    built = await asyncio.wait_for(
        _run_call(run, user, workflow, llm, fake, script, mode=mode), HARD_TIMEOUT_SECONDS
    )

    # The real factory built our Soniox service, tagged with this call.
    [(service, kwargs)] = built
    assert isinstance(service, DograhSonioxSTTService)
    assert kwargs["call_reference"] == f"run-{run.id}"
    assert kwargs["keyterms"] == ["Think Gas", "BP number", "PNG"]

    # What Soniox was sent: the agent's settings, then the caller's audio.
    config = fake.config
    assert fake.auth_header == "Bearer sk-soniox-e2e"
    assert "api_key" not in config
    assert config["model"] == "stt-rt-v5"
    assert sorted(config["language_hints"]) == ["en", "hi"]
    assert config["context"]["terms"] == ["Think Gas", "BP number", "PNG"]
    assert config["context"]["general"] == [{"key": "domain", "value": "Think Gas PNG customer support"}]
    assert config["client_reference_id"] == f"run-{run.id}"
    assert config["sample_rate"] == rate  # the line's own rate: 8 kHz on phone calls
    assert config["enable_endpoint_detection"] is False
    assert fake.audio_bytes > rate * 2  # over a second of the caller's audio
    # The local VAD heard the caller stop and asked Soniox to finalize.
    assert fake.finalize_requests >= 1

    # Soniox's transcript reached the LLM as the caller's turn.
    user_texts = [
        m.get("content") or ""
        for ctx in llm.captured_contexts
        for m in ctx["messages"]
        if m.get("role") == "user"
    ]
    assert any("Mera BP number 7000 2382 hai, call end kar dijiye" in text for text in user_texts), user_texts


@pytest.mark.asyncio
async def test_soniox_turn_detection_ends_the_callers_turn(db_session, async_session):
    """With Soniox turn detection on, Soniox, not the local VAD, ends the turn."""
    run, user, workflow = await _soniox_agent(
        db_session,
        async_session,
        "soniox-turns",
        use_soniox_turn_detection=True,
        max_endpoint_delay_ms=1000,
    )
    fake = FakeSoniox(utterance=["Haan ji,", " call end kar dijiye"])
    llm = ContextCapturingMockLLM(mock_steps=[_end_call_step()], chunk_delay=0.001)

    async def script(task):
        await _speak(task, rate=8000)

    await asyncio.wait_for(
        _run_call(run, user, workflow, llm, fake, script, mode="twilio"), HARD_TIMEOUT_SECONDS
    )

    config = fake.config
    assert config["enable_endpoint_detection"] is True
    assert config["max_endpoint_delay_ms"] == 1000
    assert fake.finalized_by_soniox is True
    assert fake.finalize_requests == 0  # nothing asked Soniox to finalize
    user_texts = [
        m.get("content") or ""
        for ctx in llm.captured_contexts
        for m in ctx["messages"]
        if m.get("role") == "user"
    ]
    assert any("Haan ji, call end kar dijiye" in text for text in user_texts), user_texts


@pytest.mark.asyncio
async def test_soniox_out_of_credit_pauses_the_campaign(db_session, async_session):
    run, user, workflow = await _soniox_agent(db_session, async_session, "credit")
    run_id = run.id
    campaign = CampaignModel(
        name="Soniox credit campaign",
        organization_id=workflow.organization_id,
        workflow_id=workflow.id,
        created_by=user.id,
        source_type="test",
        source_id="s",
        state="running",
        rate_limit_per_second=10,
    )
    async_session.add(campaign)
    await async_session.flush()
    campaign_id = campaign.id
    await async_session.execute(
        update(WorkflowRunModel).where(WorkflowRunModel.id == run_id).values(campaign_id=campaign_id)
    )
    await async_session.commit()

    fake = FakeSoniox()
    llm = MockLLMService(mock_steps=[], chunk_delay=0.001)

    async def script(task):
        await task.queue_frame(InputAudioRawFrame(audio=b"\x00\x00" * 320, sample_rate=16000, num_channels=1))
        fake.reply({"tokens": [], "error_code": 402, "error_message": "Organization balance exhausted."})
        await asyncio.sleep(1.0)
        await task.cancel()

    record = AsyncMock()
    with patch.object(cb_module.circuit_breaker, "record_and_evaluate", record):
        await asyncio.wait_for(_run_call(run, user, workflow, llm, fake, script), HARD_TIMEOUT_SECONDS)

    result = await async_session.execute(
        select(CampaignModel.state, CampaignModel.logs)
        .where(CampaignModel.id == campaign_id)
        .execution_options(populate_existing=True)
    )
    state, logs = result.one()
    assert state == "paused"
    [entry] = [log for log in logs or [] if log["event"] == "provider_error_circuit_breaker_tripped"]
    assert entry["details"]["status_code"] == 402
    assert entry["details"]["service"] == "stt"
    assert entry["details"]["workflow_run_id"] == run_id
