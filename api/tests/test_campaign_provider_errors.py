"""A campaign pauses at once when a provider rejects our key or account.

1. The HTTP status is read from every error shape our providers raise.
2. A service's 401/402/403 pauses the campaign; a tool's own HTTP error never does.
3. The DB pause touches only the failing run's campaign, logs once, and leaves
   API/test calls (no campaign) and other organizations alone.
4. Resuming clears the pause and the breaker window; a failed reset keeps it paused.
5. Dials: 401/402/403 pause, a telephony outage (5xx / no answer) counts toward
   the failure rate, a bad number (4xx) does neither.
"""

import asyncio
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import ErrorFrame
from pipecat.processors.frame_processor import FrameProcessor
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CampaignModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.campaign import circuit_breaker as module
from api.services.campaign.circuit_breaker import (
    CircuitBreaker,
    provider_http_status,
    register_campaign_provider_error_handler,
    telephony_outage,
)

# ── Reading the status ────────────────────────────────────────────────


class _WithStatusCode(Exception):  # OpenAI, Anthropic, httpx-based SDKs
    def __init__(self, status):
        super().__init__("request failed")
        self.status_code = status


class _WithStatus(Exception):  # aiohttp ClientResponseError
    def __init__(self, status):
        super().__init__("request failed")
        self.status = status


class _WithCode(Exception):  # google-genai APIError
    def __init__(self, code):
        super().__init__("request failed")
        self.code = code


class _WithResponse(Exception):  # httpx HTTPStatusError, websockets InvalidStatus
    def __init__(self, status):
        super().__init__("request failed")
        self.response = SimpleNamespace(status_code=status)


def _chained(status):
    try:
        try:
            raise _WithStatusCode(status)
        except Exception as inner:
            raise RuntimeError("wrapped by the service") from inner
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    "exception,message,expected",
    [
        (_WithStatusCode(401), "", 401),
        (_WithStatus(402), "", 402),
        (_WithCode(403), "", 403),
        (_WithResponse(401), "", 401),
        (_chained(403), "", 403),
        (None, "Deepgram connection failed: HTTP 401 Unauthorized", 401),
        (None, "server rejected WebSocket connection: status_code=403", 403),
        (Exception("Invalid API key"), "error code: 402 insufficient credits", 402),
        (_WithStatusCode(500), "", 500),
        (None, "Socket closed unexpectedly", None),
        (_WithCode(1008), "", None),  # a WebSocket close code, not HTTP
    ],
    ids=[
        "status_code",
        "aiohttp-status",
        "google-code",
        "response",
        "chained",
        "deepgram-text",
        "ws-text",
        "message",
        "server-error",
        "no-status",
        "ws-close-code",
    ],
)
def test_the_http_status_is_found_wherever_the_provider_put_it(exception, message, expected):
    assert provider_http_status(exception, message) == expected


@pytest.mark.parametrize(
    "exception,outage",
    [
        (_WithStatusCode(503), True),
        (_WithStatus(500), True),
        (TimeoutError("dial timed out"), True),
        (ConnectionError("connection refused"), True),
        (_WithStatusCode(400), False),  # malformed number
        (_WithStatusCode(404), False),  # number not found
        (_WithStatusCode(422), False),
    ],
)
def test_an_outage_is_told_apart_from_a_bad_number(exception, outage):
    assert telephony_outage(exception) is outage


# ── The service hook ──────────────────────────────────────────────────


async def _report(service, frame):
    await service._call_event_handler("on_error", frame)


@pytest.mark.asyncio
async def test_a_service_401_pauses_the_runs_campaign():
    service = FrameProcessor()
    with patch.object(module.circuit_breaker, "pause_on_provider_error", AsyncMock()) as pause:
        register_campaign_provider_error_handler(
            service, provider="sarvam", source="tts", workflow_run_id=77, organization_id=5
        )
        err = _WithStatusCode(401)
        await _report(service, ErrorFrame(error="Sarvam TTS error", exception=err))
    pause.assert_awaited_once()
    kwargs = pause.await_args.kwargs
    assert (kwargs["organization_id"], kwargs["workflow_run_id"]) == (5, 77)
    assert (kwargs["provider"], kwargs["service"]) == ("sarvam", "tts")
    assert kwargs["exception"] is err


