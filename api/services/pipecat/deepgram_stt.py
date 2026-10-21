"""Deepgram STT that reports a rejected key and backs off between reconnects.

pipecat's ``DeepgramSTTService._connection_handler`` catches every connect
failure, logs "Connection lost, will retry" and loops straight back. When the
key is rejected (the handshake fails with HTTP 401/403) that means:

- the call never gets an error: no ErrorFrame, so the pipeline's error
  handling and campaign protection never see it, and the caller just hears
  the bot not understand them;
- the loop retries as fast as the network allows for the rest of the call.

This keeps the connection lifecycle identical and changes only the failure
branch: a rejected key is reported (with the provider's own exception, so its
status can be read) and ends the retries; any other failure waits with
exponential backoff before reconnecting, and the backoff resets once a
connection is established.
"""

import asyncio

from loguru import logger

from api.services.pipecat.provider_rejection import (
    PROVIDER_PAUSE_STATUSES,
    provider_http_status,
)
from pipecat.services.deepgram.stt import DeepgramSTTService

try:
    from deepgram.core.events import EventType
except ModuleNotFoundError:  # pragma: no cover - deepgram is an optional extra
    EventType = None

RECONNECT_BACKOFF_START_SECS = 0.5
RECONNECT_BACKOFF_MAX_SECS = 5.0


class DograhDeepgramSTTService(DeepgramSTTService):
    """``DeepgramSTTService`` with a reported, non-retried key rejection."""

    async def _connection_handler(self):
        backoff = RECONNECT_BACKOFF_START_SECS
        while True:
            connect_kwargs = self._build_connect_kwargs()
            keepalive_task = None
            try:
                async with self._client.listen.v1.connect(**connect_kwargs) as connection:
                    self._connection = connection
                    self._connection_ready.set()
                    backoff = RECONNECT_BACKOFF_START_SECS
                    connection.on(EventType.MESSAGE, self._on_message)
                    connection.on(EventType.ERROR, self._on_error)

                    logger.debug(f"{self}: Websocket connection initialized")

                    keepalive_task = self.create_task(
                        self._keepalive_handler(), f"{self}::keepalive"
                    )
                    await connection.start_listening()
            except Exception as e:
                if provider_http_status(e) in PROVIDER_PAUSE_STATUSES:
                    await self.push_error(
                        error_msg=f"Deepgram rejected the connection: {e}", exception=e
                    )
                    return
                logger.warning(
                    f"{self}: Connection lost, will retry in {backoff:.1f}s: {e}"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECS)
            finally:
                self._connection_ready.clear()
                self._connection = None
                if keepalive_task:
                    await self.cancel_task(keepalive_task)
