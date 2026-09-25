"""The stale-connection race behind the run-442 incident.

``_disconnect()`` cancels the outgoing connection's background receive loop
with only a best-effort ~1s timeout (upstream `cancel_task`), and proceeds
regardless of whether that succeeded. If the loop is still alive when the next
connection starts — as happened repeatedly on a live Vectus Smart Care call
that ran past the compaction threshold — its next iteration reads
``self._session`` after it has already been cleared or reassigned by the
newer connection. That produced ``'NoneType' object has no attribute
'receive'``, which the loop's own error handling treated as a *real* failure
and reconnected for again, discarding the newer connection that was already
under way, then getting stuck repeating that same collision every 15–40
seconds while the caller heard nothing — for most of a 10-minute call.

The fix is a connection "epoch", held in Dograh's subclass
(`api/services/pipecat/realtime/gemini_live.py`) so it ships without a pipecat
change: ``_disconnect()`` bumps it immediately, and a connection's receive loop
captures its own epoch at the start of its life (in a context variable, which
is private to that loop's task).
An error that arrives after that number has moved on belongs to a connection
nobody is using any more and is dropped instead of triggering another
reconnect. These tests reproduce the exact timing (a receive loop blocked mid
turn, exactly where a live connection sits between messages) rather than
asserting on the epoch counter directly, so they fail if the guard is removed
or misplaced, not just if its bookkeeping changes shape.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService

GEMINI_3_MODEL = "models/gemini-3.1-flash-live-preview"


class _TestDograhGeminiLiveLLMService(DograhGeminiLiveLLMService):
    """Dograh Gemini service with client creation stubbed for unit tests."""

    def create_client(self):
        self._client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=None))
        )


def _make_service() -> _TestDograhGeminiLiveLLMService:
    service = _TestDograhGeminiLiveLLMService(
        api_key="test-key",
        settings=_TestDograhGeminiLiveLLMService.Settings(model=GEMINI_3_MODEL),
    )
    service.stop_all_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    service.start_llm_usage_metrics = AsyncMock()
    service.push_error = AsyncMock()
    return service


class _BlockedReceive:
    """A fake ``session.receive()``: sits exactly where a real connection sits
    between turns (awaiting the next message), then fails once released —
    standing in for whatever eventually goes wrong with a superseded
    connection's dangling stream (a closed socket, a read on a cleared
    session, and so on; the exact upstream exception is not the point here).
    """

    def __init__(self, released: asyncio.Event, reached: asyncio.Event):
        self._released = released
        self._reached = reached

    async def receive(self):
        self._reached.set()
        await self._released.wait()
        raise ConnectionResetError("simulated: the underlying connection is gone")
        yield  # pragma: no cover - unreachable; makes this an async generator


class _ConnectContextManager:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _wire_stuck_connection(service) -> tuple[asyncio.Event, asyncio.Event]:
    """Point the service at a connection whose receive loop blocks until the
    test releases it. Returns (released, reached) events."""
    released = asyncio.Event()
    reached = asyncio.Event()
    session = _BlockedReceive(released, reached)
    service._client.aio.live.connect = lambda **kwargs: _ConnectContextManager(session)
    return released, reached


async def _start_connection_task(service) -> asyncio.Task:
    task = asyncio.ensure_future(
        service._connection_task_handler(config=SimpleNamespace())
    )
    return task


@pytest.mark.asyncio
async def test_a_superseded_connections_late_failure_is_not_treated_as_a_new_error():
    """The exact run-442 shape: a disconnect (for an unrelated reason — a
    compaction refresh, a node transition) has already moved the connection
    on by the time this old loop's blocked read finally fails. It must stand
    down rather than reconnect again on top of whatever came after it.
    """
    service = _make_service()
    released, reached = _wire_stuck_connection(service)
    # _handle_connection_error is left real: it holds the guard. Only the
    # consequences a stale error must not have are observed.
    service._reconnect = AsyncMock()

    task = await _start_connection_task(service)
    await asyncio.wait_for(reached.wait(), timeout=1.0)

    # A disconnect happens now, for its own reasons — exactly what
    # _compact_context_via_reconnect / _reconnect_for_node_transition do.
    service._connection_epoch += 1
    service._session = None

    released.set()
    await asyncio.wait_for(task, timeout=1.0)

    service._reconnect.assert_not_awaited()
    service.push_error.assert_not_awaited()  # no "failed after 3 attempts"
    assert service._consecutive_failures == 0  # not counted as a failure


@pytest.mark.asyncio
async def test_a_still_current_connections_failure_still_reconnects_normally():
    """The fix must not suppress real error handling: a connection that has
    NOT been superseded reconnects on failure exactly as before.
    """
    service = _make_service()
    released, reached = _wire_stuck_connection(service)
    service._reconnect = AsyncMock()

    task = await _start_connection_task(service)
    await asyncio.wait_for(reached.wait(), timeout=1.0)

    # No disconnect happens this time — same connection, same epoch.
    released.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert service._consecutive_failures == 1  # counted, as before
    service._reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_advances_the_epoch_before_anything_else_can_run():
    """_disconnect() must mark the outgoing connection stale as one of its
    first acts — before any of its own awaits (stop_all_metrics,
    cancel_task, session.close) can hand control back to that connection's
    still-running loop.
    """
    service = _make_service()
    service.cancel_task = AsyncMock()
    before = service._connection_epoch

    await service._disconnect()

    # Advanced, not "+1": with the guard also present in pipecat, both bump.
    assert service._connection_epoch > before


# ----------------------------------------------------------------------
# Dograh's own belt-and-suspenders: actually wait for the outgoing
# connection to finish before starting the next one, instead of trusting
# _disconnect()'s best-effort cancellation.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_outgoing_connection_stopped_does_nothing_without_a_task():
    service = _make_service()

    await service._await_outgoing_connection_stopped(None)  # must not raise


@pytest.mark.asyncio
async def test_await_outgoing_connection_stopped_returns_once_the_task_finishes():
    service = _make_service()
    still_running = asyncio.Event()

    async def _slow_shutdown():
        await still_running.wait()

    task = asyncio.ensure_future(_slow_shutdown())
    waiter = asyncio.ensure_future(service._await_outgoing_connection_stopped(task))
    await asyncio.sleep(0.01)
    assert not waiter.done()  # genuinely waiting, not a no-op

    still_running.set()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_await_outgoing_connection_stopped_gives_up_after_a_bound_and_proceeds():
    """A connection that never actually stops must not block reconnection
    forever — that would turn a freeze into a permanent one."""
    service = _make_service()

    task = asyncio.ensure_future(asyncio.sleep(10))
    try:
        await service._await_outgoing_connection_stopped(
            task, timeout=0.02
        )  # must not raise
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_compaction_waits_for_the_outgoing_connection_before_reconnecting():
    """The two calls must be strictly ordered: the old connection's shutdown
    is given a real chance to finish before the new one starts, not raced."""
    service = _make_service()
    order: list[str] = []
    still_running = asyncio.Event()

    async def _slow_shutdown():
        await still_running.wait()

    service._connection_task = asyncio.ensure_future(_slow_shutdown())
    service._session_resumption_handle = "stale"

    async def _disconnect():
        order.append("disconnect")

    async def _connect(**kwargs):
        order.append("connect")

    service._disconnect = _disconnect
    service._connect = _connect

    run = asyncio.ensure_future(service._compact_context_via_reconnect())
    await asyncio.sleep(0.01)
    assert order == ["disconnect"]  # connect must not have started yet

    still_running.set()
    await asyncio.wait_for(run, timeout=1.0)
    assert order == ["disconnect", "connect"]


@pytest.mark.asyncio
async def test_await_outgoing_connection_stopped_returns_at_once_from_inside_that_connection():
    """A compaction refresh runs inside the outgoing connection's own receive
    loop (turn_complete -> bot stopped responding -> _maybe_compact_context).
    A task cannot finish while it waits on itself, so waiting there only ever
    ran out the timeout: on prod every refresh took ~3s, during which the bot
    could not hear the caller. It must return immediately instead."""
    service = _make_service()

    async def _refresh_from_inside_the_connection() -> float:
        started = time.perf_counter()
        await service._await_outgoing_connection_stopped(
            asyncio.current_task(), timeout=1.0
        )
        return time.perf_counter() - started

    elapsed = await asyncio.wait_for(
        asyncio.ensure_future(_refresh_from_inside_the_connection()), timeout=3.0
    )
    assert elapsed < 0.1