@pytest.mark.asyncio
async def test_a_tools_own_http_error_never_pauses_the_campaign():
    # e.g. the LSQ update tool getting a 401 from LeadSquared.
    service = FrameProcessor()
    with patch.object(module.circuit_breaker, "pause_on_provider_error", AsyncMock()) as pause:
        register_campaign_provider_error_handler(
            service, provider="openai", source="llm", workflow_run_id=77, organization_id=5
        )
        await _report(
            service,
            ErrorFrame(
                error="Error executing function call [lsq_update]: HTTP 401 Unauthorized",
                exception=_WithStatusCode(401),
            ),
        )
    pause.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_only_401_402_403_pause(status):
    breaker = CircuitBreaker()
    with patch.object(module.db_client, "pause_campaign_for_provider_error", AsyncMock()) as db_pause:
        await breaker.pause_on_provider_error(
            organization_id=5,
            workflow_run_id=77,
            provider="openai",
            service="llm",
            exception=_WithStatusCode(status),
        )
    db_pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_database_failure_while_pausing_never_breaks_the_call():
    breaker = CircuitBreaker()
    with patch.object(
        module.db_client, "pause_campaign_for_provider_error", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        await breaker.pause_on_provider_error(
            organization_id=5, workflow_run_id=77, provider="openai", service="llm", exception=_WithStatusCode(401)
        )  # logged, not raised


# ── The database pause ────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    original_engine, original_session = db_client.engine, db_client.async_session
    db_client.engine, db_client.async_session = engine, session_factory
    yield session_factory
    db_client.engine, db_client.async_session = original_engine, original_session
    await engine.dispose()


@dataclass
class Ids:
    organization_id: int
    user_id: int
    workflow_id: int
    campaign_id: int
    run_id: int
    loose_run_id: int  # a run with no campaign (API / test call)


async def _setup(factory, state="running") -> Ids:
    async with factory() as session:
        org = OrganizationModel(provider_id=f"org-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        user = UserModel(provider_id=f"user-{uuid.uuid4().hex[:8]}", selected_organization_id=org.id)
        session.add(user)
        await session.flush()
        workflow = WorkflowModel(
            name=f"wf-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            organization_id=org.id,
            workflow_definition={"nodes": [], "edges": []},
            template_context_variables={},
        )
        session.add(workflow)
        await session.flush()
        campaign = CampaignModel(
            name=f"c-{uuid.uuid4().hex[:8]}",
            organization_id=org.id,
            workflow_id=workflow.id,
            created_by=user.id,
            source_type="test",
            source_id="s",
            state=state,
            rate_limit_per_second=10,
        )
        session.add(campaign)
        await session.flush()
        run = WorkflowRunModel(name="campaign call", workflow_id=workflow.id, mode="voicelink", campaign_id=campaign.id)
        loose = WorkflowRunModel(name="test call", workflow_id=workflow.id, mode="smallwebrtc")
        session.add_all([run, loose])
        await session.commit()
        return Ids(org.id, user.id, workflow.id, campaign.id, run.id, loose.id)


async def _cleanup(factory, ids: Ids):
    async with factory() as session:
        await session.execute(delete(WorkflowRunModel).where(WorkflowRunModel.workflow_id == ids.workflow_id))
        await session.execute(delete(CampaignModel).where(CampaignModel.id == ids.campaign_id))
        await session.execute(delete(WorkflowModel).where(WorkflowModel.id == ids.workflow_id))
        await session.execute(delete(UserModel).where(UserModel.id == ids.user_id))
        await session.execute(delete(OrganizationModel).where(OrganizationModel.id == ids.organization_id))
        await session.commit()


async def _campaign(factory, campaign_id):
    async with factory() as session:
        return await session.scalar(select(CampaignModel).where(CampaignModel.id == campaign_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "syncing"])
async def test_a_provider_401_pauses_the_campaign_and_says_why(db_session_factory, state):
    from api.db import db_client

    ids = await _setup(db_session_factory, state)
    try:
        paused = await db_client.pause_campaign_for_provider_error(
            ids.organization_id, ids.run_id, provider="sarvam", service="tts", status_code=401
        )
        assert paused == ids.campaign_id
        campaign = await _campaign(db_session_factory, ids.campaign_id)
        assert campaign.state == "paused"
        [entry] = [log for log in campaign.logs if log["event"] == "provider_error_circuit_breaker_tripped"]
        assert "sarvam tts returned HTTP 401" in entry["message"]
        assert entry["details"] == {
            "workflow_run_id": ids.run_id,
            "provider": "sarvam",
            "service": "tts",
            "status_code": 401,
        }

        # A second failure from another in-flight call: no second entry.
        again = await db_client.pause_campaign_for_provider_error(
            ids.organization_id, ids.run_id, provider="sarvam", service="tts", status_code=401
        )
        assert again is None
        campaign = await _campaign(db_session_factory, ids.campaign_id)
        assert sum(log["event"] == "provider_error_circuit_breaker_tripped" for log in campaign.logs) == 1
    finally:
        await _cleanup(db_session_factory, ids)


@pytest.mark.asyncio
async def test_calls_without_a_campaign_and_other_orgs_are_untouched(db_session_factory):
    from api.db import db_client

    ids = await _setup(db_session_factory)
    try:
        assert (
            await db_client.pause_campaign_for_provider_error(
                ids.organization_id, ids.loose_run_id, provider="openai", service="llm", status_code=401
            )
            is None
        )
        assert (
            await db_client.pause_campaign_for_provider_error(
                ids.organization_id + 999_999, ids.run_id, provider="openai", service="llm", status_code=401
            )
            is None
        )
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "running"
    finally:
        await _cleanup(db_session_factory, ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "cancelled", "completed"])
async def test_an_inactive_campaign_is_left_as_it_is(db_session_factory, state):
    from api.db import db_client

    ids = await _setup(db_session_factory, state)
    try:
        assert (
            await db_client.pause_campaign_for_provider_error(
                ids.organization_id, ids.run_id, provider="openai", service="llm", status_code=403
            )
            is None
        )
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == state
    finally:
        await _cleanup(db_session_factory, ids)


# ── Resuming ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resuming_clears_the_pause_and_the_breaker_window(db_session_factory):
    from api.db import db_client
    from api.services.campaign import runner as runner_module
    from api.services.campaign.runner import CampaignRunnerService

    ids = await _setup(db_session_factory)
    try:
        await db_client.pause_campaign_for_provider_error(
            ids.organization_id, ids.run_id, provider="openai", service="llm", status_code=402
        )
        with patch.object(runner_module.circuit_breaker, "reset", AsyncMock(return_value=True)) as reset:
            await CampaignRunnerService().resume_campaign(ids.campaign_id)
        reset.assert_awaited_once_with(ids.campaign_id)
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "running"

        # Another rejection after the fix pauses it again.
        assert (
            await db_client.pause_campaign_for_provider_error(
                ids.organization_id, ids.run_id, provider="openai", service="llm", status_code=402
            )
            == ids.campaign_id
        )
    finally:
        await _cleanup(db_session_factory, ids)


@pytest.mark.asyncio
async def test_a_failed_breaker_reset_keeps_the_campaign_paused(db_session_factory):
    from api.db import db_client
    from api.services.campaign import runner as runner_module
    from api.services.campaign.runner import CampaignRunnerService

    ids = await _setup(db_session_factory)
    try:
        await db_client.pause_campaign_for_provider_error(
            ids.organization_id, ids.run_id, provider="openai", service="llm", status_code=401
        )
        with patch.object(runner_module.circuit_breaker, "reset", AsyncMock(return_value=False)):
            with pytest.raises(ValueError, match="reset"):
                await CampaignRunnerService().resume_campaign(ids.campaign_id)
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "paused"
    finally:
        await _cleanup(db_session_factory, ids)


# ── Dial failures ─────────────────────────────────────────────────────


async def _dial_and_fail(exception):
    """Run dispatch_call to the point where the provider rejects the dial."""
    from api.services.campaign import campaign_call_dispatcher as dispatcher_module

    campaign = SimpleNamespace(
        id=42, workflow_id=7, organization_id=5, telephony_configuration_id=None, created_by=1
    )
    queued_run = SimpleNamespace(id=9, context_variables={"phone_number": "+919999999999"}, source_uuid="u")
    run = SimpleNamespace(id=300, logs={})
    provider = SimpleNamespace(
        PROVIDER_NAME="voicelink",
        WEBHOOK_ENDPOINT="voicelink",
        initiate_call=AsyncMock(side_effect=exception),
    )
    breaker = SimpleNamespace(pause_on_provider_error=AsyncMock(), record_and_evaluate=AsyncMock())
    db = MagicMock()
    db.get_workflow_by_id = AsyncMock(return_value=SimpleNamespace(id=7))
    db.create_workflow_run = AsyncMock(return_value=run)
    db.update_workflow_run = AsyncMock()
    dispatcher = dispatcher_module.CampaignCallDispatcher()
    with (
        patch.object(dispatcher_module, "db_client", db),
        patch.object(dispatcher_module, "circuit_breaker", breaker),
        patch.object(dispatcher, "get_provider_for_campaign", AsyncMock(return_value=provider)),
        patch.object(dispatcher, "acquire_from_number", AsyncMock(return_value="slot")),
        patch.object(dispatcher, "release_call_slot", AsyncMock()),
        patch.object(dispatcher_module.rate_limiter, "pool_slot_address", MagicMock(return_value="+918000000000")),
        patch.object(dispatcher_module.rate_limiter, "store_workflow_from_number_mapping", AsyncMock()),
        patch.object(dispatcher_module, "call_concurrency", MagicMock(bind_workflow_run=AsyncMock())),
        patch.object(dispatcher_module.webhook_sync_calling, "call_started", AsyncMock()),
        patch.object(
            dispatcher_module, "authorize_workflow_run_start", AsyncMock(return_value=SimpleNamespace(has_quota=True))
        ),
        patch.object(dispatcher_module, "get_backend_endpoints", AsyncMock(return_value=("https://x", "wss://x"))),
    ):
        with pytest.raises(type(exception)):
            await dispatcher.dispatch_call(queued_run, campaign, MagicMock())
    return breaker


@pytest.mark.asyncio
async def test_a_telephony_401_pauses_the_campaign_and_isnt_counted_as_a_call_failure():
    breaker = await _dial_and_fail(_WithStatusCode(401))
    breaker.pause_on_provider_error.assert_awaited_once()
    assert breaker.pause_on_provider_error.await_args.kwargs["service"] == "telephony"
    breaker.record_and_evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [_WithStatusCode(503), TimeoutError("dial timed out")], ids=["5xx", "timeout"])
