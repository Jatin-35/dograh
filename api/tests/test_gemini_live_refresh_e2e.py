"""Gemini Live session refreshes, end to end through upstream's receive loop.

The unit tests next to this file mock single methods. These drive the real
``_connection_task_handler`` with a fake Gemini (real ``google.genai`` message
types, controllable connect latency) and check what a caller would notice:

- a compaction refresh — which runs inside the outgoing connection's own loop
  (turn_complete ends the bot turn, which starts it) — takes about as long as
  the connect itself, not a fixed timeout;
- afterwards exactly one receive loop is alive, and the superseded loop never
  reads from the new session;
- real failures of the new connection are still counted and reported.
"""

import asyncio
import itertools
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types as gt

import api.services.pipecat.realtime.gemini_live as gemini_live_module
from api.services.pipecat.realtime.gemini_live import (
    _LOOP_CONNECTION_EPOCH,
    DograhGeminiLiveLLMService,
)
from pipecat.frames.frames import EndFrame, InputAudioRawFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.asyncio.task_manager import TaskManager

GEMINI_3_MODEL = "models/gemini-3.1-flash-live-preview"
TRIGGER = 5000


@pytest.fixture(autouse=True)
def _fixed_trigger(monkeypatch):
    monkeypatch.setattr(
        gemini_live_module, "CONTEXT_COMPACTION_TRIGGER_TOKENS", TRIGGER
    )


class _FakeSession:
    """Like google-genai's session: ``receive()`` yields one turn's messages
    and ends after the turn_complete message; reads fail once closed."""

    _ids = itertools.count(1)

    def __init__(self, reads: list):
        self.id = next(self._ids)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.sent: list[str] = []
        self.payloads: list[tuple[str, dict]] = []
        self._reads = reads

    async def receive(self):
        self._reads.append((self.id, id(asyncio.current_task())))
        while True:
            if self.closed:
                raise ConnectionResetError("session closed")
            message = await self.queue.get()
            if message is None:
                raise ConnectionResetError("session closed")
            yield message
            if message.server_content and message.server_content.turn_complete:
                return

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)

    def __getattr__(self, name):  # send_client_content, send_realtime_input, ...
        async def _send(*args, **kwargs):
            self.sent.append(name)
            self.payloads.append((name, kwargs))

        return _send

    def audio_sent(self) -> list[bytes]:
        return [
            kwargs["audio"].data
            for name, kwargs in self.payloads
            if name == "send_realtime_input" and kwargs.get("audio") is not None
        ]


class _Gemini:
    """Stands in for ``client.aio.live.connect``."""

    def __init__(
        self,
        latency: float,
        fail_from: int | None = None,
        fail_attempts: set[int] | None = None,
    ):
        self.latency = latency
        self.fail_from = fail_from  # connection attempts from this index on fail
        self.fail_attempts = fail_attempts or set()  # these attempt indexes fail
        self.attempts = 0
        self.sessions: list[_FakeSession] = []
        self.owner: dict[int, int] = {}
        self.reads: list[tuple[int, int]] = []

    def __call__(self, **kwargs):
        gemini = self

        class _Connect:
            async def __aenter__(self):
                attempt = gemini.attempts
                gemini.attempts += 1
                await asyncio.sleep(gemini.latency)
                if attempt in gemini.fail_attempts or (
                    gemini.fail_from is not None
                    and len(gemini.sessions) >= gemini.fail_from
                ):
                    raise ConnectionRefusedError("simulated connect failure")
                session = _FakeSession(gemini.reads)
                gemini.sessions.append(session)
                gemini.owner[session.id] = id(asyncio.current_task())
                return session

            async def __aexit__(self, *exc_info):
                return False

        return _Connect()

    def foreign_reads(self):
        """Reads of a session by a loop other than the one that opened it."""
        return [(sid, task) for sid, task in self.reads if self.owner.get(sid) != task]


class _Service(DograhGeminiLiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=None))
        )


