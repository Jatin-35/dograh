"""Call-control strategies for TATA SmartFlo.

``SmartfloFrameSerializer`` carries the audio; these two carry everything that
happens *to* the call. They are separate because SmartFlo's media socket has no
hangup or transfer event — an integrator may send only ``media``, ``mark`` and
``clear`` — so both operations have to leave over the REST API instead.

Both strategies hit endpoints that share one rate-limit budget with dialling —
SmartFlo's limit is *combined across all APIs* and equals the account's CPS — so
neither retries on 429. A retried hangup would spend budget that a waiting call
needs to be placed.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger
from pipecat.serializers.call_strategies import HangupStrategy, TransferStrategy

from .auth import TataSmartfloAuthError, token_cache

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class _SmartfloCallOperation:
    """Shared auth and POST handling for the two call-control endpoints."""

    def __init__(
        self,
        *,
        api_base: str,
        email: str,
        password: str,
        ref_id: str | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._email = email
        self._password = password
        # Outbound knows its ref_id from the Click-to-Call response. Inbound
        # does not — there was no initiation call — so it falls back to the
        # call_id the serializer carries. SmartFlo accepts either identifier.
        self._ref_id = ref_id

    def _identifier(self, context: dict[str, Any]) -> dict[str, str] | None:
        """Address the call the way SmartFlo expects.

        ``ref_id`` when we have one — the Click-to-Call reference for an
        outbound call — otherwise the inbound ``call_id``. The serializer
        supplies both under SmartFlo's own names; the constructor argument wins
        because it was read from the run record rather than the socket.
        """
        ref_id = self._ref_id or context.get("ref_id")
        if ref_id:
            return {"ref_id": str(ref_id)}
        call_id = context.get("call_id")
        if call_id:
            return {"call_id": str(call_id)}
        return None

    def _call_key(self, context: dict[str, Any]) -> str | None:
        """The bare identifier, for keyed lookups rather than request payloads."""
        identifier = self._identifier(context)
        if not identifier:
            return None
        return next(iter(identifier.values()))

    async def _post(self, path: str, payload: dict[str, Any]) -> bool:
        try:
            token = await token_cache.get_token(
                self._api_base, self._email, self._password
            )
        except TataSmartfloAuthError as exc:
            logger.error(f"SmartFlo call operation could not authenticate: {exc}")
            return False

        url = f"{self._api_base}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    body = await response.json(content_type=None)

                    if response.status == 401:
                        # Our clock said the token was good; SmartFlo disagreed.
                        # Mint once more, then give up — a loop here would burn
                        # the shared rate-limit budget during a live call.
                        token_cache.invalidate(self._api_base, self._email)
                        token = await token_cache.get_token(
                            self._api_base,
                            self._email,
                            self._password,
                            force_refresh=True,
                        )
                        async with session.post(
                            url,
                            json=payload,
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=_REQUEST_TIMEOUT,
                        ) as retry:
                            body = await retry.json(content_type=None)
                            response_status = retry.status
                    else:
                        response_status = response.status
        except (aiohttp.ClientError, TataSmartfloAuthError) as exc:
            logger.error(f"SmartFlo {path} request failed: {exc}")
            return False

        if response_status == 429:
            # Shared CPS budget, exhausted. Surfaced rather than retried.
            logger.warning(f"SmartFlo rate limit hit on {path}")
            return False

        succeeded = response_status == 200 and bool((body or {}).get("success"))
        if not succeeded:
            logger.error(
                f"SmartFlo {path} returned HTTP {response_status}: "
                f"{(body or {}).get('message')}"
            )
        return succeeded


class TataSmartfloHangupStrategy(_SmartfloCallOperation, HangupStrategy):
    """End a call through ``POST /v1/call/hangup``.

    SmartFlo's streaming protocol has no hangup event — an integrator may send
    only ``media``, ``mark`` and ``clear`` — so ending a call the agent decided
    to end has to go through the REST API.

    A 200 means *accepted*, not disconnected. SmartFlo's own documentation says
    to watch the webhooks to confirm the call actually dropped, so callers must
    not treat success here as the call being over.
    """

    async def execute_hangup(self, context: dict[str, Any]) -> bool:
        identifier = self._identifier(context)
        if identifier is None:
            logger.error(
                "SmartFlo hangup has neither ref_id nor call_id; cannot identify "
                "the call to end."
            )
            return False

        logger.info(f"SmartFlo hangup requested for {identifier}")
        return await self._post("/v1/call/hangup", identifier)


class TataSmartfloTransferStrategy(_SmartfloCallOperation, TransferStrategy):
    """Transfer a live call through ``POST /v1/call/options`` with ``type: 4``.

    SmartFlo distinguishes two kinds of destination and they are not
    interchangeable: ``agent_id`` routes to a SmartFlo agent, while ``intercom``
    covers external mobile numbers, extensions, departments, queues, IVRs and
    SIP trunks. Sending a phone number as ``agent_id`` is rejected.
    """

    _TRANSFER = 4

    def __init__(
        self,
        *,
        api_base: str,
        email: str,
        password: str,
        ref_id: str | None = None,
        agent_id: str | None = None,
        intercom: str | None = None,
    ) -> None:
        super().__init__(
            api_base=api_base, email=email, password=password, ref_id=ref_id
        )
        self._agent_id = agent_id
        self._intercom = intercom

    async def execute_transfer(self, context: dict[str, Any]) -> bool:
        identifier = self._identifier(context)
        if identifier is None:
            logger.error("SmartFlo transfer has no ref_id or call_id.")
            return False

        destination: dict[str, Any]
        if self._agent_id:
            # A configured SmartFlo agent is an explicit routing decision; it
            # needs no destination lookup.
            destination = {"agent_id": self._agent_id}
            kind = "agent"
        else:
            # The destination the agent resolved at runtime is not on this
            # context dict — serializers build that from their own state. It
            # lives in the Redis TransferContext the transfer tool stored
            # before dispatching, and it describes *this* call, so it outranks
            # whatever was configured up front.
            intercom = await self._resolved_destination(context) or self._intercom
            if not intercom:
                logger.error(
                    "SmartFlo transfer needs agent_id (a SmartFlo agent) or intercom "
                    "(external number, extension, department, queue, IVR or SIP trunk)."
                )
                return False
            destination = {"intercom": intercom}
            kind = "intercom"

        payload = {"type": self._TRANSFER, **identifier, **destination}
        # The destination is a phone number — log which kind was chosen, never
        # the number itself.
        logger.info(f"SmartFlo transfer requested for {identifier} to {kind}")
        return await self._post("/v1/call/options", payload)

    async def _resolved_destination(self, context: dict[str, Any]) -> str | None:
        """Read the target number out of the in-flight transfer context.

        ``pipecat_engine_custom_tools`` stores a ``TransferContext`` keyed by
        ``gathered_context["call_id"]`` before it dispatches a transfer, which
        for SmartFlo is the ref_id on outbound and the call id on inbound —
        the same value ``_identifier`` picks.
        """
        call_key = self._call_key(context)
        if not call_key:
            return None

        try:
            from api.services.telephony.call_transfer_manager import (
                get_call_transfer_manager,
            )

            manager = await get_call_transfer_manager()
            transfer_context = await manager.find_transfer_context_for_call(call_key)
        except Exception as exc:  # noqa: BLE001 - never fail a live call over this
            logger.error(f"SmartFlo could not read the transfer context: {exc}")
            return None

        if not transfer_context:
            logger.error(f"SmartFlo transfer has no stored context for {call_key}")
            return None
        return transfer_context.target_number