async def test_a_telephony_outage_counts_toward_the_failure_rate(exception):
    breaker = await _dial_and_fail(exception)
    breaker.record_and_evaluate.assert_awaited_once()
    kwargs = breaker.record_and_evaluate.await_args.kwargs
    assert kwargs["is_failure"] is True and kwargs["reason"] == "telephony_unavailable"
    assert kwargs["workflow_run_id"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_a_bad_number_neither_pauses_nor_counts(status):
    breaker = await _dial_and_fail(_WithStatusCode(status))
    breaker.record_and_evaluate.assert_not_awaited()
    # pause_on_provider_error is consulted but ignores non-401/402/403.
    assert breaker.pause_on_provider_error.await_count == 1


# ── Rejections described only in words ────────────────────────────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ('400 Bad Request. {"message": "API key not valid. Please pass a valid API key."}', 401),  # Google
        ("Gemini Live connection failed after 3 consecutive attempts: 1007 None. API key not valid.", 401),
        ("connection closed, but with an error: received 1008 (policy violation) Invalid API key", 401),
        ("received 1008 (policy violation) Invalid api key used.", 401),  # Murf
        ("Error code: 429 - You exceeded your current quota, please check your plan and billing details.", 402),
        ("insufficient_quota", 402),
        ("Error code: 429 - Rate limit reached, please try again in 20ms", 429),  # wait it out, don't pause
        ("connection closed by server", None),
        ("Error code: 503 - service unavailable", 503),
    ],
    ids=[
        "google-400",
        "gemini-live-1007",
        "elevenlabs-1008",
        "murf-1008",
        "openai-no-credit",
        "insufficient-quota",
        "rate-limit",
        "plain-close",
        "server-error",
    ],
)
def test_rejections_described_only_in_words_are_recognised(message, expected):
    assert provider_http_status(None, message) == expected