async def _start(
    latency: float,
    fail_from: int | None = None,
    fail_attempts: set[int] | None = None,
):
    service = _Service(
        api_key="test-key",
        settings=_Service.Settings(model=GEMINI_3_MODEL, system_instruction="sys"),
    )
    # pipecat's own task manager, as in a call: its cancel_task swallows the
    # CancelledError of a task that cancels itself, which is exactly what an
    # in-loop refresh does to the outgoing connection.
    service._task_manager = TaskManager(loop=asyncio.get_running_loop())
    for name in (
        "push_frame",
        "push_error",
        "stop_all_metrics",
        "start_ttfb_metrics",
        "stop_ttfb_metrics",
        "start_llm_usage_metrics",
        "start_processing_metrics",
        "stop_processing_metrics",
        "broadcast_interruption",
        "queue_frame",
    ):
        setattr(service, name, AsyncMock())
    context = LLMContext()
    context.add_message({"role": "user", "content": "namaste"})
    context.add_message({"role": "assistant", "content": "ji boliye"})
    service._context = context
    service._handled_initial_context = True

    gemini = _Gemini(latency, fail_from, fail_attempts)
    service._client.aio.live.connect = gemini
    await service._connect(session_resumption_handle=None)
    await _until(lambda: service._session is not None)
    service._ready_for_realtime_input = True
    return service, gemini


async def _until(condition, timeout: float = 5.0):
    deadline = time.perf_counter() + timeout
    while not condition():
        if time.perf_counter() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def _end_bot_turn(session: _FakeSession, prompt_tokens: int):
    session.queue.put_nowait(
        gt.LiveServerMessage(
            server_content=gt.LiveServerContent(turn_complete=True),
            usage_metadata=gt.UsageMetadata(
                prompt_token_count=prompt_tokens,
                response_token_count=10,
                total_token_count=prompt_tokens + 10,
            ),
        )
    )


async def _arm(service):
    """One over-budget bot turn: compaction becomes due at the next turn end."""
    service._bot_is_responding = True
    _end_bot_turn(service._session, TRIGGER + 1000)
    await _until(lambda: service._context_compaction_pending)


def _refreshed(service, gemini, before: int) -> bool:
    return (
        len(gemini.sessions) > before
        and service._session is gemini.sessions[-1]
        and not service._awaiting_context_compaction_seed
    )


async def _refresh_once(service, gemini) -> float:
    """Arm compaction with one over-budget turn; the next turn's end runs it.
    Returns the seconds from that turn ending to the new session being live."""
    await _arm(service)
    before = len(gemini.sessions)
    service._bot_is_responding = True
    started = time.perf_counter()
    _end_bot_turn(service._session, TRIGGER + 1000)
    await _until(lambda: _refreshed(service, gemini, before))
    return time.perf_counter() - started


def _live_loops() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().endswith("_connection_task_handler")
    ]


def _audio(tag: bytes) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=tag * 160, sample_rate=16000, num_channels=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("latency", [0.0, 0.05, 0.3])
async def test_compaction_refresh_takes_about_as_long_as_the_connect(latency):
    service, gemini = await _start(latency)
    try:
        elapsed = await _refresh_once(service, gemini)
        await asyncio.sleep(0.1)

        # Previously a flat ~3s: the refresh waited on its own task.
        assert elapsed < latency + 0.5
        assert len(_live_loops()) == 1
        assert gemini.foreign_reads() == []
        assert "send_client_content" in gemini.sessions[-1].sent  # reseeded
        service.push_error.assert_not_awaited()
        assert service._consecutive_failures == 0
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_repeated_refreshes_leave_one_loop_and_no_cross_reads():
    """The shape of the prod call that surfaced this: 13 refreshes in 8 minutes."""
    service, gemini = await _start(0.05)
    try:
        timings = [await _refresh_once(service, gemini) for _ in range(13)]
        await asyncio.sleep(0.1)

        assert max(timings) < 0.5
        assert len(gemini.sessions) == 14
        assert len(_live_loops()) == 1
        assert gemini.foreign_reads() == []
        service.push_error.assert_not_awaited()
        assert service._consecutive_failures == 0
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_node_transition_refresh_from_its_own_task_still_retires_the_old_loop():
    service, gemini = await _start(0.05)
    try:
        old_loop = service._connection_task
        await asyncio.wait_for(
            asyncio.get_running_loop().create_task(
                service._reconnect_for_node_transition()
            ),
            timeout=5,
        )
        await asyncio.sleep(0.2)

        assert old_loop.done()
        assert len(gemini.sessions) == 2
        assert len(_live_loops()) == 1
        service.push_error.assert_not_awaited()
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_real_failures_of_the_new_connection_are_still_reported():
    """The superseded-loop guard must not hide genuine failures: when every new
    connection fails, they are counted and end in one reported error."""
    service, gemini = await _start(0.02, fail_from=1)
    try:
        await _arm(service)
        service._bot_is_responding = True
        _end_bot_turn(service._session, TRIGGER + 1000)
        await _until(lambda: service.push_error.await_count >= 1)

        assert service._consecutive_failures == gemini_live_module_max_failures()
        assert len(gemini.sessions) == 1
    finally:
        await service._disconnect()


