"""A refused resume must not end the call (run 667).

Google dropped a gemini-3.8-live session with 1011 forty seconds into a call;
upstream then retried the same resumption handle three times, Google refused
every resume with 1011 (google-gemini/gemini-live-api-examples#60), and the
call ended. The reconnect now resumes once and, if that is refused, starts a
fresh session reseeded from LLMContext.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

GEMINI_3_MODEL = "models/gemini-3.1-flash-live-preview"


class _TestService(DograhGeminiLiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


def _make_service() -> _TestService:
    service = _TestService(
        api_key="test-key", settings=_TestService.Settings(model=GEMINI_3_MODEL)
    )
    service.push_error = AsyncMock()
    return service


async def _reconnect_with(service, failures: int) -> list:
    """Run one reconnect after `failures` consecutive errors; return the
    resumption handles `_connect` was called with."""
    service._consecutive_failures = failures
    handles = []

    async def fake_connect(self, session_resumption_handle=None):
        handles.append(session_resumption_handle)

    with patch.object(GeminiLiveLLMService, "_connect", fake_connect), patch.object(
        GeminiLiveLLMService, "_disconnect", AsyncMock()
    ):
        await service._reconnect()
    return handles


@pytest.mark.asyncio
async def test_first_retry_resumes_the_session():
    service = _make_service()
    service._session_resumption_handle = "handle-1"

    assert await _reconnect_with(service, failures=1) == ["handle-1"]
    assert service._awaiting_context_compaction_seed is False


@pytest.mark.asyncio
async def test_a_refused_resume_is_followed_by_a_fresh_reseeded_session():
    service = _make_service()
    service._session_resumption_handle = "handle-1"

    # The resume (failure 1) was refused too: this is failure 2.
    assert await _reconnect_with(service, failures=2) == [None]
    assert service._session_resumption_handle is None
    # Routed through the compaction refresh: hold audio, reseed, replay.
    assert service._awaiting_context_compaction_seed is True


@pytest.mark.asyncio
async def test_the_fresh_session_is_reseeded_and_held_audio_replayed():
    service = _make_service()
    service._session_resumption_handle = "handle-1"
    await _reconnect_with(service, failures=2)

    # Caller audio arriving while the fresh session connects is held, not lost.
    frame = SimpleNamespace(audio=b"\x01\x02" * 160)
    sent = []
    with patch.object(
        GeminiLiveLLMService, "_send_user_audio", AsyncMock(side_effect=lambda f: sent.append(f))
    ):
        await service._send_user_audio(frame)
        assert sent == []

        service._create_initial_response = AsyncMock()
        service._drain_pending_tool_results = AsyncMock()
        await service._handle_session_ready(SimpleNamespace())

    service._create_initial_response.assert_awaited_once_with(for_reconnect=True)
    assert sent == [frame]
    assert service._awaiting_context_compaction_seed is False


@pytest.mark.asyncio
async def test_without_a_handle_nothing_changes():
    service = _make_service()
    service._session_resumption_handle = None

    assert await _reconnect_with(service, failures=2) == [None]
    assert service._awaiting_context_compaction_seed is False