# ── Murf: a key rejection stops reconnecting and says why ─────────────


@pytest.mark.asyncio
async def test_murf_reports_a_rejected_key_instead_of_reconnecting():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    from api.services.pipecat.provider_rejection import ProviderRejectionAwareWebsocket

    class _Base:
        reconnects = 0

        async def _maybe_try_reconnect(self, error_message, report_error, error=None):
            _Base.reconnects += 1
            return True

    class _Service(ProviderRejectionAwareWebsocket, _Base):
        pass

    reported = []

    async def report(frame):
        reported.append(frame)

    rejected = ConnectionClosedError(Close(1008, "Invalid api key used."), None)
    keep_going = await _Service()._maybe_try_reconnect("closed with an error", report, rejected)
    assert keep_going is False and _Base.reconnects == 0
    assert reported and provider_http_status(reported[0].exception, reported[0].error) == 401

    # Any other drop still reconnects as before.
    dropped = ConnectionClosedError(Close(1011, "internal error"), None)
    assert await _Service()._maybe_try_reconnect("closed with an error", report, dropped) is True
    assert _Base.reconnects == 1


def test_the_murf_service_uses_the_rejection_handling():
    from api.services.pipecat.murf_tts import MurfTTSService
    from api.services.pipecat.provider_rejection import ProviderRejectionAwareWebsocket

    assert MurfTTSService.__mro__[1] is ProviderRejectionAwareWebsocket


