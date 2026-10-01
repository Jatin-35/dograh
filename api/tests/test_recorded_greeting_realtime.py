"""A recorded (audio) greeting on a realtime model (ported from upstream 9c82ceb0).

The recording is played straight to the transport, so Gemini Live never sees
the opening turn. Without a handoff its session is unseeded: it does not know
the call has started or what was said, and its first reply greets the caller
again. The engine now hands the greeting to the service, which seeds it as a
model turn without asking for a reply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pipecat.frames.frames import LLMAssistantPushAggregationFrame, TTSTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext

from api.services.pipecat.audio_playback import play_audio
from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService
from api.services.pipecat.realtime.gemini_live_38 import DograhGemini38LiveLLMService
from api.services.pipecat.recording_audio_cache import RecordingAudio
from api.services.workflow.pipecat_engine import PipecatEngine
from api.tests.test_text_and_audio_playback import (  # noqa: F401 (fixture)
    FAKE_PCM_AUDIO,
    audio_workflow,
)


def _client_stub(self):
    self._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=None)))


class _Gemini31(DograhGeminiLiveLLMService):
    create_client = _client_stub


class _Gemini38(DograhGemini38LiveLLMService):
    create_client = _client_stub


def _session():
    return SimpleNamespace(
        send_client_content=AsyncMock(),
        send_realtime_input=AsyncMock(),
        send_tool_response=AsyncMock(),
    )


def _engine(workflow, llm, context):
    engine = PipecatEngine(
        llm=llm,
        context=context,
        workflow=workflow,
        call_context_vars={},
        workflow_run_id=1,
    )
    # Recordings are stored by numeric id (the shared fixture uses a label).
    engine.get_node_greeting = lambda node_id: ("audio", "5")
    engine.set_transport_output(Mock(queue_frame=AsyncMock()))
    engine.set_fetch_recording_audio(
        AsyncMock(return_value=RecordingAudio(FAKE_PCM_AUDIO, "Namaskar, main Aastha"))
    )
    return engine


# ─── the engine hands the greeting over ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_recorded_greeting_is_handed_to_the_realtime_service(audio_workflow):
    llm = Mock(spec=["handle_prerecorded_greeting"])
    llm.handle_prerecorded_greeting = AsyncMock()
    context = LLMContext()
    engine = _engine(audio_workflow, llm, context)

    result = await engine.queue_node_opening(
        node_id=audio_workflow.start_node_id,
        previous_node_id=None,
        generate_if_no_greeting=True,
    )

    assert result == "greeting"
    llm.handle_prerecorded_greeting.assert_awaited_once_with(
        context, "Namaskar, main Aastha"
    )
    queued = [c.args[0] for c in engine._transport_output.queue_frame.await_args_list]
    assert [type(f).__name__ for f in queued] == [
        "TTSStartedFrame",
        "TTSTextFrame",
        "TTSAudioRawFrame",
        "TTSStoppedFrame",
        "LLMAssistantPushAggregationFrame",  # the greeting is its own turn
    ]


@pytest.mark.asyncio
async def test_a_text_llm_without_a_session_is_left_alone(audio_workflow):
    llm = Mock(spec=["queue_frame"])  # no handle_prerecorded_greeting
    engine = _engine(audio_workflow, llm, LLMContext())
    result = await engine.queue_node_opening(
        node_id=audio_workflow.start_node_id,
        previous_node_id=None,
        generate_if_no_greeting=True,
    )
    assert result == "greeting"


@pytest.mark.asyncio
async def test_a_recording_kept_out_of_context_does_not_close_a_turn():
    """Transition recordings (append_to_context=False) are unchanged."""
    queue = AsyncMock()
    await play_audio(
        FAKE_PCM_AUDIO, sample_rate=8000, queue_frame=queue,
        transcript="Aap nishchint rahein", persist_to_logs=True,
    )
    frames = [c.args[0] for c in queue.await_args_list]
    assert not any(isinstance(f, LLMAssistantPushAggregationFrame) for f in frames)
    assert any(isinstance(f, TTSTextFrame) for f in frames)


# ─── Gemini is told, but not asked to speak ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cls,model",
    [(_Gemini31, "gemini-3.1-flash-live-preview"), (_Gemini38, "gemini-3.8-live")],
)
async def test_gemini_seeds_the_greeting_without_asking_for_a_reply(cls, model):
    service = cls(api_key="k", settings=cls.Settings(model=model))
    session = _session()
    service._session = session
    service._disconnecting = False
    service._ready_for_realtime_input = False
    context = LLMContext()

    await service.handle_prerecorded_greeting(context, "Namaskar, main Aastha")

    kwargs = session.send_client_content.await_args.kwargs
    assert kwargs["turn_complete"] is False  # not asked to produce a turn
    last = kwargs["turns"][-1]
    assert last.role == "model" and last.parts[0].text == "Namaskar, main Aastha"
    session.send_realtime_input.assert_not_awaited()  # no generation nudge
    assert service._ready_for_realtime_input is True  # caller audio accepted
    assert service._handled_initial_context is True

    # A later greeting trigger must not open the conversation a second time.
    await service.handle_prerecorded_greeting(context, "again")
    assert session.send_client_content.await_count == 1


@pytest.mark.asyncio
async def test_before_the_session_connects_the_greeting_waits_for_it():
    service = _Gemini38(api_key="k", settings=_Gemini38.Settings(model="gemini-3.8-live"))
    service._session = None
    service._disconnecting = False

    await service.handle_prerecorded_greeting(LLMContext(), "Namaskar")
    assert service._pending_prerecorded_greeting == ("Namaskar",)
    assert service._run_llm_when_session_ready is True

    session = _session()
    await service._handle_session_ready(session)

    kwargs = session.send_client_content.await_args.kwargs
    assert kwargs["turn_complete"] is False
    assert kwargs["turns"][-1].parts[0].text == "Namaskar"
    assert service._pending_prerecorded_greeting is None
    session.send_realtime_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_context_yet_is_ignored_safely():
    service = _Gemini31(api_key="k", settings=_Gemini31.Settings(model="gemini-3.1-flash-live-preview"))
    await service.handle_prerecorded_greeting(None, "Namaskar")
    assert service._handled_initial_context is False
