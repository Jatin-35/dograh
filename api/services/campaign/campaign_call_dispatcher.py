import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp
from loguru import logger

from api.db import db_client
from api.db.models import QueuedRunModel, WorkflowRunModel
from api.enums import WorkflowRunState
from api.services.call_concurrency import (
    CallConcurrencyLimitError,
    CallConcurrencySlot,
    call_concurrency,
)
from api.services.call_concurrency.rate_limiter import rate_limiter
from api.services.campaign.circuit_breaker import circuit_breaker, telephony_outage
from api.services.campaign.errors import (
    CampaignDispatchPaused,
    CampaignRateLimitTimeout,
    ConcurrentSlotAcquisitionError,
    PhoneNumberPoolExhaustedError,
)
from api.services.campaign.traffic_split import campaign_split, pick_variant
from api.services.quota_service import authorize_workflow_run_start
from api.services.webhook_sync import calling as webhook_sync_calling
from api.services.workflow.initial_context import merge_external_initial_context
from api.services.workflow.run_creation import prepare_workflow_run_inputs
from api.utils.common import get_backend_endpoints

if TYPE_CHECKING:
    # Type-only — importing api.services.telephony eagerly triggers the
    # provider package init, which can pull in this module via the routes
    # chain and create a circular import. Runtime calls below lazy-import the
    # factory helpers inside methods instead.
    from api.services.telephony.base import TelephonyProvider


# Errors that may leave a dial request delivered to the provider without its
# answer reaching us.
_UNCERTAIN_DIAL_ERRORS = (
    asyncio.CancelledError,
    TimeoutError,
    ConnectionError,
    aiohttp.ClientError,
)