# ── Deepgram: a rejected key is reported once; drops back off ─────────


class _DeepgramFake:
    """Stands in for the Deepgram client's connect() context manager."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.attempts = 0

    def connect(self, **_kwargs):
        fake = self

        class _Ctx:
            async def __aenter__(self):
                fake.attempts += 1
                if fake.failures:
                    raise fake.failures.pop(0)
                raise asyncio.CancelledError  # end the test once failures run out

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_deepgram_reports_a_rejected_key_and_stops_retrying():
    from api.services.pipecat.deepgram_stt import DograhDeepgramSTTService

    service = DograhDeepgramSTTService(api_key="bad", sample_rate=16000)
    fake = _DeepgramFake([_WithStatusCode(401)])
    service._client = SimpleNamespace(listen=SimpleNamespace(v1=fake))
    service.push_error = AsyncMock()
    await service._connection_handler()
    assert fake.attempts == 1  # no retry loop
    service.push_error.assert_awaited_once()
    assert provider_http_status(service.push_error.await_args.kwargs["exception"]) == 401


@pytest.mark.asyncio
async def test_deepgram_backs_off_between_reconnects_after_a_drop():
    from api.services.pipecat import deepgram_stt

    service = deepgram_stt.DograhDeepgramSTTService(api_key="k", sample_rate=16000)
    fake = _DeepgramFake([ConnectionError("network")] * 4)
    service._client = SimpleNamespace(listen=SimpleNamespace(v1=fake))
    service.push_error = AsyncMock()
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)

    with patch.object(deepgram_stt.asyncio, "sleep", fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await service._connection_handler()
    assert sleeps == [0.5, 1.0, 2.0, 4.0]  # was a tight loop with no wait at all
    service.push_error.assert_not_awaited()


def test_the_factory_builds_the_fixed_deepgram_service():
    from api.services.pipecat import service_factory

    assert service_factory.DograhDeepgramSTTService.__name__ == "DograhDeepgramSTTService"
    assert not hasattr(service_factory, "DeepgramSTTService")


# ── Many calls hitting the same bad key at once ───────────────────────


@pytest.mark.asyncio
async def test_twenty_calls_failing_at_once_pause_once_with_one_log_entry(db_session_factory):
    from api.db import db_client

    ids = await _setup(db_session_factory)
    try:
        async with db_session_factory() as session:
            extra = [
                WorkflowRunModel(
                    name=f"call {i}", workflow_id=ids.workflow_id, mode="voicelink", campaign_id=ids.campaign_id
                )
                for i in range(19)
            ]
            session.add_all(extra)
            await session.commit()
            run_ids = [ids.run_id] + [run.id for run in extra]
        results = await asyncio.gather(
            *[
                db_client.pause_campaign_for_provider_error(
                    ids.organization_id, run_id, provider="sarvam", service="tts", status_code=403
                )
                for run_id in run_ids
            ]
        )
        assert sum(r is not None for r in results) == 1  # exactly one call did the pausing
        campaign = await _campaign(db_session_factory, ids.campaign_id)
        assert campaign.state == "paused"
        assert sum(log["event"] == "provider_error_circuit_breaker_tripped" for log in campaign.logs) == 1
    finally:
        await _cleanup(db_session_factory, ids)


# ── A rejection that lands during a resume is not lost ────────────────


@pytest.mark.asyncio
async def test_a_rejection_during_a_resume_leaves_the_campaign_paused(db_session_factory):
    from api.db import db_client
    from api.services.campaign import runner as runner_module
    from api.services.campaign.runner import CampaignRunnerService

    ids = await _setup(db_session_factory, state="paused")
    try:
        in_reset = asyncio.Event()
        finish_reset = asyncio.Event()

        async def slow_reset(campaign_id):
            in_reset.set()
            await finish_reset.wait()
            return True

        with patch.object(runner_module.circuit_breaker, "reset", slow_reset):
            resume = asyncio.create_task(CampaignRunnerService().resume_campaign(ids.campaign_id))
            await in_reset.wait()
            # Another call's provider rejects the key while the resume holds the row.
            pause = asyncio.create_task(
                db_client.pause_campaign_for_provider_error(
                    ids.organization_id, ids.run_id, provider="openai", service="llm", status_code=401
                )
            )
            await asyncio.sleep(0.3)
            assert not pause.done()  # waits for the resume instead of racing it
            finish_reset.set()
            await resume
            paused_by = await pause
        assert paused_by == ids.campaign_id
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "paused"
    finally:
        await _cleanup(db_session_factory, ids)

# ── The failure-rate path end to end (real Redis + DB) ────────────────


@pytest.mark.asyncio
async def test_five_failed_calls_pause_the_campaign_naming_them(db_session_factory):
    from api.services.campaign.circuit_breaker import circuit_breaker

    ids = await _setup(db_session_factory)
    try:
        await circuit_breaker.reset(ids.campaign_id)
        failed_runs = [ids.run_id * 1000 + i for i in range(5)]
        for i, run_id in enumerate(failed_runs):
            await circuit_breaker.record_and_evaluate(
                ids.campaign_id, is_failure=True, workflow_run_id=run_id, reason="pipeline_error"
            )
            expected = "paused" if i == 4 else "running"  # min 5 calls, 50% threshold
            assert (await _campaign(db_session_factory, ids.campaign_id)).state == expected
        campaign = await _campaign(db_session_factory, ids.campaign_id)
        [trip] = [log for log in campaign.logs if log["event"] == "circuit_breaker_tripped"]
        named = {entry["workflow_run_id"] for entry in trip["details"]["recent_failures"]}
        assert named == set(failed_runs)
    finally:
        await circuit_breaker.reset(ids.campaign_id)
        await _cleanup(db_session_factory, ids)


@pytest.mark.asyncio
async def test_the_same_call_reported_many_times_counts_once(db_session_factory):
    from api.services.campaign.circuit_breaker import circuit_breaker

    ids = await _setup(db_session_factory)
    try:
        await circuit_breaker.reset(ids.campaign_id)
        for _ in range(10):  # one failed call, finalized ten times
            await circuit_breaker.record_and_evaluate(
                ids.campaign_id, is_failure=True, workflow_run_id=ids.run_id, reason="pipeline_error"
            )
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "running"
        # A failed call can't also count as a success.
        await circuit_breaker.record_and_evaluate(
            ids.campaign_id, is_failure=False, workflow_run_id=ids.run_id, reason="user_hangup"
        )
        tripped, stats = await circuit_breaker.is_circuit_open(ids.campaign_id)
        assert stats is None or (stats["failure_count"], stats["success_count"]) == (1, 0)
    finally:
        await circuit_breaker.reset(ids.campaign_id)
        await _cleanup(db_session_factory, ids)


@pytest.mark.asyncio
async def test_healthy_calls_keep_the_rate_below_the_threshold(db_session_factory):
    from api.services.campaign.circuit_breaker import circuit_breaker

    ids = await _setup(db_session_factory)
    try:
        await circuit_breaker.reset(ids.campaign_id)
        base = ids.run_id * 1000
        for i in range(6):  # 6 good calls
            await circuit_breaker.record_and_evaluate(
                ids.campaign_id, is_failure=False, workflow_run_id=base + i, reason="user_hangup"
            )
        for i in range(4):  # 4 failures: 40% < 50%
            await circuit_breaker.record_and_evaluate(
                ids.campaign_id, is_failure=True, workflow_run_id=base + 100 + i, reason="pipeline_error"
            )
        assert (await _campaign(db_session_factory, ids.campaign_id)).state == "running"
    finally:
        await circuit_breaker.reset(ids.campaign_id)
        await _cleanup(db_session_factory, ids)


# ── Inception (Mercury): a rejected key pauses like any other ─────────


@pytest.mark.asyncio
async def test_an_inception_key_rejection_is_recognised():
    """A campaign on a bad or out-of-credit Inception key pauses."""
    import httpx
    import openai
    from pipecat.processors.aggregators.llm_context import LLMContext

    from api.services.pipecat.service_factory import create_llm_service_from_provider

    svc = create_llm_service_from_provider("inception", "mercury-3", "bad")
    context = LLMContext(messages=[{"role": "user", "content": "Mujhe tank chahiye"}])
    for status, message, expected in [
        (401, "Invalid API key", 401),
        (402, "Insufficient credits", 402),
        (429, "insufficient_quota: add credits", 402),
        (429, "Rate limit reached, try again shortly", 429),  # wait it out, don't pause
    ]:
        svc._client = openai.AsyncOpenAI(
            api_key="bad",
            base_url="https://api.inceptionlabs.ai/v1",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, s=status, m=message: httpx.Response(
                        s, json={"error": {"message": m}}
                    )
                )
            ),
            max_retries=0,
        )
        with pytest.raises(openai.APIStatusError) as exc:
            await svc.run_inference(context)
        assert provider_http_status(exc.value, str(exc.value)) == expected
