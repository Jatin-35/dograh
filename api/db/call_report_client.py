from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.db.base_client import BaseDBClient
from api.db.models import CallReportModel, WorkflowModel, WorkflowRunModel
from api.services.call_report.schema import CallReport


def _columns(report: CallReport) -> dict:
    """The typed, indexable columns derived from a report."""
    analysis = report.analysis
    return {
        "organization_id": report.organization_id,
        "workflow_id": report.workflow_id,
        "call_started_at": datetime.fromisoformat(report.call.started_at),
        "call_type": report.call.call_type,
        "is_telephony": report.call.is_telephony,
        "duration_seconds": report.call.duration_seconds,
        "disconnect_category": report.disconnect.category,
        "outcome": report.outcome.code,
        "is_successful": report.outcome.successful,
        "sentiment": analysis.sentiment,
        "satisfied": analysis.satisfied,
        "reason_for_call": analysis.reason_for_call,
        "ticket_count": report.ticket.count,
        "ticket_created": report.ticket.created,
        "ticket_closed": report.ticket.closed,
        "qa_status": analysis.status,
        "ticket_capable": report.capabilities.ticket,
        "analysis_capable": report.capabilities.analysis,
        "schema_version": report.schema_version,
        "report": report.model_dump(mode="json"),
    }


# The grouping columns, in the order GROUPING() reports them (leftmost = highest bit).
_STATS_GROUP_COLUMNS = (
    "outcome",
    "disconnect_category",
    "sentiment",
    "satisfied",
    "reason_for_call",
    "day",
    "hour",
)
_GRAND_TOTAL_ID = (1 << len(_STATS_GROUP_COLUMNS)) - 1

# One pass over the scoped rows, one output row per group. `gid` says which grouping
# a row belongs to (GROUPING() is 1 for a column that is *not* part of the set), so a
# NULL key (an unknown sentiment, say) is never confused with "no grouping".
_CALL_STATS_SQL = """
WITH scoped AS (
    SELECT is_successful, disconnect_category, outcome, sentiment, satisfied,
           reason_for_call, call_type, duration_seconds, ticket_created,
           ticket_closed, ticket_count, qa_status, ticket_capable, analysis_capable,
           (timezone(:timezone, call_started_at))::date AS day,
           (extract(hour FROM timezone(:timezone, call_started_at)))::int AS hour
    FROM call_reports
    WHERE {where}
)
SELECT
    GROUPING(outcome, disconnect_category, sentiment, satisfied,
             reason_for_call, day, hour) AS gid,
    outcome, disconnect_category, sentiment, satisfied, reason_for_call, day, hour,
    count(*) AS calls,
    count(*) FILTER (WHERE qa_status = 'analysed') AS analysed_calls,
    count(*) FILTER (WHERE is_successful IS TRUE) AS successful,
    count(*) FILTER (WHERE call_type = 'inbound') AS inbound,
    count(*) FILTER (WHERE call_type = 'outbound') AS outbound,
    count(*) FILTER (
        WHERE disconnect_category IN ('system_error', 'not_connected')
    ) AS failed,
    avg(duration_seconds) AS avg_duration,
    coalesce(sum(duration_seconds), 0) AS total_duration,
    count(*) FILTER (WHERE ticket_created IS TRUE) AS with_ticket,
    count(*) FILTER (WHERE ticket_created IS TRUE AND ticket_closed IS TRUE) AS ticket_closed,
    coalesce(sum(ticket_count), 0) AS tickets_created,
    coalesce(bool_or(ticket_capable), false) AS ticket_capable,
    coalesce(bool_or(analysis_capable), false) AS analysis_capable
FROM scoped
GROUP BY GROUPING SETS (
    (), (outcome), (disconnect_category), (sentiment), (satisfied),
    (reason_for_call), (day), (hour)
)
"""


def _grouped_column(gid: int) -> Optional[str]:
    """The one column a row is grouped by, or None for the grand total."""
    if gid == _GRAND_TOTAL_ID:
        return None
    for position, column in enumerate(_STATS_GROUP_COLUMNS):
        if not gid & (1 << (len(_STATS_GROUP_COLUMNS) - 1 - position)):
            return column
    raise ValueError(f"Unexpected grouping id {gid}")


def _assemble_call_stats(rows) -> dict:
    """Turn the grouped rows back into the shape the dashboard service expects."""
    totals: dict = {}
    by_column: dict[str, list] = {column: [] for column in _STATS_GROUP_COLUMNS}

    for row in rows:
        column = _grouped_column(row["gid"])
        if column is None:
            totals = {
                "total": row["calls"],
                "inbound": row["inbound"],
                "outbound": row["outbound"],
                "successful": row["successful"],
                "failed": row["failed"],
                "avg_duration": row["avg_duration"],
                "total_duration": row["total_duration"],
                "with_ticket": row["with_ticket"],
                "ticket_closed": row["ticket_closed"],
                "tickets_created": row["tickets_created"],
                "analysed": row["analysed_calls"],
                "ticket_capable": row["ticket_capable"],
                "analysis_capable": row["analysis_capable"],
            }
        else:
            by_column[column].append(row)

    def counts(column: str, measure: str = "calls", skip_null: bool = False):
        pairs = [
            (row[column], row[measure])
            for row in by_column[column]
            if row[measure] > 0 and not (skip_null and row[column] is None)
        ]
        return sorted(pairs, key=lambda pair: (-pair[1], str(pair[0])))

    return {
        "totals": totals,
        "outcome": counts("outcome"),
        "disconnect": counts("disconnect_category"),
        # Sentiment, satisfaction and reasons are only meaningful for analysed calls.
        "sentiment": counts("sentiment", "analysed_calls", skip_null=True),
        "satisfaction": counts("satisfied", "analysed_calls"),
        "reason": counts("reason_for_call", "analysed_calls", skip_null=True),
        "daily": sorted(
            (row["day"], row["calls"], row["successful"]) for row in by_column["day"]
        ),
        "hourly": sorted((int(row["hour"]), row["calls"]) for row in by_column["hour"]),
    }


