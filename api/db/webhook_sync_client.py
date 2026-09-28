"""Database access for Webhook Sync endpoints, leads and request logs.

Every read and write of an endpoint, lead or log is scoped by
``organization_id`` except the receiver's endpoint lookup, which resolves the
organization *from* the endpoint's unguessable UUID.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from api.db.base_client import BaseDBClient
from api.db.webhook_sync_models import (
    WebhookEndpointAuditLogModel,
    WebhookEndpointModel,
    WebhookLeadModel,
    WebhookRequestLogModel,
)
from api.db.models import WorkflowModel

# Lead statuses that mean "this lead was not really received as a new one".
NON_ORIGINAL_STATUSES = ("invalid_number", "duplicate")


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
        self, organization_id: int
    ) -> List[Tuple[WebhookEndpointModel, Optional[str], int, int]]:
        """Endpoints with (workflow name, leads today, leads total)."""
        today = _start_of_today_utc()
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
        self, endpoint_id: int, organization_id: int
    ) -> Tuple[int, int]:
        """(leads today, leads total) for one endpoint."""
        today = _start_of_today_utc()
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
        **fields: Any,
    ) -> Tuple[WebhookLeadModel, bool]:
        """Store a lead; returns (lead, replayed).

        ``replayed`` is True when the request repeats one we already have
        (same Idempotency-Key or same CRM lead id): the existing lead comes
        back and nothing new is stored. Otherwise a lead whose phone already
        arrived on this endpoint within the dedupe window is stored as
        ``duplicate``. The phone check holds a per-(endpoint, phone) advisory
        lock so two simultaneous requests can't both pass as new.
        """
        replay_filters = []
        if idempotency_key:
            replay_filters.append(WebhookLeadModel.idempotency_key == idempotency_key)
        if external_lead_id:
            replay_filters.append(WebhookLeadModel.external_lead_id == external_lead_id)

        async def _find_replay(session) -> Optional[WebhookLeadModel]:
            if not replay_filters:
                return None
            result = await session.execute(
                select(WebhookLeadModel)
                .where(
                    WebhookLeadModel.endpoint_id == endpoint.id,
                    or_(*replay_filters),
                )
                .order_by(WebhookLeadModel.id)
                .limit(1)
            )
            return result.scalar_one_or_none()

        try:
            async with self.async_session() as session:
                async with session.begin():
                    lead, replayed = await self._insert_or_replay(
                        session,
                        _find_replay,
                        endpoint=endpoint,
                        phone=phone,
                        dedupe_window_hours=dedupe_window_hours,
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
                existing = await _find_replay(session)
                if existing is None:
                    raise
                return existing, True

    async def _insert_or_replay(
        self,
        session,
        find_replay,
        *,
        endpoint: WebhookEndpointModel,
        phone: Optional[str],
        dedupe_window_hours: int,
        status: str,
        status_reason: Optional[str],
        idempotency_key: Optional[str],
        external_lead_id: Optional[str],
        fields: Dict[str, Any],
    ) -> Tuple[WebhookLeadModel, bool]:
        """Inside one transaction: the replayed lead, or a newly stored one."""
        existing = await find_replay(session)
        if existing is not None:
            return existing, True

        duplicate_of = None
        if phone and status == "received" and dedupe_window_hours > 0:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _advisory_key(endpoint.id, phone)},
            )
            since = datetime.now(UTC) - timedelta(hours=dedupe_window_hours)
            result = await session.execute(
                select(WebhookLeadModel.id)
                .where(
                    WebhookLeadModel.endpoint_id == endpoint.id,
                    WebhookLeadModel.phone == phone,
                    WebhookLeadModel.received_at >= since,
                    WebhookLeadModel.status.notin_(NON_ORIGINAL_STATUSES),
                )
                .order_by(WebhookLeadModel.received_at)
                .limit(1)
            )
            duplicate_of = result.scalar_one_or_none()
            if duplicate_of is not None:
                status = "duplicate"
                status_reason = f"Same number received within {dedupe_window_hours}h"

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