class CampaignCallDispatcher:
    """Manages rate-limited and concurrent-limited call dispatching.

    BotrixAI keeps, on top of upstream's parallel dispatcher:
    - a caller-ID pool with one slot per channel of each number, reserved for
      the call's lifetime, so a number never carries more calls than its
      carrier allows;
    - a "still running?" check before each dial, so a pause or stop takes
      effect at once;
    - provider account rejections pausing the campaign, and only telephony
      outages (not bad numbers) counting toward the failure rate;
    - Webhook Sync lead updates when a call starts or cannot start.
    """

    async def get_provider_for_campaign(self, campaign) -> "TelephonyProvider":
        """Resolve and pre-flight the provider used by a campaign.

        Legacy campaigns without a pinned configuration select the first active
        outbound-ready config, preferring an explicit default. The resolved id
        is pinned on this detached campaign instance for the rest of the batch.
        """
        from api.services.telephony.factory import get_telephony_provider_by_id
        from api.services.telephony.outbound_readiness import (
            resolve_outbound_configuration_id,
        )

        requested_id = campaign.telephony_configuration_id
        resolved_id = await resolve_outbound_configuration_id(
            requested_id,
            campaign.organization_id,
            db=db_client,
        )
        if requested_id is None:
            logger.warning(
                f"Campaign {campaign.id} has no telephony_configuration_id; "
                f"using ready config {resolved_id} for org "
                f"{campaign.organization_id}"
            )
            campaign.telephony_configuration_id = resolved_id
        return await get_telephony_provider_by_id(resolved_id, campaign.organization_id)

    async def get_channel_capacity_for_campaign(self, campaign) -> dict[str, int]:
        """Channels per active number of the campaign's telephony config (the
        org default for legacy campaigns without a pinned config)."""
        config_id = campaign.telephony_configuration_id
        if not config_id:
            default_cfg = await db_client.get_default_telephony_configuration(
                campaign.organization_id
            )
            config_id = default_cfg.id if default_cfg else None
        if not config_id:
            return {}
        return await db_client.get_active_channel_capacity_for_config(config_id)

    async def get_org_concurrent_limit(self, organization_id: int) -> int:
        """Get the concurrent call limit for an organization."""
        return await call_concurrency.get_org_concurrent_limit(organization_id)

    # Leave time for cleanup inside ARQ's 300-second job deadline.
    BATCH_TIMEOUT = 240
    SETUP_CONCURRENCY = 10
    CAPACITY_WAIT_TIMEOUT = 30

    async def process_batch(self, campaign_id: int, batch_size: int = 20) -> int:
        """Claim disjoint rows and set up calls with bounded parallelism."""
        campaign = await db_client.get_campaign_by_id(campaign_id)
        if not campaign:
            raise ValueError(f"Campaign {campaign_id} not found")
        if campaign.state != "running":
            return 0

        # Resolve legacy configurations once, and reject incomplete shared setup
        # before taking any claims or concurrency slots.
        provider = await self.get_provider_for_campaign(campaign)
        queued_runs = await db_client.claim_queued_runs_for_processing(
            campaign_id=campaign_id,
            scheduled_before=datetime.now(UTC),
            limit=batch_size,
        )
        if not queued_runs:
            return 0

        # One pool slot per channel of each caller ID on this campaign's config.
        try:
            if provider.from_numbers:
                await rate_limiter.initialize_from_number_pool(
                    campaign.organization_id,
                    provider.from_numbers,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                    channels=await self.get_channel_capacity_for_campaign(campaign),
                )
        except Exception as e:
            logger.warning(f"Failed to initialize from_number pool: {e}")

        processed_run_ids: set[int] = set()
        semaphore = asyncio.Semaphore(self.SETUP_CONCURRENCY)

        async def process_one(queued_run):
            async with semaphore:
                slot = None
                try:
                    # A pause or stop that landed after this batch was claimed
                    # leaves the contact queued for resume.
                    await self._ensure_campaign_running(
                        campaign_id, campaign.organization_id
                    )
                    slot = await self.acquire_concurrent_slot(
                        campaign.organization_id,
                        campaign,
                        timeout=self.CAPACITY_WAIT_TIMEOUT,
                    )
                    # Waiting for a slot can take a while, and a provider error on
                    # another call can pause the campaign meanwhile.
                    await self._ensure_campaign_running(
                        campaign_id, campaign.organization_id
                    )
                    dispatch_slot, slot = slot, None  # dispatch_call owns it now
                    run = await self.dispatch_call(queued_run, campaign, dispatch_slot)
                    # A provider accepted the call. Finish bookkeeping even if the
                    # batch is cancelled; it must never be returned for another dial.
                    await self._await_cleanup(
                        db_client.mark_campaign_run_dispatched(
                            queued_run.id,
                            run.id,
                            campaign.id,
                            campaign.organization_id,
                        )
                    )
                    processed_run_ids.add(queued_run.id)
                except CampaignDispatchPaused as exc:
                    if slot is not None:
                        await call_concurrency.release_slot(slot)
                    logger.info(
                        f"Campaign {campaign_id} paused before dialing queued run "
                        f"{queued_run.id} ({exc}); it stays queued"
                    )
                    return
                except (
                    ConcurrentSlotAcquisitionError,
                    CampaignRateLimitTimeout,
                    PhoneNumberPoolExhaustedError,
                ):
                    # Capacity contention is temporary, not a failed contact.
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        f"Error processing queued run {queued_run.id}: {exc}"
                    )
                    if not getattr(exc, "dispatch_uncertain", False):
                        # A Webhook Sync lead shows why it wasn't called (no
                        # quota, dialing error, agent or telephony config gone).
                        # Not for an uncertain dial: that call may be live.
                        await webhook_sync_calling.call_not_started(
                            queued_run, campaign.organization_id, str(exc)
                        )
                    await db_client.update_queued_run(
                        queued_run_id=queued_run.id,
                        state="failed",
                        processed_at=datetime.now(UTC),
                    )

        tasks = [asyncio.create_task(process_one(row)) for row in queued_runs]
        try:
            async with asyncio.timeout(self.BATCH_TIMEOUT):
                await asyncio.gather(*tasks)
        except TimeoutError:
            logger.info(
                f"Campaign {campaign_id} batch deadline reached; deferring remaining work"
            )
        finally:
            # No claim can be returned while a sibling could still originate it.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._return_unprocessed_claims(
                queued_runs,
                processed_run_ids,
                reason="batch_finished",
            )
        return len(processed_run_ids)

    @staticmethod
    async def _await_cleanup(operation):
        """Finish a short persistence/release operation before propagating cancellation."""
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _return_unprocessed_claims(
        self,
        queued_runs: list[QueuedRunModel],
        processed_run_ids: set[int],
        *,
        reason: str,
    ) -> None:
        queued_run_ids = [
            queued_run.id
            for queued_run in queued_runs
            if queued_run.id not in processed_run_ids
        ]
        if not queued_run_ids:
            return

        try:
            returned_count = (
                await db_client.return_processing_queued_runs_without_workflow(
                    queued_run_ids
                )
            )
            logger.info(
                f"Returned {returned_count}/{len(queued_run_ids)} claimed queued runs "
                f"back to queued state; reason={reason}; "
                f"queued_run_ids={queued_run_ids}"
            )
        except Exception as revert_error:
            logger.error(
                f"Failed to return claimed queued runs; reason={reason}; "
                f"queued_run_ids={queued_run_ids}; error={revert_error}"
            )

    async def dispatch_call(
        self,
        queued_run: QueuedRunModel,
        campaign,
        concurrency_slot: CallConcurrencySlot,
    ) -> WorkflowRunModel:
        """Own the acquired slot until a provider accepts (or may have accepted) a call."""
        workflow_run = None
        provider = None
        # The pool slot (a channel on the caller ID) this call occupies.
        from_slot = None
        attempted = False
        accepted = False
        try:
            phone_number = queued_run.context_variables.get("phone_number")
            if not phone_number:
                raise ValueError(f"No phone number in queued run {queued_run.id}")
            split = campaign_split(campaign)
            variant = pick_variant(split, phone_number)
            workflow_id = variant["workflow_id"]
            workflow = await db_client.get_workflow(
                workflow_id,
                organization_id=campaign.organization_id,
            )
            if not workflow:
                raise ValueError(f"Workflow {workflow_id} not found")

            provider = await self.get_provider_for_campaign(campaign)
            from_number = None
            # A provider with no caller IDs (e.g. an Asterisk trunk) dials
            # without one; there is no pool to reserve from.
            if provider.from_numbers:
                from_slot = await self.acquire_from_number(
                    campaign.organization_id,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                    timeout=self.CAPACITY_WAIT_TIMEOUT,
                )
                if from_slot is None:
                    raise PhoneNumberPoolExhaustedError(
                        organization_id=campaign.organization_id
                    )
                from_number = rate_limiter.pool_slot_address(from_slot)

            initial_context = {
                **merge_external_initial_context({}, queued_run.context_variables),
                "campaign_id": campaign.id,
                "provider": provider.PROVIDER_NAME,
                "source_uuid": queued_run.source_uuid,
                "caller_number": from_number,
                "called_number": phone_number,
                "direction": "outbound",
                "telephony_configuration_id": campaign.telephony_configuration_id,
            }
            run_inputs = await prepare_workflow_run_inputs(
                db_client, workflow, definition_id=variant["workflow_definition_id"]
            )
            workflow_run = await db_client.create_workflow_run(
                name=f"WR-CAMPAIGN-{campaign.id}-{queued_run.id}",
                workflow_id=workflow_id,
                mode=provider.PROVIDER_NAME,
                user_id=campaign.created_by,
                initial_context=initial_context,
                campaign_id=campaign.id,
                queued_run_id=queued_run.id,
                organization_id=campaign.organization_id,
                definition_id=run_inputs.definition_id,
                campaign_traffic_split={
                    "variant_id": variant["id"],
                    "revision": split["revision"],
                    "workflow_definition_id": variant["workflow_definition_id"],
                    "weight": variant["weight"],
                },
            )
            await call_concurrency.bind_workflow_run(concurrency_slot, workflow_run.id)
            if from_slot is not None:
                # Released through this mapping when the call ends.
                await rate_limiter.store_workflow_from_number_mapping(
                    workflow_run.id,
                    campaign.organization_id,
                    from_slot,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                )
            await webhook_sync_calling.call_started(
                queued_run, workflow_run, campaign.organization_id
            )
            if queued_run.context_variables.get("is_retry"):
                reason = queued_run.context_variables.get("retry_reason", "unknown")
                await db_client.update_workflow_run(
                    run_id=workflow_run.id,
                    gathered_context={"call_tags": ["retry", f"retry_reason_{reason}"]},
                )
            quota = await authorize_workflow_run_start(
                workflow_id=workflow_id,
                organization_id=campaign.organization_id,
                workflow_run_id=workflow_run.id,
                campaign_id=campaign.id,
            )
            if not quota.has_quota:
                raise ValueError(quota.error_message or "Quota exceeded")

            backend_endpoint, _ = await get_backend_endpoints()
            webhook_url = (
                f"{backend_endpoint}/api/v1/telephony/{provider.WEBHOOK_ENDPOINT}"
                f"?workflow_id={workflow_id}"
                f"&workflow_run_id={workflow_run.id}"
                f"&organization_id={campaign.organization_id}"
            )
            await self.apply_rate_limit(
                campaign.organization_id,
                campaign.rate_limit_per_second,
                scope_key=f"campaign:{campaign.id}",
            )
            # No long wait or DB operation between rate admission and dialing.
            attempted = True
            call_result = await provider.initiate_call(
                to_number=phone_number,
                webhook_url=webhook_url,
                workflow_run_id=workflow_run.id,
                from_number=from_number,
                workflow_id=workflow_id,
                organization_id=campaign.organization_id,
            )
            accepted = True
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                gathered_context={
                    "provider": provider.PROVIDER_NAME,
                    **(call_result.provider_metadata or {}),
                },
            )
            return workflow_run
        except (Exception, asyncio.CancelledError) as exc:
            if accepted:
                # Bookkeeping failure cannot free capacity occupied by a real call.
                logger.exception(
                    f"Call accepted for run {workflow_run.id}; persistence interrupted"
                )
                if isinstance(exc, asyncio.CancelledError):
                    await self._await_cleanup(
                        db_client.mark_campaign_run_dispatched(
                            queued_run.id,
                            workflow_run.id,
                            campaign.id,
                            campaign.organization_id,
                        )
                    )
                    raise
                return workflow_run

            uncertain = attempted and isinstance(exc, _UNCERTAIN_DIAL_ERRORS)
            if workflow_run is None:
                # Nothing was created: hand back the raw capacity directly.
                await self._await_cleanup(
                    self._release_unbound(campaign, concurrency_slot, from_slot)
                )
            else:
                await self._await_cleanup(
                    self._finish_interrupted_dispatch(
                        queued_run,
                        campaign,
                        workflow_run,
                        concurrency_slot,
                        exc,
                        uncertain=uncertain,
                        attempted=attempted,
                        provider_name=provider.PROVIDER_NAME if provider else None,
                        from_slot=from_slot,
                    )
                )
            if uncertain and isinstance(exc, Exception):
                exc.dispatch_uncertain = True
            raise

    async def _release_unbound(self, campaign, slot, from_slot) -> None:
        """Release capacity taken before a workflow run existed."""
        try:
            if from_slot is not None:
                await rate_limiter.release_from_number(
                    campaign.organization_id,
                    from_slot,
                    telephony_configuration_id=campaign.telephony_configuration_id,
                )
        finally:
            await call_concurrency.release_slot(slot)

    async def _finish_interrupted_dispatch(
        self,
        queued_run,
        campaign,
        workflow_run,
        slot,
        error,
        *,
        uncertain: bool,
        attempted: bool,
        provider_name: str | None = None,
        from_slot: str | None = None,
    ) -> None:
        message = str(error) or "Call setup cancelled"
        if uncertain:
            # A request may have reached the provider. Never requeue it or free
            # a possibly live slot (or its caller-ID channel); terminal callbacks
            # and stale recovery own release. Save recovery first in case
            # updating the queue fails. The slot identity must survive expiration
            # of its Redis mapping.
            uncertain_at = datetime.now(UTC)
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                gathered_context={"error": message, "call_initiation_uncertain": True},
                logs={
                    "campaign_dispatch": {
                        "outcome": "uncertain",
                        "uncertain_at": uncertain_at.isoformat(),
                        "slot_id": slot.slot_id,
                        "scope_key": slot.scope_key,
                    }
                },
            )
            await db_client.update_queued_run(
                queued_run_id=queued_run.id,
                state="failed",
                processed_at=uncertain_at,
            )
            logger.warning(
                f"Uncertain call initiation for run {workflow_run.id}; slot retained"
            )
            if isinstance(error, Exception) and telephony_outage(error):
                # A dial that timed out or lost its connection is how an outage
                # usually looks, and stale recovery records no outcome, so
                # count it here or such an outage would never pause the
                # campaign. A cancelled batch is not an outage.
                await circuit_breaker.record_and_evaluate(
                    campaign.id,
                    is_failure=True,
                    workflow_run_id=workflow_run.id,
                    reason="telephony_unavailable",
                )
            return

        try:
            await db_client.update_workflow_run(
                run_id=workflow_run.id,
                is_completed=True,
                state=WorkflowRunState.COMPLETED.value,
                gathered_context={"error": message},
                logs={
                    "campaign_dispatch": {
                        "outcome": "failed" if attempted else "not_started",
                    },
                    "telephony_status_callbacks": [
                        {
                            "status": "failed",
                            "timestamp": datetime.now(UTC).isoformat(),
                            "data": {"error": message},
                        }
                    ],
                },
            )
            if isinstance(error, (asyncio.CancelledError, CampaignRateLimitTimeout)):
                # This task owns the row and has not contacted the provider.
                await db_client.update_queued_run(
                    queued_run_id=queued_run.id,
                    state="queued",
                )
            if attempted and isinstance(error, Exception):
                # The telephony provider refusing our credentials or account
                # (HTTP 401/402/403) pauses the campaign at once. Otherwise a
                # rejected dial counts toward the failure rate only when the
                # provider itself is failing (5xx, timeout, connection error):
                # a bad number in the list (4xx) must not pause a healthy
                # campaign, but an outage must.
                await circuit_breaker.pause_on_provider_error(
                    organization_id=campaign.organization_id,
                    workflow_run_id=workflow_run.id,
                    provider=provider_name or "telephony",
                    service="telephony",
                    exception=error,
                )
                if telephony_outage(error):
                    await circuit_breaker.record_and_evaluate(
                        campaign.id,
                        is_failure=True,
                        workflow_run_id=workflow_run.id,
                        reason="telephony_unavailable",
                    )
        finally:
            # Also release the raw slots if setup failed before mapping them.
            try:
                mapped = await rate_limiter.get_workflow_from_number_mapping(
                    workflow_run.id
                )
                await self.release_call_slot(workflow_run.id)
                if from_slot is not None and not mapped:
                    # Only when unmapped: once released through the mapping,
                    # the channel may already belong to another call.
                    await rate_limiter.release_from_number(
                        campaign.organization_id,
                        from_slot,
                        telephony_configuration_id=campaign.telephony_configuration_id,
                    )
            finally:
                await call_concurrency.release_slot(slot)

    async def recover_stale_dispatches(self) -> None:
        """Settle uncertain calls without callbacks and retry durable slot cleanup."""
        pending = await db_client.recover_stale_campaign_dispatches(
            stale_before=datetime.now(UTC)
            - timedelta(seconds=rate_limiter.stale_call_timeout)
        )
        for recovery in pending:
            try:
                await rate_limiter.reconcile_workflow_slot_mapping(
                    recovery["workflow_run_id"],
                    organization_id=recovery["organization_id"],
                    slot_id=recovery["slot_id"],
                    scope_key=recovery["scope_key"],
                )
                await db_client.mark_campaign_dispatch_slot_reconciled(
                    recovery["workflow_run_id"], recovery["organization_id"]
                )
            except Exception:
                # This persisted cleanup record is scanned even after its
                # campaign completes, pauses or stops.
                logger.exception(
                    f"Stale dispatch slot cleanup failed for run "
                    f"{recovery['workflow_run_id']}; will retry"
                )

    @staticmethod
    async def _ensure_campaign_running(campaign_id: int, organization_id: int) -> None:
        """Raise CampaignDispatchPaused unless the campaign is still running."""
        try:
            current = await db_client.get_campaign(campaign_id, organization_id)
        except Exception as exc:
            logger.error(
                f"Cannot verify campaign {campaign_id} state before dialing "
                f"({type(exc).__name__})"
            )
            raise CampaignDispatchPaused(
                "Cannot verify campaign state before dialing"
            ) from exc
        if current is None or current.state != "running":
            raise CampaignDispatchPaused("Campaign is no longer running")

    async def apply_rate_limit(
        self,
        organization_id: int,
        rate_limit: int,
        *,
        scope_key: str,
    ) -> None:
        """Wait on the same campaign bucket used to admit actual dial attempts."""
        deadline = time.monotonic() + self.CAPACITY_WAIT_TIMEOUT
        while True:
            if await rate_limiter.acquire_token(
                organization_id,
                rate_limit,
                scope_key=scope_key,
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CampaignRateLimitTimeout(
                    "Campaign dial rate unavailable; retry later"
                )
            wait_time = await rate_limiter.get_next_available_slot(
                organization_id,
                rate_limit,
                scope_key=scope_key,
            )
            await asyncio.sleep(min(remaining, max(0.01, wait_time)))

    async def acquire_concurrent_slot(
        self, organization_id: int, campaign: any, timeout: float = 30
    ) -> CallConcurrencySlot:
        """
        Acquires a concurrent call slot - waits if necessary until a slot is available.

        Args:
            organization_id: The organization ID
            campaign: The campaign object
            timeout: Maximum time to wait for a slot (default 30 seconds)

        Returns the slot which must be released when the call completes.

        Raises:
            ConcurrentSlotAcquisitionError: If slot cannot be acquired within timeout
        """
        # Check for campaign-level max_concurrency in orchestrator_metadata.
        # It caps this campaign's own concurrent calls via a campaign-scoped
        # counter — the org-wide limit still applies on top, but calls from
        # other sources (WebRTC, inbound, other campaigns) don't count
        # against the campaign's cap.
        campaign_max_concurrency = None
        if campaign.orchestrator_metadata:
            campaign_max_concurrency = campaign.orchestrator_metadata.get(
                "max_concurrency"
            )

        try:
            return await call_concurrency.acquire_org_slot(
                organization_id,
                source=f"campaign:{campaign.id}",
                timeout=timeout,
                scope_key=(
                    f"campaign:{campaign.id}"
                    if campaign_max_concurrency is not None
                    else None
                ),
                scope_max_concurrent=campaign_max_concurrency,
                retry_interval=1,
            )
        except CallConcurrencyLimitError as e:
            raise ConcurrentSlotAcquisitionError(
                organization_id=organization_id,
                campaign_id=campaign.id,
                wait_time=e.wait_time,
            ) from e

    async def acquire_from_number(
        self,
        organization_id: int,
        telephony_configuration_id: int | None,
        timeout: float = 30,
    ) -> str | None:
        """Reserve a caller-ID channel from the (org, telephony config) pool.

        Polls every second for up to ``timeout`` seconds and returns the pool
        member (see ``rate_limiter.pool_slot_address`` for its number), or None
        when every channel stayed busy.
        """
        wait_start = time.time()

        while True:
            from_slot = await rate_limiter.acquire_from_number(
                organization_id, telephony_configuration_id
            )
            if from_slot:
                return from_slot

            wait_time = time.time() - wait_start
            if wait_time > timeout:
                logger.warning(
                    f"From number pool exhausted for org {organization_id} "
                    f"config {telephony_configuration_id} after waiting "
                    f"{wait_time:.1f}s"
                )
                return None
            await asyncio.sleep(1)

    async def release_call_slot(self, workflow_run_id: int) -> bool:
        """Release the call's concurrency slot and caller-ID channel.

        Called by provider status callbacks and workflow completion.
        """
        slot_released = await call_concurrency.release_workflow_run_slot(
            workflow_run_id
        )

        from_number_mapping = await rate_limiter.get_workflow_from_number_mapping(
            workflow_run_id
        )
        if from_number_mapping:
            fn_org_id, fn_number, fn_tcid = from_number_mapping
            fn_success = await rate_limiter.release_from_number(
                fn_org_id, fn_number, telephony_configuration_id=fn_tcid
            )
            if fn_success:
                await rate_limiter.delete_workflow_from_number_mapping(workflow_run_id)
                logger.info(
                    f"Released from_number {fn_number} for workflow run {workflow_run_id}"
                )

        return slot_released


# Global instance
campaign_call_dispatcher = CampaignCallDispatcher()