async def _start_refresh(service, gemini) -> int:
    """End an armed turn and return once the refresh is in flight."""
    before = len(gemini.sessions)
    service._bot_is_responding = True
    _end_bot_turn(service._session, TRIGGER + 1000)
    await _until(lambda: service._awaiting_context_compaction_seed)
    return before


@pytest.mark.asyncio
async def test_caller_audio_during_a_refresh_reaches_the_new_session_in_order():
    service, gemini = await _start(0.3)
    try:
        old = service._session
        await _arm(service)
        before = await _start_refresh(service, gemini)
        service._user_is_speaking = True  # the caller starts talking in the gap
        for tag in (b"a", b"b", b"c"):
            await service._send_user_audio(_audio(tag))
        await _until(lambda: _refreshed(service, gemini, before))

        new = gemini.sessions[-1]
        assert new.audio_sent() == [b"a" * 160, b"b" * 160, b"c" * 160]
        assert old.audio_sent() == []
        # Seeded first, so the replayed words land on the restored context.
        assert new.sent.index("send_client_content") < new.sent.index(
            "send_realtime_input"
        )
        assert service._compaction_audio_frames == []
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_tool_result_landing_mid_refresh_is_delivered_to_the_new_session():
    service, gemini = await _start(0.3)
    try:
        await _arm(service)
        before = await _start_refresh(service, gemini)
        delivered = await service._tool_result(
            "call-1", "vectus_area_manager_lookup", {"status": "ok"}
        )
        assert delivered is False  # no session in the gap: queued, not lost
        await _until(lambda: _refreshed(service, gemini, before))
        await _until(lambda: not service._pending_tool_results)

        assert "send_tool_response" in gemini.sessions[-1].sent
        assert "call-1" in service._completed_tool_calls
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_refresh_waits_out_the_callers_utterance_then_runs():
    service, gemini = await _start(0.05)
    try:
        await _arm(service)
        service._user_is_speaking = True
        service._bot_is_responding = True
        _end_bot_turn(service._session, TRIGGER + 1000)
        await asyncio.sleep(0.2)
        assert len(gemini.sessions) == 1  # not while the caller talks
        assert service._context_compaction_pending

        service._user_is_speaking = False
        elapsed = await _refresh_once(service, gemini)
        assert elapsed < 0.5
        assert len(_live_loops()) == 1
    finally:
        await service._disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("latency", [0.0, 0.3])
