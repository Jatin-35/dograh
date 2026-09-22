"""TATA SmartFlo telephony provider.

Outbound goes through Click-to-Call: ``POST /v1/click_to_call_support`` queues
the dial and SmartFlo streams the answered call to whichever voice bot the API
key is bound to. The response carries a ``ref_id``, and that value is the only
handle on the call afterwards — transfer, hangup and webhook correlation all
key on it — so it is returned as ``provider_metadata`` and persisted on the run.

Inbound is served by ``routes.py``: SmartFlo calls our connect endpoint with the
call details and we answer with the socket URL. Nothing here is involved.

Rate limiting deserves a note. SmartFlo's limit is *combined across all APIs*
and equals the account's CPS, so on a CPS-3 trunk that is three requests per
second covering auth, dialling, transfer and hangup together. 429 is surfaced
rather than retried: the dispatcher already understands backpressure, and a
retry here would spend the budget a queued call is waiting for.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from api.enums import WorkflowRunMode
from api.services.telephony.base import (
    CallInitiationResult,
    NormalizedInboundData,
    TelephonyProvider,
)

from .auth import TataSmartfloAuthError, token_cache
from .config import DEFAULT_TATA_SMARTFLO_API_BASE

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class TataSmartfloProvider(TelephonyProvider):
    """Telephony provider for TATA SmartFlo."""

    PROVIDER_NAME = WorkflowRunMode.TATA_SMARTFLO.value

    # SmartFlo is pointed at us by configuration, not by a per-call answer URL:
    # inbound is bound on the DID, outbound on the Click-to-Call key. There is
    # no TwiML-style webhook to serve, so this stays empty.
    WEBHOOK_ENDPOINT = ""

    def __init__(self, config: Dict[str, Any]):
        self.api_base = (
            config.get("api_base") or DEFAULT_TATA_SMARTFLO_API_BASE
        ).rstrip("/")
        self.email = config.get("email")
        self.password = config.get("password")
        self.api_key = config.get("api_key")
        self.caller_id = config.get("caller_id")
        self.from_numbers: List[str] = config.get("from_numbers") or []
        self.connect_secret = config.get("connect_secret")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _bearer(self, *, force_refresh: bool = False) -> str:
        if not self.email or not self.password:
            raise TataSmartfloAuthError(
                "SmartFlo requires email and password; it issues no static token."
            )
        return await token_cache.get_token(
            self.api_base, self.email, self.password, force_refresh=force_refresh
        )

    async def _authed_post(
        self, path: str, payload: Dict[str, Any]
    ) -> tuple[int, Dict[str, Any]]:
        """POST with a bearer token, re-minting once on 401.

        Once, not in a loop: a 401 that survives a fresh token is a credential
        problem, and retrying it only spends rate-limit budget.
        """
        url = f"{self.api_base}{path}"
        token = await self._bearer()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                body = await response.json(content_type=None)
                if response.status != 401:
                    return response.status, (body or {})

            token = await self._bearer(force_refresh=True)
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT,
            ) as retry:
                return retry.status, (await retry.json(content_type=None) or {})

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def initiate_call(
        self,
        to_number: str,
        webhook_url: str,
        workflow_run_id: Optional[int] = None,
        from_number: Optional[str] = None,
        **kwargs: Any,
    ) -> CallInitiationResult:
        """Queue an outbound call through Click-to-Call.

        ``webhook_url`` is accepted for interface compatibility but unused:
        SmartFlo binds the destination bot to the API key, so there is no
        per-call URL to hand over.
        """
        if not self.api_key:
            raise ValueError(
                "SmartFlo outbound calls require the Click-to-Call Support API "
                "key. Inbound-only configurations can omit it."
            )

        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "customer_number": to_number,
            # SmartFlo supports no other value; synchronous mode does not exist.
            "async": 1,
        }

        caller_id = from_number or self.caller_id
        if caller_id:
            payload["caller_id"] = caller_id

        if workflow_run_id is not None:
            # Echoed back on webhooks, which is how a lifecycle event is tied to
            # a run without depending on ref_id having been persisted yet.
            payload["custom_identifier"] = {"workflow_run_id": str(workflow_run_id)}

        for optional in ("customer_ring_timeout", "call_timeout"):
            if optional in kwargs and kwargs[optional] is not None:
                payload[optional] = kwargs[optional]

        status, body = await self._authed_post("/v1/click_to_call_support", payload)

        if status == 429:
            raise RuntimeError(
                "SmartFlo rate limit reached. The limit is shared across all "
                "SmartFlo APIs and equals the account's CPS."
            )
        if status != 200 or not body.get("success"):
            raise RuntimeError(
                f"SmartFlo Click-to-Call failed (HTTP {status}): "
                f"{body.get('message') or body}"
            )

        ref_id = body.get("ref_id")
        if not ref_id:
            # Without ref_id the call will connect and then be uncontrollable —
            # no transfer, no hangup. Loud now beats silent later.
            logger.error(
                f"[run {workflow_run_id}] SmartFlo accepted the call but returned "
                "no ref_id; transfer and hangup will be unavailable for it."
            )

        logger.info(
            f"[run {workflow_run_id}] SmartFlo call queued to {to_number} "
            f"(ref_id={ref_id})"
        )

        return CallInitiationResult(
            call_id=ref_id or "",
            status="queued",
            caller_number=caller_id,
            # Merged into the run's gathered_context by the outbound route.
            # Stored under both keys deliberately: `call_id` is the generic one
            # that db_client.get_workflow_run_by_call_id looks up through the
            # idx_workflow_runs_call_id index, which is how a lifecycle webhook
            # finds its run; `smartflo_ref_id` says what the value actually is,
            # for transport.py and for anyone reading the row later.
            provider_metadata={"call_id": ref_id, "smartflo_ref_id": ref_id},
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Call control
    # ------------------------------------------------------------------

    def supports_transfers(self) -> bool:
        """SmartFlo transfers over ``/v1/call/options`` with ``type: 4``."""
        return True

    async def transfer_call(
        self,
        destination: str,
        transfer_id: str,
        conference_name: str,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Transfer a live call to an agent or an external destination.

        SmartFlo separates the two and will not accept one for the other:
        ``agent_id`` routes to a SmartFlo agent, ``intercom`` covers external
        numbers, extensions, departments, queues, IVRs and SIP trunks.

        Unlike conference-style providers, SmartFlo moves the *existing* call
        rather than dialling a second leg, so it needs that call's identifier.
        The transfer tool does not pass one — it calls every provider with the
        same four keyword arguments — so when it is absent the id is recovered
        from the transfer context the tool stored just before dispatching.
        """
        ref_id = kwargs.get("ref_id") or kwargs.get("call_id")
        if not ref_id:
            ref_id = await self._call_id_for_transfer(transfer_id)
        if not ref_id:
            logger.error(
                f"SmartFlo transfer {transfer_id} has no call identifier: neither "
                "a ref_id/call_id argument nor a stored transfer context."
            )
            return {"status": "error", "error": "No ref_id/call_id for transfer"}

        agent_id = kwargs.get("agent_id")
        payload: Dict[str, Any] = {"type": 4, "ref_id": ref_id}
        payload.update(
            {"agent_id": agent_id} if agent_id else {"intercom": destination}
        )

        status, body = await self._authed_post("/v1/call/options", payload)
        succeeded = status == 200 and bool(body.get("success"))
        if not succeeded:
            logger.error(f"SmartFlo transfer failed (HTTP {status}): {body}")

        return {
            "status": "success" if succeeded else "error",
            "transfer_id": transfer_id,
            "raw_response": body,
        }

    @staticmethod
    async def _call_id_for_transfer(transfer_id: str) -> Optional[str]:
        """Recover the live call's identifier from the stored transfer context.

        ``pipecat_engine_custom_tools`` writes a ``TransferContext`` keyed by
        ``transfer_id`` before calling this method, carrying
        ``original_call_sid`` — which for SmartFlo is ``gathered_context
        ["call_id"]``: the ref_id on outbound, the inbound call id otherwise.
        """
        try:
            from api.services.telephony.call_transfer_manager import (
                get_call_transfer_manager,
            )

            manager = await get_call_transfer_manager()
            context = await manager.get_transfer_context(transfer_id)
        except Exception as exc:  # noqa: BLE001 - never fail a live call over this
            logger.error(f"SmartFlo could not read transfer context {transfer_id}: {exc}")
            return None

        return context.original_call_sid if context else None

    async def get_call_status(self, call_id: str) -> Dict[str, Any]:
        """SmartFlo exposes no per-call status poll on this surface.

        Status arrives by webhook instead — see ``routes.py``. Returning
        "unknown" rather than raising keeps callers that poll opportunistically
        from failing a live call.
        """
        return {"call_id": call_id, "status": "unknown", "source": "webhook-only"}

    async def get_call_cost(self, call_id: str) -> Dict[str, Any]:
        """Cost is not exposed per call; billing duration arrives on the webhook."""
        return {"call_id": call_id, "cost": None, "currency": None}

    async def get_available_phone_numbers(self) -> List[str]:
        """DIDs are configured on the telephony config, not discovered."""
        return list(self.from_numbers)

    def validate_config(self) -> bool:
        """Inbound needs credentials; outbound additionally needs the API key."""
        return bool(self.email and self.password)

    # ------------------------------------------------------------------
    # Inbound — the work happens in routes.py
    # ------------------------------------------------------------------

    @staticmethod
    def can_handle_webhook(
        webhook_data: Dict[str, Any], headers: Dict[str, str]
    ) -> bool:
        """Claim SmartFlo voice-streaming connect requests on ``/inbound/run``.

        Going through the shared dispatcher rather than a private endpoint is
        deliberate. That handler already resolves the DID to a config and org,
        verifies the signature, acquires a concurrency slot, creates the run and
        calls ``authorize_workflow_run_start`` — then hands off to
        ``start_inbound_stream`` for the provider-shaped reply. Every one of
        those steps is a thing PR #731 omitted by writing its own route.

        SmartFlo sends camelCase on the streaming connect request and
        ``$``-prefixed names on webhooks, so both spellings are matched.

        **Why the negative guard matters.** ``_detect_provider`` returns the
        *first* provider whose claim is true, and registration order puts
        ``tata_smartflo`` fourth of nine — ahead of telnyx, twilio, vobiz,
        voicelink and vonage. A false claim here does not fail loudly; it routes
        a live call belonging to another provider into SmartFlo's handling.

        So rather than relying on SmartFlo's keys being unique — which cannot be
        confirmed until Tata supplies a real payload — anything carrying another
        provider's signature is refused outright first. That makes a collision
        structurally impossible rather than merely untested.
        """
        # Another provider's marker wins, always. Cheap, and it fails safe in
        # the direction that matters: a missed SmartFlo call is a support
        # ticket, a stolen Twilio call is an outage for someone else.
        foreign_markers = (
            "CallSid",  # twilio, cloudonix
            "CallUUID",  # plivo, vobiz
            "conversation_uuid",  # vonage
            "AccountSid",
            "call_control_id",  # telnyx
        )
        if any(webhook_data.get(k) for k in foreign_markers):
            return False

        # Nested envelopes belong to telnyx (`data`) and voicelink (`call`).
        for envelope in ("data", "call"):
            if isinstance(webhook_data.get(envelope), dict):
                return False

        for header in ("telnyx-signature-ed25519", "x-twilio-signature"):
            if header in {k.lower() for k in headers}:
                return False

        has_called_number = any(
            webhook_data.get(k) for k in ("toNumber", "to_number", "$call_to_number")
        )
        has_call_id = any(
            webhook_data.get(k) for k in ("callId", "call_id", "$call_id", "$uuid")
        )
        return bool(has_called_number and has_call_id)

    @staticmethod
    def parse_inbound_webhook(webhook_data: Dict[str, Any]) -> NormalizedInboundData:
        """Normalize a SmartFlo inbound connect payload.

        SmartFlo sends ``callId``/``fromNumber``/``toNumber``; the webhook
        variants use ``$``-prefixed names, so both spellings are accepted.
        """
        def pick(*names: str) -> str:
            for name in names:
                value = webhook_data.get(name)
                if value:
                    return str(value)
            return ""

        return NormalizedInboundData(
            provider=TataSmartfloProvider.PROVIDER_NAME,
            call_id=pick("callId", "call_id", "$call_id", "uuid", "$uuid"),
            from_number=pick("fromNumber", "from_number", "$caller_id_number"),
            to_number=pick("toNumber", "to_number", "$call_to_number"),
            direction="inbound",
            call_status=pick("status", "$status") or "ringing",
            raw_data=webhook_data,
        )

    @staticmethod
    def validate_account_id(config_data: dict, webhook_account_id: str) -> bool:
        """Match a stored config by login id."""
        return bool(
            webhook_account_id
            and config_data.get("email")
            and config_data["email"] == webhook_account_id
        )

    async def verify_inbound_signature(
        self,
        url: str,
        webhook_data: Dict[str, Any],
        headers: Dict[str, str],
        body: str = "",
    ) -> bool:
        """Verify an inbound request came from SmartFlo.

        SmartFlo documents no request signing for voice streaming, so there is
        nothing cryptographic to check. What it does support is configurable
        custom headers, so a shared secret is the available mechanism — see
        ``connect_secret`` on the config.

        Returns False when a secret is configured and absent or wrong. When no
        secret is configured this returns True: refusing every call would take
        the provider down, and the operator has been told in the config UI what
        leaving it blank means.
        """
        if not self.connect_secret:
            return True

        presented = (
            headers.get("x-smartflo-secret")
            or headers.get("X-Smartflo-Secret")
            or webhook_data.get("secret")
        )
        # Compared without short-circuiting on length to avoid leaking it by timing.
        import hmac

        return bool(presented) and hmac.compare_digest(
            str(presented), str(self.connect_secret)
        )

    async def verify_webhook_signature(
        self, url: str, params: Dict[str, Any], signature: str
    ) -> bool:
        """Lifecycle webhooks use the same shared secret as inbound."""
        return await self.verify_inbound_signature(url, params, {}, "")

    def parse_status_callback(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a SmartFlo lifecycle webhook into our shared shape."""
        return {
            "call_id": data.get("$ref_id") or data.get("ref_id") or data.get("$uuid"),
            "status": data.get("$status") or data.get("status"),
            "duration": data.get("$duration") or data.get("duration"),
            "billsec": data.get("$billsec") or data.get("billsec"),
            "hangup_cause": data.get("$hangup_cause") or data.get("hangup_cause"),
            "recording_url": data.get("$recording_url") or data.get("recording_url"),
            "raw": data,
        }

    async def get_webhook_response(
        self, workflow_id: int, organization_id: int, workflow_run_id: int
    ) -> str:
        """SmartFlo has no TwiML equivalent; the connect endpoint answers JSON."""
        raise NotImplementedError(
            "SmartFlo does not use a document-style webhook response; the "
            "connect endpoint in routes.py returns the socket URL as JSON."
        )

    async def handle_websocket(
        self, websocket: Any, workflow_id: int, organization_id: int, workflow_run_id: int
    ) -> None:
        """Media is served by the shared telephony WebSocket route."""
        raise NotImplementedError(
            "SmartFlo media is handled by the shared /api/v1/telephony/ws route."
        )

    async def start_inbound_stream(
        self,
        *,
        websocket_url: str,
        workflow_run_id: int,
        normalized_data: NormalizedInboundData,
        backend_endpoint: str,
    ) -> Any:
        """Return the JSON body SmartFlo expects from the connect endpoint.

        The contract is exact and unforgiving: HTTP 200, keys spelled
        ``success`` and ``wss_url``, within 2000 ms. SmartFlo's documentation
        states that any deviation hangs the call up immediately.
        """
        return {"success": True, "wss_url": websocket_url}

    @staticmethod
    def generate_error_response(error_type: str, message: str) -> tuple:
        """Refuse a call in the shape SmartFlo understands."""
        return _smartflo_refusal(message)

    @staticmethod
    def generate_validation_error_response(error_type) -> tuple:
        """Refuse an inbound call before any socket is opened.

        This is the return value of every rejection in the shared ``/inbound/run``
        handler — no matching DID, no inbound workflow, bad signature, org at its
        concurrency limit, out of quota. Returning SmartFlo's own refusal shape
        is what turns those checks into a call that never streams.

        HTTP 200 with ``success: false`` rather than a 4xx: SmartFlo's contract
        is that the body decides, and a non-200 is documented to hang the call
        up immediately — which is the same outcome but loses the message.
        """
        from api.errors.telephony_errors import TELEPHONY_ERROR_MESSAGES, TelephonyError

        message = TELEPHONY_ERROR_MESSAGES.get(
            error_type, TELEPHONY_ERROR_MESSAGES[TelephonyError.GENERAL_AUTH_FAILED]
        )
        return _smartflo_refusal(message)


def _smartflo_refusal(message: str):
    """A refusal SmartFlo will parse.

    Their published JSON schema requires ``wss_url`` on every response (even a
    refusal), matching ``^wss://.+`` — an empty string fails that pattern, so
    this cannot simply omit or blank the value — and sets
    ``additionalProperties: false``, so no other key such as ``message`` is
    allowed. The placeholder below is syntactically valid but points nowhere;
    it exists only to satisfy the pattern on a call that ``success: false``
    already declines. The real reason is logged, not put in the response body.
    """
    import json

    from fastapi import Response

    logger.info(f"SmartFlo inbound call refused: {message}")

    return Response(
        content=json.dumps({"success": False, "wss_url": "wss://declined.invalid/"}),
        media_type="application/json",
    )
