"""Context compaction: refreshing the Gemini Live session to shed audio history.

Gemini Live bills the whole accumulated context every turn and keeps spoken
history as audio, so a long call pays repeatedly for an expensive form of its
own transcript. Reconnecting reseeds from LLMContext (text), so these tests pin
down when that refresh fires and when it must not.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from api.services.pipecat.realtime import gemini_live as gemini_live_module
from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService

GEMINI_3_MODEL = "models/gemini-3.1-flash-live-preview"
GEMINI_25_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"

TRIGGER = 5000


@pytest.fixture(autouse=True)
def _fixed_trigger(monkeypatch):
    """Pin the trigger so tests don't depend on the deployment's env value."""
    monkeypatch.setattr(
        gemini_live_module, "CONTEXT_COMPACTION_TRIGGER_TOKENS", TRIGGER
    )


class _TestDograhGeminiLiveLLMService(DograhGeminiLiveLLMService):
    """Dograh Gemini service with client creation stubbed for unit tests."""

    def create_client(self):
        self._client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=None))
        )


class _FakeSession:
    def __init__(self):
        self.send_client_content = AsyncMock()
        self.send_tool_response = AsyncMock()
        self.send_realtime_input = AsyncMock()
        self.close = AsyncMock()


def _make_service(model: str = GEMINI_3_MODEL) -> _TestDograhGeminiLiveLLMService:
    service = _TestDograhGeminiLiveLLMService(
        api_key="test-key",
        settings=_TestDograhGeminiLiveLLMService.Settings(model=model),
    )
    service.stop_all_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    service.start_llm_usage_metrics = AsyncMock()
    service.cancel_task = AsyncMock()
    service.push_error = AsyncMock()
    return service


def _audio_frame(audio: bytes) -> SimpleNamespace:
    return SimpleNamespace(audio=audio, sample_rate=16000, num_channels=1)


def _usage_message(prompt_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            response_token_count=10,
            total_token_count=prompt_tokens + 10,
            cached_content_token_count=None,
            thoughts_token_count=None,
        )
    )


# ----------------------------------------------------------------------
# Deciding when the context is over budget
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_over_trigger_schedules_compaction():
    service = _make_service()

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER + 1))

    assert service._context_compaction_pending


@pytest.mark.asyncio
async def test_usage_under_trigger_leaves_session_alone():
    service = _make_service()

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER - 1))

    assert not service._context_compaction_pending


@pytest.mark.asyncio
async def test_usage_metrics_still_reported():
    """Compaction piggybacks on usage reporting; it must not swallow it."""
    service = _make_service()

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER + 1))

    service.start_llm_usage_metrics.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_usage_during_a_refresh_does_not_requeue():
    """The finished turn's usage lands ~120ms into the refresh, describing the
    context being discarded. Acting on it queues a redundant second refresh."""
    service = _make_service()
    service._awaiting_context_compaction_seed = True

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER * 2))

    assert not service._context_compaction_pending


@pytest.mark.asyncio
async def test_usage_after_the_refresh_completes_still_arms():
    service = _make_service()
    service._awaiting_context_compaction_seed = False

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER + 1))

    assert service._context_compaction_pending


@pytest.mark.asyncio
async def test_compaction_disabled_on_gemini_25():
    """A 2.5 reseed forces a recap utterance, so compaction must not run there."""
    service = _make_service(model=GEMINI_25_MODEL)

    await service._handle_msg_usage_metadata(_usage_message(TRIGGER * 10))

    assert not service._context_compaction_pending


@pytest.mark.asyncio
async def test_compaction_disabled_when_trigger_is_zero(monkeypatch):
    monkeypatch.setattr(gemini_live_module, "CONTEXT_COMPACTION_TRIGGER_TOKENS", 0)
    service = _make_service()

    await service._handle_msg_usage_metadata(_usage_message(100_000))

    assert not service._context_compaction_pending


# ----------------------------------------------------------------------
# Choosing a safe moment to act
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_waits_while_user_is_speaking():
    service = _make_service()
    service._session = _FakeSession()
    service._context = LLMContext(messages=[])
    service._context_compaction_pending = True
    service._user_is_speaking = True
    service._compact_context_via_reconnect = AsyncMock()

    await service._maybe_compact_context()

    service._compact_context_via_reconnect.assert_not_awaited()
    # Still pending, so the next bot turn retries rather than dropping it.
    assert service._context_compaction_pending


@pytest.mark.asyncio
async def test_compaction_yields_to_a_pending_node_transition():
    """A transition reconnects and reseeds from text already — same compaction."""
    service = _make_service()
    service._session = _FakeSession()
    service._context = LLMContext(messages=[])
    service._context_compaction_pending = True
    service._awaiting_node_transition_context = True
    service._compact_context_via_reconnect = AsyncMock()

    await service._maybe_compact_context()

    service._compact_context_via_reconnect.assert_not_awaited()
    assert not service._context_compaction_pending


@pytest.mark.asyncio
async def test_compaction_skipped_while_disconnecting():
    service = _make_service()
    service._session = _FakeSession()
    service._context = LLMContext(messages=[])
    service._context_compaction_pending = True
    service._disconnecting = True
    service._compact_context_via_reconnect = AsyncMock()

    await service._maybe_compact_context()

    service._compact_context_via_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_turn_end_runs_a_due_compaction():
    service = _make_service()
    service._session = _FakeSession()
    service._context = LLMContext(messages=[])
    service._bot_is_responding = True
    service._context_compaction_pending = True
    service._compact_context_via_reconnect = AsyncMock()

    await service._set_bot_is_responding(False)

    service._compact_context_via_reconnect.assert_awaited_once()
    assert not service._context_compaction_pending


# ----------------------------------------------------------------------
# Performing the refresh
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_drops_the_resumption_handle_and_reconnects():
    """Resuming would restore the audio history we are trying to shed."""
    service = _make_service()
    service._session = _FakeSession()
    service._session_resumption_handle = "stale-handle"
    service._disconnect = AsyncMock()
    service._connect = AsyncMock()

    await service._compact_context_via_reconnect()

    assert service._awaiting_context_compaction_seed
    assert service._session_resumption_handle is None
    service._disconnect.assert_awaited_once()
    service._connect.assert_awaited_once_with(session_resumption_handle=None)


@pytest.mark.asyncio
async def test_session_ready_reseeds_from_context_without_speaking():
    service = _make_service()
    service._awaiting_context_compaction_seed = True
    service._create_initial_response = AsyncMock()
    service._drain_pending_tool_results = AsyncMock()

    session = _FakeSession()
    await service._handle_session_ready(session)

    service._create_initial_response.assert_awaited_once_with(for_reconnect=True)
    assert service._session is session
    assert service._ready_for_realtime_input
    assert not service._awaiting_context_compaction_seed


@pytest.mark.asyncio
async def test_held_audio_is_replayed_after_the_seed():
    service = _make_service()
    service._awaiting_context_compaction_seed = True
    service._create_initial_response = AsyncMock()
    service._drain_pending_tool_results = AsyncMock()

    # Speech landing mid-refresh must survive it.
    await service._send_user_audio(_audio_frame(b"\x01\x02"))
    await service._send_user_audio(_audio_frame(b"\x03\x04"))
    assert len(service._compaction_audio_frames) == 2

    sent = []
    monkey_super = AsyncMock(side_effect=lambda frame: sent.append(frame.audio))
    with patch.object(GeminiLiveLLMService, "_send_user_audio", monkey_super):
        await service._handle_session_ready(_FakeSession())

    assert sent == [b"\x01\x02", b"\x03\x04"]
    assert service._compaction_audio_frames == []
    assert service._compaction_audio_bytes == 0


@pytest.mark.asyncio
async def test_held_audio_is_bounded_when_a_reconnect_stalls():
    service = _make_service()
    service._awaiting_context_compaction_seed = True

    chunk = b"\x00" * 4000
    for _ in range(60):  # 240 kB, far past the ~160 kB cap
        await service._send_user_audio(_audio_frame(chunk))

    assert (
        service._compaction_audio_bytes
        <= gemini_live_module.MAX_COMPACTION_BUFFERED_AUDIO_BYTES
    )
    # Oldest dropped, most recent speech kept.
    assert service._compaction_audio_frames


@pytest.mark.asyncio
async def test_muted_audio_is_not_held():
    service = _make_service()
    service._awaiting_context_compaction_seed = True
    service._user_is_muted = True

    await service._send_user_audio(_audio_frame(b"\x01\x02"))

    assert service._compaction_audio_frames == []


@pytest.mark.asyncio
async def test_node_transition_seed_takes_precedence_over_compaction_seed():
    """Both flags set: the transition path owns the reseed and stays gated."""
    service = _make_service()
    service._awaiting_node_transition_context = True
    service._awaiting_context_compaction_seed = True
    service._maybe_seed_node_transition_context = AsyncMock()
    service._create_initial_response = AsyncMock()

    await service._handle_session_ready(_FakeSession())

    service._maybe_seed_node_transition_context.assert_awaited_once()
    service._create_initial_response.assert_not_awaited()
    assert not service._ready_for_realtime_input