async def test_barge_in_refresh_from_outside_the_loop_retires_the_old_loop(latency):
    """An interruption ends the bot turn from the frame-processing task, not the
    receive loop, so that refresh does wait for the old loop."""
    service, gemini = await _start(latency)
    try:
        old_loop = service._connection_task
        await _arm(service)
        before = len(gemini.sessions)
        service._bot_is_responding = True
        started = time.perf_counter()
        await asyncio.get_running_loop().create_task(service._handle_interruption())
        await _until(lambda: _refreshed(service, gemini, before))
        await asyncio.sleep(0.1)

        assert time.perf_counter() - started < latency + 0.5
        assert old_loop.done()
        assert len(_live_loops()) == 1
        assert gemini.foreign_reads() == []
        service.push_error.assert_not_awaited()
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_no_refresh_when_the_turn_end_releases_a_hang_up():
    """An EndFrame held back until the bot finished speaking is released by this
    same turn end. Opening a fresh session for a call that is hanging up only
    buys a connection that the EndFrame then tears down."""
    service, gemini = await _start(0.05)
    try:
        await _arm(service)
        end_frame = EndFrame()
        service._end_frame_pending_bot_turn_finished = end_frame
        service._bot_is_responding = True
        _end_bot_turn(service._session, TRIGGER + 1000)
        await _until(lambda: service.queue_frame.await_count >= 1)
        await asyncio.sleep(0.2)

        service.queue_frame.assert_awaited_with(end_frame)
        assert len(gemini.sessions) == 1
        assert gemini.attempts == 1
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_hang_up_during_the_refresh_leaves_nothing_running():
    service, gemini = await _start(0.3)
    try:
        await _arm(service)
        await _start_refresh(service, gemini)
        await service._disconnect()  # what stop() does on EndFrame
        await asyncio.sleep(0.5)

        assert _live_loops() == []
        assert len(gemini.sessions) == 1  # the refresh's connect never landed
        service.push_error.assert_not_awaited()
    finally:
        await service._disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [0.0, 0.001, 0.005])
async def test_hang_up_racing_the_refresh_teardown_leaves_nothing_after_cleanup(
    offset,
):
    """stop() and the in-loop refresh can both be inside _disconnect at once.
    Whatever interleaving wins, pipeline cleanup (another _disconnect) must
    leave no connection loop behind."""
    service, gemini = await _start(0.05)
    try:
        await _arm(service)
        service._bot_is_responding = True
        _end_bot_turn(service._session, TRIGGER + 1000)
        if offset:
            await asyncio.sleep(offset)
        await service._disconnect()
        await asyncio.sleep(0.2)
        await service._disconnect()  # cleanup()
        await asyncio.sleep(0.1)

        assert _live_loops() == []
        service.push_error.assert_not_awaited()
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_a_due_node_transition_takes_over_the_refresh():
    service, gemini = await _start(0.05)
    try:
        await _arm(service)
        scheduled = []

        def _schedule(fcs):
            scheduled.append(fcs)
            service._transition_function_call_task = service.create_task(
                asyncio.sleep(0.3), "transition"
            )

        service._schedule_node_transition_function_calls = _schedule
        service._pending_node_transition_function_calls = [
            SimpleNamespace(function_name="go_to_closing")
        ]
        service._bot_is_responding = True
        _end_bot_turn(service._session, TRIGGER + 1000)
        await _until(lambda: scheduled)
        await asyncio.sleep(0.2)

        assert len(gemini.sessions) == 1  # the transition's reconnect compacts
        assert not service._context_compaction_pending
    finally:
        await service._disconnect()


@pytest.mark.asyncio
async def test_one_failed_connect_after_a_refresh_retries_and_still_reseeds():
    service, gemini = await _start(0.1, fail_attempts={1})
    try:
        await _arm(service)
        before = await _start_refresh(service, gemini)
        service._user_is_speaking = True
        await service._send_user_audio(_audio(b"x"))
        await _until(lambda: _refreshed(service, gemini, before))
        await asyncio.sleep(0.1)

        new = gemini.sessions[-1]
        assert gemini.attempts == 3
        assert "send_client_content" in new.sent  # reseeded after the retry
        assert new.audio_sent() == [b"x" * 160]
        assert service._consecutive_failures == 1
        assert len(_live_loops()) == 1
        service.push_error.assert_not_awaited()
    finally:
        await service._disconnect()


def gemini_live_module_max_failures() -> int:
    from pipecat.services.google.gemini_live import llm as upstream

    return upstream.MAX_CONSECUTIVE_FAILURES


def test_loop_epoch_is_unbound_outside_a_connection_loop():
    # The guard treats "no epoch" as "not a superseded loop", so callers outside
    # any connection loop keep upstream's error handling unchanged.
    assert _LOOP_CONNECTION_EPOCH.get() is None
