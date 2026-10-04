"""Database access for Webhook Sync endpoints, leads and request logs.

Every read and write of an endpoint, lead or log is scoped by
``organization_id`` except the receiver's endpoint lookup, which resolves the
organization *from* the endpoint's unguessable UUID.
"""

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Integer, cast, delete, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from api.db.base_client import BaseDBClient
from api.db.webhook_sync_models import (
    WebhookEndpointAuditLogModel,
    WebhookEndpointModel,
    WebhookLeadModel,
    WebhookRequestLogModel,
)
from api.db.models import UserModel, WorkflowModel

# Lead statuses that mean "this lead was not really received as a new one".
NON_ORIGINAL_STATUSES = ("invalid_number", "duplicate")
# A lead nothing has been dialled for yet.
WAITING_STATUSES = ("received", "on_hold", "queued", "scheduled")


def _advisory_key(endpoint_id: int, phone: str) -> int:
    """A signed 64-bit Postgres advisory-lock key for (endpoint, phone)."""
    digest = hashlib.blake2b(f"{endpoint_id}:{phone}".encode(), digest_size=8)
    return int.from_bytes(digest.digest(), "big", signed=True)


def _start_of_today_utc() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class WebhookSyncClient(BaseDBClient):
    # ======== ENDPOINTS ========

    async def create_webhook_endpoint(
        self,
        *,
        organization_id: int,
        name: str,
        workflow_id: int,
        auth_type: str,
        secret: str,
        is_active: bool,
        auto_call: bool,
        field_mapping: Dict[str, Any],
        call_settings: Dict[str, Any],
        rate_limit_per_minute: int,
        created_by: Optional[int],
    ) -> WebhookEndpointModel:
        async with self.async_session() as session:
            endpoint = WebhookEndpointModel(
                organization_id=organization_id,
                name=name,
                endpoint_uuid=str(uuid.uuid4()),
                auth_type=auth_type,
                secret=secret,
                workflow_id=workflow_id,
                is_active=is_active,
                auto_call=auto_call,
                field_mapping=field_mapping,
                call_settings=call_settings,
                rate_limit_per_minute=rate_limit_per_minute,
                created_by=created_by,
            )
            session.add(endpoint)
            await session.commit()
            await session.refresh(endpoint)
            return endpoint

    async def get_webhook_endpoint_by_uuid(
        self, endpoint_uuid: str
    ) -> Optional[WebhookEndpointModel]:
        """Receiver lookup; not org-scoped (the UUID identifies the org)."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookEndpointModel).where(
                    WebhookEndpointModel.endpoint_uuid == endpoint_uuid
                )
            )
            return result.scalar_one_or_none()

    async def get_webhook_endpoint(
        self, endpoint_id: int, organization_id: int
    ) -> Optional[WebhookEndpointModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookEndpointModel).where(
                    WebhookEndpointModel.id == endpoint_id,
                    WebhookEndpointModel.organization_id == organization_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_webhook_endpoints(
        self, organization_id: int, *, today: Optional[datetime] = None
    ) -> List[Tuple[WebhookEndpointModel, Optional[str], int, int]]:
        """Endpoints with (workflow name, leads since ``today``, leads total)."""
        today = today or _start_of_today_utc()
        counts = (
            select(
                WebhookLeadModel.endpoint_id.label("endpoint_id"),
                func.count().label("total"),
                func.count()
                .filter(WebhookLeadModel.received_at >= today)
                .label("today"),
            )
            .where(WebhookLeadModel.organization_id == organization_id)
            .group_by(WebhookLeadModel.endpoint_id)
            .subquery()
        )
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    WebhookEndpointModel,
                    WorkflowModel.name,
                    func.coalesce(counts.c.today, 0),
                    func.coalesce(counts.c.total, 0),
                )
                .join(
                    WorkflowModel,
                    WorkflowModel.id == WebhookEndpointModel.workflow_id,
                    isouter=True,
                )
                .join(
                    counts,
                    counts.c.endpoint_id == WebhookEndpointModel.id,
                    isouter=True,
                )
                .where(WebhookEndpointModel.organization_id == organization_id)
                .order_by(WebhookEndpointModel.created_at.desc())
            )
            return [
                (e, name, today_n, total_n)
                for e, name, today_n, total_n in result.all()
            ]

    async def count_webhook_leads_for_endpoint(
        self,
        endpoint_id: int,
        organization_id: int,
        *,
        today: Optional[datetime] = None,
    ) -> Tuple[int, int]:
        """(leads since ``today``, leads total) for one endpoint."""
        today = today or _start_of_today_utc()
        async with self.async_session() as session:
            result = await session.execute(
                select(
                    func.count().filter(WebhookLeadModel.received_at >= today),
                    func.count(),
                ).where(
                    WebhookLeadModel.endpoint_id == endpoint_id,
                    WebhookLeadModel.organization_id == organization_id,
                )
            )
            today_n, total_n = result.one()
            return int(today_n or 0), int(total_n or 0)

    async def update_webhook_endpoint(
        self, endpoint_id: int, organization_id: int, **fields: Any
    ) -> Optional[WebhookEndpointModel]:
        allowed = {
            "name",
            "workflow_id",
            "auth_type",
            "secret",
            "is_active",
            "auto_call",
            "field_mapping",
            "call_settings",
            "rate_limit_per_minute",
            "campaign_id",
        }
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookEndpointModel).where(
                    WebhookEndpointModel.id == endpoint_id,
                    WebhookEndpointModel.organization_id == organization_id,
                )
            )
            endpoint = result.scalar_one_or_none()
            if endpoint is None:
                return None
            for key, value in fields.items():
                if key not in allowed:
                    raise ValueError(f"Cannot update field: {key}")
                setattr(endpoint, key, value)
            await session.commit()
            await session.refresh(endpoint)
            return endpoint

    async def delete_webhook_endpoint(
        self, endpoint_id: int, organization_id: int
    ) -> bool:
        """Delete an endpoint with its leads and logs."""
        async with self.async_session() as session:
            result = await session.execute(
                delete(WebhookEndpointModel).where(
                    WebhookEndpointModel.id == endpoint_id,
                    WebhookEndpointModel.organization_id == organization_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    # ======== LEADS ========

    async def insert_webhook_lead(
        self,
        *,
        endpoint: WebhookEndpointModel,
        phone: Optional[str],
        dedupe_window_hours: int,
        status: str,
        status_reason: Optional[str],
        idempotency_key: Optional[str],
        external_lead_id: Optional[str],
        reenquiry_days: int = 30,
        **fields: Any,
    ) -> Tuple[WebhookLeadModel, bool]:
        """Store a lead; returns (lead, replayed).

        ``replayed`` is True when the request repeats one we already have and
        nothing new is stored: the same Idempotency-Key, or the same CRM lead
        id within ``reenquiry_days`` with no new number. The same CRM lead id
        *is* stored again when it brings a different valid number (the number
        was corrected; a still-waiting earlier lead is then replaced) or when
        it comes back after the re-enquiry window (the customer asked again).

        A number someone opted out anywhere in the organization is stored as
        ``do_not_call`` and never called. Otherwise a lead whose phone already
        arrived on this endpoint within the dedupe window is stored as
        ``duplicate``. Per-(endpoint, phone) and per-(endpoint, CRM id)
        advisory locks keep simultaneous requests from both passing as new.
        """

        async def _find_by_key(session) -> Optional[WebhookLeadModel]:
            if not idempotency_key:
                return None
            result = await session.execute(
                select(WebhookLeadModel)
                .where(
                    WebhookLeadModel.endpoint_id == endpoint.id,
                    WebhookLeadModel.idempotency_key == idempotency_key,
                )
                .limit(1)
            )
            return result.scalar_one_or_none()

        try:
            async with self.async_session() as session:
                async with session.begin():
                    lead, replayed = await self._insert_or_replay(
                        session,
                        _find_by_key,
                        endpoint=endpoint,
                        phone=phone,
                        dedupe_window_hours=dedupe_window_hours,
                        reenquiry_days=reenquiry_days,
                        status=status,
                        status_reason=status_reason,
                        idempotency_key=idempotency_key,
                        external_lead_id=external_lead_id,
                        fields=fields,
                    )
                # The commit expired the row; reload it for the caller.
                await session.refresh(lead)
                return lead, replayed
        except IntegrityError:
            # A concurrent retry with the same key committed first.
            async with self.async_session() as session:
                existing = await _find_by_key(session)
                if existing is None:
                    raise
                return existing, True

    async def _insert_or_replay(
        self,
        session,
        find_by_key,
        *,
        endpoint: WebhookEndpointModel,
        phone: Optional[str],
        dedupe_window_hours: int,
        reenquiry_days: int,
        status: str,
        status_reason: Optional[str],
        idempotency_key: Optional[str],
        external_lead_id: Optional[str],
        fields: Dict[str, Any],
    ) -> Tuple[WebhookLeadModel, bool]:
        """Inside one transaction: the replayed lead, or a newly stored one."""
        from api.db.models import QueuedRunModel

        now = datetime.now(UTC)
        existing = await find_by_key(session)
        if existing is not None:
            return existing, True

        replaced: Optional[WebhookLeadModel] = None
        if external_lead_id:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_key(endpoint.id, f"crm:{external_lead_id}")},
            )
            prior = (
                await session.execute(
                    select(WebhookLeadModel)
                    .where(
                        WebhookLeadModel.endpoint_id == endpoint.id,
                        WebhookLeadModel.external_lead_id == external_lead_id,
                    )
                    .order_by(WebhookLeadModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if prior is not None:
                new_number = bool(phone) and phone != prior.phone
                recent = prior.received_at >= now - timedelta(days=reenquiry_days)
                if recent and not new_number:
                    return prior, True
                if new_number and prior.status in WAITING_STATUSES:
                    replaced = prior

        duplicate_of = None
        if phone and status in ("received", "on_hold"):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_key(endpoint.id, phone)},
            )
            opted_out = (
                await session.execute(
                    select(WebhookLeadModel.id)
                    .where(
                        WebhookLeadModel.organization_id == endpoint.organization_id,
                        WebhookLeadModel.phone == phone,
                        WebhookLeadModel.status == "do_not_call",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if opted_out is not None:
                status = "do_not_call"
                status_reason = f"This number opted out of calls (lead #{opted_out})"
            elif dedupe_window_hours > 0:
                since = now - timedelta(hours=dedupe_window_hours)
                result = await session.execute(
                    select(WebhookLeadModel.id)
                    .where(
                        WebhookLeadModel.endpoint_id == endpoint.id,
                        WebhookLeadModel.phone == phone,
                        WebhookLeadModel.received_at >= since,
                        WebhookLeadModel.status.notin_(NON_ORIGINAL_STATUSES),
                        *(
                            [WebhookLeadModel.id != replaced.id]
                            if replaced is not None
                            else []
                        ),
                    )
                    .order_by(WebhookLeadModel.received_at)
                    .limit(1)
                )
                duplicate_of = result.scalar_one_or_none()
                if duplicate_of is not None:
                    status = "duplicate"
                    status_reason = (
                        f"Same number received within {dedupe_window_hours}h"
                    )

        lead = WebhookLeadModel(
            organization_id=endpoint.organization_id,
            endpoint_id=endpoint.id,
            phone=phone,
            status=status,
            status_reason=status_reason,
            idempotency_key=idempotency_key,
            external_lead_id=external_lead_id,
            duplicate_of_lead_id=duplicate_of,
            **fields,
        )
        session.add(lead)
        await session.flush()

        if replaced is not None:
            # The CRM corrected the number: the earlier lead must not call the
            # old one. Its queued call comes off the queue in this transaction.
            if replaced.queued_run_id:
                await session.execute(
                    update(QueuedRunModel)
                    .where(
                        QueuedRunModel.id == replaced.queued_run_id,
                        QueuedRunModel.state == "queued",
                    )
                    .values(state="failed", processed_at=now)
                )
            replaced.status = "failed"
            replaced.status_reason = (
                f"Replaced by lead #{lead.id} with a corrected number"
            )
            replaced.next_retry_at = None
        return lead, False

    async def get_webhook_lead(
        self, lead_id: int, organization_id: int
    ) -> Optional[WebhookLeadModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel).where(
                    WebhookLeadModel.id == lead_id,
                    WebhookLeadModel.organization_id == organization_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_webhook_leads(
        self,
        organization_id: int,
        *,
        endpoint_id: Optional[int] = None,
        statuses: Optional[List[str]] = None,
        search: Optional[str] = None,
        received_from: Optional[datetime] = None,
        received_to: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[WebhookLeadModel], int]:
        filters = [WebhookLeadModel.organization_id == organization_id]
        if endpoint_id is not None:
            filters.append(WebhookLeadModel.endpoint_id == endpoint_id)
        if statuses:
            filters.append(WebhookLeadModel.status.in_(statuses))
        if received_from is not None:
            filters.append(WebhookLeadModel.received_at >= received_from)
        if received_to is not None:
            filters.append(WebhookLeadModel.received_at < received_to)
        if search:
            pattern = f"%{search.strip()}%"
            filters.append(
                or_(
                    WebhookLeadModel.name.ilike(pattern),
                    WebhookLeadModel.phone.ilike(pattern),
                    WebhookLeadModel.phone_raw.ilike(pattern),
                    WebhookLeadModel.email.ilike(pattern),
                )
            )
        async with self.async_session() as session:
            total = (
                await session.execute(select(func.count()).where(*filters))
            ).scalar_one()
            result = await session.execute(
                select(WebhookLeadModel)
                .where(*filters)
                .order_by(
                    WebhookLeadModel.received_at.desc(), WebhookLeadModel.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), int(total)

    @asynccontextmanager
    async def webhook_endpoint_campaign_lock(self, endpoint_id: int):
        """Serialize creating an endpoint's campaign: simultaneous first leads
        would otherwise each create one. A session-level advisory lock, held
        on its own connection while the caller checks and creates."""
        key = _advisory_key(endpoint_id, "campaign")
        async with self.async_session() as session:
            await session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                yield
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )

    async def get_webhook_leads_by_ids(
        self, lead_ids: List[int], organization_id: int
    ) -> List[WebhookLeadModel]:
        if not lead_ids:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel)
                .where(
                    WebhookLeadModel.id.in_(lead_ids),
                    WebhookLeadModel.organization_id == organization_id,
                )
                .order_by(WebhookLeadModel.id)
            )
            return list(result.scalars().all())

    async def queue_webhook_leads(
        self,
        *,
        organization_id: int,
        campaign_id: int,
        items: List[Dict[str, Any]],
    ) -> int:
        """Queue leads for calling in one transaction: one queued run per lead
        and the lead marked queued/scheduled. ``items`` are dicts with
        ``lead_id``, ``context_variables``, ``scheduled_for``, ``status``,
        ``status_reason`` and ``next_retry_at``. A lead that is no longer
        ``received`` (queued meanwhile, e.g. by the sweep) is skipped."""
        from api.db.models import QueuedRunModel

        if not items:
            return 0
        async with self.async_session() as session:
            async with session.begin():
                claimed = (
                    (
                        await session.execute(
                            select(WebhookLeadModel.id)
                            .where(
                                WebhookLeadModel.id.in_([i["lead_id"] for i in items]),
                                WebhookLeadModel.organization_id == organization_id,
                                WebhookLeadModel.status == "received",
                                WebhookLeadModel.queued_run_id.is_(None),
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                todo = [i for i in items if i["lead_id"] in set(claimed)]
                if not todo:
                    return 0
                run_ids = (
                    (
                        await session.execute(
                            insert(QueuedRunModel).returning(
                                QueuedRunModel.id, sort_by_parameter_order=True
                            ),
                            [
                                {
                                    "campaign_id": campaign_id,
                                    "source_uuid": f"lead-{i['lead_id']}",
                                    "context_variables": i["context_variables"],
                                    "state": "queued",
                                    "retry_count": 0,
                                    "scheduled_for": i["scheduled_for"],
                                }
                                for i in todo
                            ],
                        )
                    )
                    .scalars()
                    .all()
                )
                # One executemany UPDATE by primary key for all the leads.
                await session.execute(
                    update(WebhookLeadModel),
                    [
                        {
                            "id": item["lead_id"],
                            "status": item["status"],
                            "status_reason": item["status_reason"],
                            "queued_run_id": run_id,
                            "next_retry_at": item["next_retry_at"],
                        }
                        for item, run_id in zip(todo, run_ids)
                    ],
                )
            return len(todo)

    async def update_webhook_lead(
        self,
        lead_id: int,
        organization_id: int,
        *,
        increment_attempts: bool = False,
        unless_do_not_call: bool = False,
        **fields: Any,
    ) -> Optional[WebhookLeadModel]:
        """Update a lead's call state (status, run, retry time, ...).

        ``unless_do_not_call`` leaves a lead someone stopped calling as it is:
        the engine's events for a call already under way must not undo it."""
        allowed = {
            "status",
            "status_reason",
            "queued_run_id",
            "last_workflow_run_id",
            "last_call_status",
            "disposition",
            "next_retry_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update lead fields: {sorted(unknown)}")
        values = dict(fields)
        if increment_attempts:
            values["call_attempts"] = WebhookLeadModel.call_attempts + 1
        async with self.async_session() as session:
            result = await session.execute(
                update(WebhookLeadModel)
                .where(
                    WebhookLeadModel.id == lead_id,
                    WebhookLeadModel.organization_id == organization_id,
                    *(
                        [WebhookLeadModel.status != "do_not_call"]
                        if unless_do_not_call
                        else []
                    ),
                )
                .values(**values)
                .returning(WebhookLeadModel)
            )
            lead = result.scalar_one_or_none()
            await session.commit()
            # The commit expired it; load it again so callers can read it.
            if lead is not None:
                await session.refresh(lead)
            return lead

    async def cancel_queued_runs(
        self, campaign_id: int, queued_run_ids: List[int]
    ) -> int:
        """Take not-yet-dispatched runs off the queue (a stopped lead)."""
        from api.db.models import QueuedRunModel

        ids = [i for i in queued_run_ids if i]
        if not ids:
            return 0
        async with self.async_session() as session:
            result = await session.execute(
                update(QueuedRunModel)
                .where(
                    QueuedRunModel.id.in_(ids),
                    QueuedRunModel.campaign_id == campaign_id,
                    QueuedRunModel.state == "queued",
                )
                .values(state="failed", processed_at=datetime.now(UTC))
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def opt_out_webhook_phone(
        self, organization_id: int, phone: str, reason: str
    ) -> int:
        """Mark every not-yet-called lead with this number in the organization
        "do not call" and take their queued calls off the queue, in one
        transaction. Future leads with the number are stored as do_not_call
        by insert_webhook_lead. Returns how many leads were stopped."""
        from api.db.models import QueuedRunModel

        if not phone:
            return 0
        async with self.async_session() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(WebhookLeadModel.id, WebhookLeadModel.queued_run_id)
                        .where(
                            WebhookLeadModel.organization_id == organization_id,
                            WebhookLeadModel.phone == phone,
                            WebhookLeadModel.status.in_(WAITING_STATUSES),
                        )
                        .with_for_update()
                    )
                ).all()
                if not rows:
                    return 0
                run_ids = [run_id for _, run_id in rows if run_id]
                if run_ids:
                    await session.execute(
                        update(QueuedRunModel)
                        .where(
                            QueuedRunModel.id.in_(run_ids),
                            QueuedRunModel.state == "queued",
                        )
                        .values(state="failed", processed_at=datetime.now(UTC))
                    )
                await session.execute(
                    update(WebhookLeadModel)
                    .where(WebhookLeadModel.id.in_([lead_id for lead_id, _ in rows]))
                    .values(
                        status="do_not_call", status_reason=reason, next_retry_at=None
                    )
                )
            return len(rows)

    async def unqueue_waiting_webhook_leads(
        self, endpoint_id: int, organization_id: int, reason: str
    ) -> int:
        """Take an endpoint's waiting calls off the queue (auto-call turned
        off). A lead never called goes back to ``received``; one waiting for a
        retry keeps its last outcome (no_answer, busy). Calls already being
        dialled are not touched. Returns how many leads were unqueued."""
        from api.db.models import QueuedRunModel

        async with self.async_session() as session:
            async with session.begin():
                leads = (
                    (
                        await session.execute(
                            select(WebhookLeadModel)
                            .where(
                                WebhookLeadModel.endpoint_id == endpoint_id,
                                WebhookLeadModel.organization_id == organization_id,
                                WebhookLeadModel.status.in_(("queued", "scheduled")),
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                if not leads:
                    return 0
                run_ids = [lead.queued_run_id for lead in leads if lead.queued_run_id]
                cancelled: set[int] = set()
                if run_ids:
                    cancelled = set(
                        (
                            await session.execute(
                                update(QueuedRunModel)
                                .where(
                                    QueuedRunModel.id.in_(run_ids),
                                    QueuedRunModel.state == "queued",
                                )
                                .values(state="failed", processed_at=datetime.now(UTC))
                                .returning(QueuedRunModel.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                count = 0
                for lead in leads:
                    if lead.queued_run_id and lead.queued_run_id not in cancelled:
                        continue  # already being dialled
                    lead.status = (
                        (lead.last_call_status or "failed")
                        if lead.call_attempts
                        else "received"
                    )
                    lead.status_reason = reason
                    lead.queued_run_id = None
                    lead.next_retry_at = None
                    count += 1
            return count

    async def list_webhook_lead_ids_on_hold(
        self, endpoint_id: int, organization_id: int, limit: int = 5000
    ) -> List[int]:
        """Leads an endpoint stored while paused and nobody has decided on
        yet (call or skip), oldest first."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel.id)
                .where(
                    WebhookLeadModel.endpoint_id == endpoint_id,
                    WebhookLeadModel.organization_id == organization_id,
                    WebhookLeadModel.status == "on_hold",
                )
                .order_by(WebhookLeadModel.received_at)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def count_webhook_leads_on_hold(
        self, endpoint_id: int, organization_id: int
    ) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                select(func.count()).where(
                    WebhookLeadModel.endpoint_id == endpoint_id,
                    WebhookLeadModel.organization_id == organization_id,
                    WebhookLeadModel.status == "on_hold",
                )
            )
            return int(result.scalar_one() or 0)

    async def reset_webhook_leads_for_call(
        self, lead_ids: List[int], organization_id: int, *, from_statuses
    ) -> List[int]:
        """Make leads callable again (``received``, off any old queued run)
        when they are still in one of ``from_statuses``; returns the ids
        reset. The status check is in the UPDATE, so a lead that changed in
        the meantime (now calling, stopped) is left alone."""
        if not lead_ids:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                update(WebhookLeadModel)
                .where(
                    WebhookLeadModel.id.in_(lead_ids),
                    WebhookLeadModel.organization_id == organization_id,
                    WebhookLeadModel.status.in_(tuple(from_statuses)),
                )
                .values(status="received", queued_run_id=None, next_retry_at=None)
                .returning(WebhookLeadModel.id)
            )
            ids = list(result.scalars().all())
            await session.commit()
            return ids

    async def skip_webhook_leads(
        self, lead_ids: List[int], organization_id: int, *, reason: str
    ) -> List[int]:
        """Mark leads on hold (or stored but never queued) as ``skipped``:
        they won't be called, but the number isn't blocked. Returns the ids
        skipped."""
        if not lead_ids:
            return []
        async with self.async_session() as session:
            result = await session.execute(
                update(WebhookLeadModel)
                .where(
                    WebhookLeadModel.id.in_(lead_ids),
                    WebhookLeadModel.organization_id == organization_id,
                    or_(
                        WebhookLeadModel.status == "on_hold",
                        (WebhookLeadModel.status == "received")
                        & WebhookLeadModel.queued_run_id.is_(None),
                    ),
                )
                .values(status="skipped", status_reason=reason, next_retry_at=None)
                .returning(WebhookLeadModel.id)
            )
            ids = list(result.scalars().all())
            await session.commit()
            return ids

    async def opted_out_webhook_phones(
        self, organization_id: int, phones: List[str]
    ) -> set:
        """Which of ``phones`` opted out of calls in the organization."""
        wanted = sorted({p for p in phones if p})
        if not wanted:
            return set()
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel.phone)
                .where(
                    WebhookLeadModel.organization_id == organization_id,
                    WebhookLeadModel.phone.in_(wanted),
                    WebhookLeadModel.status == "do_not_call",
                )
                .distinct()
            )
            return set(result.scalars().all())

    async def list_stuck_calling_webhook_leads(
        self, *, not_updated_since: datetime, limit: int = 200
    ) -> List[WebhookLeadModel]:
        """Leads still "calling" with no update since the cutoff (the call's
        end never reached us, e.g. a restart mid-call). The system sweep: not
        org-scoped, each lead carries its own organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel)
                .where(
                    WebhookLeadModel.status == "calling",
                    WebhookLeadModel.updated_at < not_updated_since,
                )
                .order_by(WebhookLeadModel.updated_at)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def is_webhook_phone_opted_out(
        self, organization_id: int, phone: str
    ) -> bool:
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel.id)
                .where(
                    WebhookLeadModel.organization_id == organization_id,
                    WebhookLeadModel.phone == phone,
                    WebhookLeadModel.status == "do_not_call",
                )
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def list_webhook_endpoints_with_paused_calling(
        self,
    ) -> List[WebhookEndpointModel]:
        """Active endpoints whose campaign is paused: the circuit breaker
        stopped calling after too many failed calls. The system sweep: not
        org-scoped."""
        from api.db.models import CampaignModel

        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookEndpointModel)
                .join(
                    CampaignModel, CampaignModel.id == WebhookEndpointModel.campaign_id
                )
                .where(
                    WebhookEndpointModel.is_active.is_(True),
                    CampaignModel.state == "paused",
                )
            )
            return list(result.scalars().all())

    async def list_dead_webhook_sync_callbacks(
        self, *, since: datetime
    ) -> Dict[Tuple[int, int], int]:
        """CRM callbacks that gave up for good since ``since``, counted per
        (organization, endpoint). The system sweep: not org-scoped."""
        from api.db.models import WebhookDeliveryModel

        async with self.async_session() as session:
            result = await session.execute(
                select(
                    WebhookDeliveryModel.organization_id, WebhookDeliveryModel.payload
                )
                .where(
                    WebhookDeliveryModel.status == "dead_letter",
                    WebhookDeliveryModel.webhook_node_id.like("webhook_sync_lead_%"),
                    WebhookDeliveryModel.updated_at >= since,
                )
                .limit(1000)
            )
            counts: Dict[Tuple[int, int], int] = {}
            for organization_id, payload in result.all():
                endpoint_id = ((payload or {}).get("endpoint") or {}).get("id")
                if isinstance(endpoint_id, int):
                    key = (organization_id, endpoint_id)
                    counts[key] = counts.get(key, 0) + 1
            return counts

    async def restart_webhook_delivery(
        self, delivery_id: int, organization_id: int, payload: Dict[str, Any]
    ) -> Optional[Any]:
        """Send a delivery again from scratch with a fresh payload (a CRM
        callback that gave up)."""
        from api.db.models import WebhookDeliveryModel

        async with self.async_session() as session:
            result = await session.execute(
                update(WebhookDeliveryModel)
                .where(
                    WebhookDeliveryModel.id == delivery_id,
                    WebhookDeliveryModel.organization_id == organization_id,
                    WebhookDeliveryModel.status != "pending",
                )
                .values(
                    status="pending",
                    attempt_count=0,
                    scheduled_for=datetime.now(UTC),
                    last_error=None,
                    last_status_code=None,
                    payload=payload,
                )
                .returning(WebhookDeliveryModel)
            )
            delivery = result.scalar_one_or_none()
            await session.commit()
            # The commit expired it; load it again so callers can read it.
            if delivery is not None:
                await session.refresh(delivery)
            return delivery

    async def list_leads_awaiting_call(
        self, *, older_than: datetime, newer_than: datetime, limit: int = 200
    ) -> List[Tuple[WebhookLeadModel, WebhookEndpointModel]]:
        """New leads of active auto-call endpoints that never got queued (the
        receiver's enqueue failed), oldest first. Not org-scoped: this is the
        system sweep, and each lead is paired with its own endpoint."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookLeadModel, WebhookEndpointModel)
                .join(
                    WebhookEndpointModel,
                    WebhookEndpointModel.id == WebhookLeadModel.endpoint_id,
                )
                .where(
                    WebhookLeadModel.status == "received",
                    WebhookLeadModel.queued_run_id.is_(None),
                    WebhookLeadModel.received_at < older_than,
                    WebhookLeadModel.received_at >= newer_than,
                    WebhookEndpointModel.is_active.is_(True),
                    WebhookEndpointModel.auto_call.is_(True),
                )
                .order_by(WebhookLeadModel.received_at)
                .limit(limit)
            )
            return [(lead, endpoint) for lead, endpoint in result.all()]

    async def webhook_sync_stats(
        self,
        organization_id: int,
        *,
        endpoint_id: Optional[int] = None,
        since: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Dashboard numbers over leads received since ``since``: how many were
        called and connected, how fast the first call went out, request errors
        and CRM callback outcomes."""
        from api.db.models import WebhookDeliveryModel, WorkflowRunModel

        lead_filters = [WebhookLeadModel.organization_id == organization_id]
        log_filters = [WebhookRequestLogModel.organization_id == organization_id]
        if endpoint_id is not None:
            lead_filters.append(WebhookLeadModel.endpoint_id == endpoint_id)
            log_filters.append(WebhookRequestLogModel.endpoint_id == endpoint_id)
        if since is not None:
            lead_filters.append(WebhookLeadModel.received_at >= since)
            log_filters.append(WebhookRequestLogModel.received_at >= since)

        not_callable = WebhookLeadModel.status.in_(NON_ORIGINAL_STATUSES)
        lead_id_in_run = cast(
            WorkflowRunModel.initial_context.op("->>")("webhook_lead_id"), Integer
        )
        first_calls = (
            select(
                lead_id_in_run.label("lead_id"),
                func.min(WorkflowRunModel.created_at).label("first_call_at"),
            )
            .where(
                WorkflowRunModel.campaign_id.in_(
                    select(WebhookEndpointModel.campaign_id).where(
                        WebhookEndpointModel.organization_id == organization_id,
                        WebhookEndpointModel.campaign_id.isnot(None),
                    )
                ),
                WorkflowRunModel.initial_context.op("->>")("webhook_lead_id").isnot(
                    None
                ),
            )
            .group_by(lead_id_in_run)
            .subquery()
        )
        wait_seconds = func.extract(
            "epoch", first_calls.c.first_call_at - WebhookLeadModel.received_at
        )

        async with self.async_session() as session:
            leads = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(~not_callable),
                        func.count().filter(WebhookLeadModel.call_attempts > 0),
                        func.count().filter(WebhookLeadModel.status == "completed"),
                    ).where(*lead_filters)
                )
            ).one()
            median_wait = (
                await session.execute(
                    select(func.percentile_cont(0.5).within_group(wait_seconds))
                    .select_from(WebhookLeadModel)
                    .join(first_calls, first_calls.c.lead_id == WebhookLeadModel.id)
                    .where(*lead_filters)
                )
            ).scalar_one_or_none()
            requests = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(
                            WebhookRequestLogModel.response_code >= 400
                        ),
                    ).where(*log_filters)
                )
            ).one()
            callbacks = (
                await session.execute(
                    select(WebhookDeliveryModel.status, func.count())
                    .join(
                        WebhookLeadModel,
                        WebhookDeliveryModel.webhook_node_id
                        == func.concat("webhook_sync_lead_", WebhookLeadModel.id),
                    )
                    .where(
                        WebhookDeliveryModel.organization_id == organization_id,
                        *lead_filters,
                    )
                    .group_by(WebhookDeliveryModel.status)
                )
            ).all()

        total, callable_n, called, connected = (int(n or 0) for n in leads)
        return {
            "leads": total,
            "callable": callable_n,
            "called": called,
            "connected": connected,
            "connect_rate": round(connected / called, 4) if called else None,
            "median_seconds_to_first_call": (
                round(float(median_wait), 1) if median_wait is not None else None
            ),
            "requests": int(requests[0] or 0),
            "request_errors": int(requests[1] or 0),
            "callbacks": {status: int(n) for status, n in callbacks},
        }

    async def get_webhook_lead_callback(
        self, lead_id: int, organization_id: int
    ) -> Optional[Any]:
        """The CRM callback delivery for a lead, if one was queued."""
        from api.db.models import WebhookDeliveryModel

        async with self.async_session() as session:
            result = await session.execute(
                select(WebhookDeliveryModel).where(
                    WebhookDeliveryModel.organization_id == organization_id,
                    WebhookDeliveryModel.webhook_node_id
                    == f"webhook_sync_lead_{lead_id}",
                )
            )
            return result.scalars().first()

    async def count_webhook_leads_by_status(
        self,
        organization_id: int,
        *,
        endpoint_id: Optional[int] = None,
        today: Optional[datetime] = None,
    ) -> Tuple[Dict[str, int], int]:
        """({status: count}, leads since ``today``) for the dashboard cards."""
        filters = [WebhookLeadModel.organization_id == organization_id]
        if endpoint_id is not None:
            filters.append(WebhookLeadModel.endpoint_id == endpoint_id)
        today = today or _start_of_today_utc()
        async with self.async_session() as session:
            rows = await session.execute(
                select(WebhookLeadModel.status, func.count())
                .where(*filters)
                .group_by(WebhookLeadModel.status)
            )
            by_status = {status: int(n) for status, n in rows.all()}
            today_n = (
                await session.execute(
                    select(func.count()).where(
                        *filters, WebhookLeadModel.received_at >= today
                    )
                )
            ).scalar_one()
            return by_status, int(today_n or 0)

    # ======== REQUEST LOGS ========

    async def create_webhook_request_log(self, **fields: Any) -> WebhookRequestLogModel:
        async with self.async_session() as session:
            log = WebhookRequestLogModel(**fields)
            session.add(log)
            await session.commit()
            await session.refresh(log)
            return log

    async def list_webhook_request_logs(
        self,
        organization_id: int,
        endpoint_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[WebhookRequestLogModel], int]:
        filters = [
            WebhookRequestLogModel.organization_id == organization_id,
            WebhookRequestLogModel.endpoint_id == endpoint_id,
        ]
        async with self.async_session() as session:
            total = (
                await session.execute(select(func.count()).where(*filters))
            ).scalar_one()
            result = await session.execute(
                select(WebhookRequestLogModel)
                .where(*filters)
                .order_by(
                    WebhookRequestLogModel.received_at.desc(),
                    WebhookRequestLogModel.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), int(total)

    async def delete_webhook_request_logs_before(self, cutoff: datetime) -> int:
        async with self.async_session() as session:
            result = await session.execute(
                delete(WebhookRequestLogModel).where(
                    WebhookRequestLogModel.received_at < cutoff
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    # ======== RETENTION ========

    async def clear_webhook_lead_payloads_before(self, cutoff: datetime) -> int:
        """Drop the full CRM payload from leads received before ``cutoff``;
        the lead's own fields stay."""
        async with self.async_session() as session:
            result = await session.execute(
                update(WebhookLeadModel)
                .where(
                    WebhookLeadModel.received_at < cutoff,
                    WebhookLeadModel.raw_payload.isnot(None),
                )
                .values(raw_payload=None)
            )
            await session.commit()
            return int(result.rowcount or 0)

    # ======== AUDIT LOG ========

    async def create_webhook_audit_log(
        self,
        *,
        organization_id: int,
        endpoint_id: Optional[int],
        endpoint_name: Optional[str],
        user_id: Optional[int],
        action: str,
        changes: Optional[Dict[str, Any]] = None,
    ) -> WebhookEndpointAuditLogModel:
        async with self.async_session() as session:
            entry = WebhookEndpointAuditLogModel(
                organization_id=organization_id,
                endpoint_id=endpoint_id,
                endpoint_name=endpoint_name,
                user_id=user_id,
                action=action,
                changes=changes,
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            return entry

    async def list_webhook_audit_logs(
        self,
        organization_id: int,
        *,
        endpoint_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[WebhookEndpointAuditLogModel], int]:
        filters = [WebhookEndpointAuditLogModel.organization_id == organization_id]
        if endpoint_id is not None:
            filters.append(WebhookEndpointAuditLogModel.endpoint_id == endpoint_id)
        async with self.async_session() as session:
            total = (
                await session.execute(select(func.count()).where(*filters))
            ).scalar_one()
            result = await session.execute(
                select(WebhookEndpointAuditLogModel)
                .where(*filters)
                .order_by(
                    WebhookEndpointAuditLogModel.created_at.desc(),
                    WebhookEndpointAuditLogModel.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all()), int(total)

    async def get_webhook_user_emails(self, user_ids: List[int]) -> Dict[int, str]:
        """Emails of the users named in audit entries."""
        if not user_ids:
            return {}
        async with self.async_session() as session:
            result = await session.execute(
                select(UserModel.id, UserModel.email).where(
                    UserModel.id.in_(set(user_ids))
                )
            )
            return {uid: email for uid, email in result.all() if email}