class CallReportClient(BaseDBClient):
    async def upsert_call_report(self, report: CallReport) -> None:
        """Insert the report for a run, or replace it if one already exists.

        Idempotent by design: the report is a pure function of the run, so it is
        rebuilt whenever new data (QA finishing late) lands, and the latest wins.
        """
        values = {"workflow_run_id": report.run_id, **_columns(report)}
        update = {**_columns(report), "updated_at": datetime.now(UTC)}

        async with self.async_session() as session:
            statement = (
                pg_insert(CallReportModel)
                .values(**values, created_at=datetime.now(UTC))
                .on_conflict_do_update(
                    constraint="uq_call_reports_workflow_run", set_=update
                )
            )
            await session.execute(statement)
            await session.commit()

    async def get_call_report(
        self, workflow_run_id: int, organization_id: int
    ) -> Optional[CallReport]:
        """The stored report for a run, scoped to the caller's organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(CallReportModel.report).where(
                    CallReportModel.workflow_run_id == workflow_run_id,
                    CallReportModel.organization_id == organization_id,
                )
            )
            stored = result.scalar_one_or_none()

        if stored is None:
            return None
        return CallReport.model_validate(stored)

    async def list_run_ids_for_report_backfill(
        self,
        *,
        after_id: int,
        limit: int,
        organization_id: Optional[int] = None,
        workflow_id: Optional[int] = None,
        since: Optional[datetime] = None,
        only_missing: bool = True,
    ) -> list[int]:
        """The next batch of completed runs to (re)build reports for, by ascending id.

        Keyset-paginated on the run id, so a large backfill never re-scans what it
        has already handled and stays cheap however many runs there are. With
        ``only_missing`` it returns just the runs that have no report yet.
        """
        query = (
            select(WorkflowRunModel.id)
            .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
            .where(
                WorkflowRunModel.id > after_id,
                WorkflowRunModel.is_completed.is_(True),
            )
        )
        if only_missing:
            query = query.outerjoin(
                CallReportModel, CallReportModel.workflow_run_id == WorkflowRunModel.id
            ).where(CallReportModel.id.is_(None))
        if organization_id is not None:
            query = query.where(WorkflowModel.organization_id == organization_id)
        if workflow_id is not None:
            query = query.where(WorkflowRunModel.workflow_id == workflow_id)
        if since is not None:
            query = query.where(WorkflowRunModel.created_at >= since)

        async with self.async_session() as session:
            result = await session.execute(
                query.order_by(WorkflowRunModel.id).limit(limit)
            )
            return list(result.scalars().all())

    async def get_call_stats(
        self,
        *,
        organization_id: int,
        start_utc: datetime,
        end_utc: datetime,
        timezone: str,
        workflow_id: Optional[int] = None,
        call_type: Optional[str] = None,
        include_test_calls: bool = False,
    ) -> dict:
        """Aggregate a range of calls into the numbers a dashboard shows.

        The range is read **once**: every breakdown (totals, outcomes, endings,
        sentiment, satisfaction, reasons, days, hours) comes out of one pass over
        the rows, using ``GROUPING SETS``. Reading it once per breakdown would cost
        the range seven times over. The query filters on ``organization_id`` and a
        ``call_started_at`` range, so it is served by ``ix_call_reports_org_started``
        and its cost is proportional to the calls in range, not to how many
        reports the platform holds. Days and hours are bucketed in ``timezone`` so
        "today" means the organization's today.

        Only fixed SQL fragments are assembled here; every value is a bound
        parameter.
        """
        conditions = [
            "organization_id = :organization_id",
            "call_started_at >= :start_utc",
            "call_started_at <= :end_utc",
        ]
        params: dict = {
            "organization_id": organization_id,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "timezone": timezone,
        }
        if not include_test_calls:
            conditions.append("is_telephony IS TRUE")
        if workflow_id is not None:
            conditions.append("workflow_id = :workflow_id")
            params["workflow_id"] = workflow_id
        if call_type:
            conditions.append("call_type = :call_type")
            params["call_type"] = call_type

        statement = text(_CALL_STATS_SQL.format(where=" AND ".join(conditions)))
        async with self.async_session() as session:
            rows = (await session.execute(statement, params)).mappings().all()

        return _assemble_call_stats(rows)
