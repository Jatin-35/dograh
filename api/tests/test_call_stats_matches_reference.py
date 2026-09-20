"""The dashboard's single-pass SQL against an independent Python calculation.

The statistics come out of one ``GROUPING SETS`` query. That is exactly the kind of
query that can be subtly wrong (a NULL key confused with a missing grouping, a filter
applied to the wrong measure), so it is checked here against a plain-Python
recomputation over the same random reports, across several filters and timezones.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CallReportModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.call_report.schema import (
    AnalysisSection,
    CallReport,
    CallSection,
    CapabilitiesSection,
    DisconnectSection,
    OutcomeSection,
    TicketSection,
)

DISCONNECTS = [
    "customer_hung_up",
    "agent_completed",
    "silence_timeout",
    "system_error",
    "not_connected",
]
OUTCOMES = ["ticket_closed", "no_outcome", "resolved", "failed", "transferred"]
SENTIMENTS = ["positive", "neutral", "negative", None]
REASONS = ["recharge", "meter_balance", "payment", None]
QA_STATUSES = ["analysed", "not_run", "skipped", "error"]


@pytest.fixture(scope="module")
async def db_session_factory(setup_test_database):
    from api.db import db_client

    engine = create_async_engine(setup_test_database, echo=False)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    original_engine = db_client.engine
    original_session = db_client.async_session
    db_client.engine = engine
    db_client.async_session = session_factory

    yield session_factory

    db_client.engine = original_engine
    db_client.async_session = original_session
    await engine.dispose()


def _random_report(
    rng: random.Random, run_id: int, workflow_id: int, org_id: int, when: datetime
) -> CallReport:
    created = rng.random() < 0.3
    status = rng.choice(QA_STATUSES)
    return CallReport(
        run_id=run_id,
        workflow_id=workflow_id,
        organization_id=org_id,
        call=CallSection(
            call_type=rng.choice(["inbound", "inbound", "outbound"]),
            mode="voicelink",
            is_telephony=rng.random() < 0.85,
            started_at=when.isoformat(),
            duration_seconds=rng.choice([None, round(rng.uniform(0, 400), 1)]),
        ),
        disconnect=DisconnectSection(category=rng.choice(DISCONNECTS), label="x"),
        outcome=OutcomeSection(
            code=rng.choice(OUTCOMES), label="x", successful=rng.random() < 0.7
        ),
        ticket=TicketSection(
            created=created,
            closed=created and rng.random() < 0.6,
            count=rng.randint(1, 3) if created else 0,
        ),
        analysis=AnalysisSection(
            status=status,
            sentiment=rng.choice(SENTIMENTS),
            satisfied=rng.choice([True, False, None]),
            reason_for_call=rng.choice(REASONS),
        ),
        capabilities=CapabilitiesSection(
            ticket=rng.random() < 0.5, analysis=rng.random() < 0.5
        ),
    )


def _reference(
    reports: list[CallReport], *, start, end, tz, workflow_id, call_type, include_test
) -> dict:
    zone = ZoneInfo(tz)
    scoped = []
    for r in reports:
        when = datetime.fromisoformat(r.call.started_at)
        if not (start <= when <= end):
            continue
        if not include_test and not r.call.is_telephony:
            continue
        if workflow_id is not None and r.workflow_id != workflow_id:
            continue
        if call_type and r.call.call_type != call_type:
            continue
        scoped.append((r, when.astimezone(zone)))

    def tally(items):
        out: dict = {}
        for key in items:
            out[key] = out.get(key, 0) + 1
        return out

    durations = [
        r.call.duration_seconds
        for r, _ in scoped
        if r.call.duration_seconds is not None
    ]
    analysed = [(r, local) for r, local in scoped if r.analysis.status == "analysed"]
    return {
        "total": len(scoped),
        "inbound": sum(r.call.call_type == "inbound" for r, _ in scoped),
        "outbound": sum(r.call.call_type == "outbound" for r, _ in scoped),
        "successful": sum(r.outcome.successful for r, _ in scoped),
        "failed": sum(
            r.disconnect.category in ("system_error", "not_connected")
            for r, _ in scoped
        ),
        "avg_duration": (sum(durations) / len(durations)) if durations else None,
        "total_duration": sum(durations),
        "with_ticket": sum(r.ticket.created for r, _ in scoped),
        "ticket_closed": sum(r.ticket.created and r.ticket.closed for r, _ in scoped),
        "tickets_created": sum(r.ticket.count for r, _ in scoped),
        "analysed": len(analysed),
        "ticket_capable": any(r.capabilities.ticket for r, _ in scoped),
        "analysis_capable": any(r.capabilities.analysis for r, _ in scoped),
        "outcome": tally(r.outcome.code for r, _ in scoped),
        "disconnect": tally(r.disconnect.category for r, _ in scoped),
        "sentiment": tally(
            r.analysis.sentiment
            for r, _ in analysed
            if r.analysis.sentiment is not None
        ),
        "satisfaction": tally(r.analysis.satisfied for r, _ in analysed),
        "reason": tally(
            r.analysis.reason_for_call
            for r, _ in analysed
            if r.analysis.reason_for_call is not None
        ),
        "daily": sorted(
            (
                day,
                calls,
                sum(
                    1
                    for r, local in scoped
                    if local.date() == day and r.outcome.successful
                ),
            )
            for day, calls in tally(local.date() for _, local in scoped).items()
        ),
        "hourly": sorted(tally(local.hour for _, local in scoped).items()),
    }


def _actual(raw: dict) -> dict:
    t = raw["totals"]
    return {
        "total": t["total"],
        "inbound": t["inbound"],
        "outbound": t["outbound"],
        "successful": t["successful"],
        "failed": t["failed"],
        "avg_duration": None if t["avg_duration"] is None else float(t["avg_duration"]),
        "total_duration": float(t["total_duration"]),
        "with_ticket": t["with_ticket"],
        "ticket_closed": t["ticket_closed"],
        "tickets_created": int(t["tickets_created"]),
        "analysed": t["analysed"],
        "ticket_capable": t["ticket_capable"],
        "analysis_capable": t["analysis_capable"],
        "outcome": dict(raw["outcome"]),
        "disconnect": dict(raw["disconnect"]),
        "sentiment": dict(raw["sentiment"]),
        "satisfaction": dict(raw["satisfaction"]),
        "reason": dict(raw["reason"]),
        "daily": sorted(raw["daily"]),
        "hourly": sorted(raw["hourly"]),
    }


def _assert_same(actual: dict, expected: dict, label: str) -> None:
    for key, want in expected.items():
        got = actual[key]
        if isinstance(want, float) or isinstance(got, float):
            assert got == pytest.approx(want, rel=1e-6, abs=1e-6), f"{label}: {key}"
        else:
            assert got == want, f"{label}: {key}: {got!r} != {want!r}"


class TestSinglePassQueryMatchesReference:
    @pytest.mark.parametrize("seed", [1, 2, 3])
    async def test_random_reports_across_filters_and_timezones(
        self, db_session_factory, seed
    ):
        from api.db import db_client

        rng = random.Random(seed)
        async with db_session_factory() as session:
            org = OrganizationModel(provider_id=f"test-org-{uuid.uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            user = UserModel(
                provider_id=f"test-user-{uuid.uuid4().hex[:8]}",
                selected_organization_id=org.id,
            )
            session.add(user)
            await session.flush()
            workflows = []
            for _ in range(3):
                workflow = WorkflowModel(
                    name=f"test-workflow-{uuid.uuid4().hex[:8]}",
                    user_id=user.id,
                    organization_id=org.id,
                    workflow_definition={"nodes": [], "edges": []},
                    template_context_variables={},
                )
                session.add(workflow)
                workflows.append(workflow)
            await session.flush()

            base = datetime(2026, 9, 1, tzinfo=UTC)
            planned = []
            for _ in range(300):
                workflow = rng.choice(workflows)
                when = base + timedelta(seconds=rng.randint(0, 20 * 24 * 3600))
                run = WorkflowRunModel(
                    name=f"test-run-{uuid.uuid4().hex[:8]}",
                    workflow_id=workflow.id,
                    mode="voicelink",
                    call_type="inbound",
                    is_completed=True,
                    created_at=when,
                    initial_context={},
                    gathered_context={},
                    usage_info={},
                    annotations={},
                    logs={},
                )
                session.add(run)
                planned.append((run, workflow.id, when))
            await session.flush()
            reports = [
                _random_report(rng, run.id, workflow_id, org.id, when)
                for run, workflow_id, when in planned
            ]
            await session.commit()
            workflow_ids = [w.id for w in workflows]
            user_id, org_id = user.id, org.id

        try:
            for report in reports:
                await db_client.upsert_call_report(report)

            cases = [
                dict(
                    tz="Asia/Kolkata",
                    start=datetime(2026, 9, 3, 18, 30, tzinfo=UTC),
                    end=datetime(2026, 9, 12, 18, 29, 59, tzinfo=UTC),
                    workflow_id=None,
                    call_type=None,
                    include_test=False,
                ),
                dict(
                    tz="UTC",
                    start=datetime(2026, 9, 1, tzinfo=UTC),
                    end=datetime(2026, 9, 21, tzinfo=UTC),
                    workflow_id=None,
                    call_type=None,
                    include_test=True,
                ),
                dict(
                    tz="America/Los_Angeles",
                    start=datetime(2026, 9, 5, tzinfo=UTC),
                    end=datetime(2026, 9, 9, tzinfo=UTC),
                    workflow_id=workflow_ids[0],
                    call_type=None,
                    include_test=False,
                ),
                dict(
                    tz="Asia/Kolkata",
                    start=datetime(2026, 9, 1, tzinfo=UTC),
                    end=datetime(2026, 9, 21, tzinfo=UTC),
                    workflow_id=None,
                    call_type="outbound",
                    include_test=True,
                ),
                dict(
                    tz="Australia/Sydney",
                    start=datetime(2026, 9, 1, tzinfo=UTC),
                    end=datetime(2026, 9, 21, tzinfo=UTC),
                    workflow_id=workflow_ids[1],
                    call_type="inbound",
                    include_test=False,
                ),
                dict(
                    tz="UTC",
                    start=datetime(2027, 1, 1, tzinfo=UTC),
                    end=datetime(2027, 1, 2, tzinfo=UTC),
                    workflow_id=None,
                    call_type=None,
                    include_test=True,
                ),
            ]
            for index, case in enumerate(cases):
                raw = await db_client.get_call_stats(
                    organization_id=org_id,
                    start_utc=case["start"],
                    end_utc=case["end"],
                    timezone=case["tz"],
                    workflow_id=case["workflow_id"],
                    call_type=case["call_type"],
                    include_test_calls=case["include_test"],
                )
                expected = _reference(reports, **case)
                _assert_same(_actual(raw), expected, f"seed {seed}, case {index}")
        finally:
            async with db_session_factory() as session:
                await session.execute(
                    delete(CallReportModel).where(
                        CallReportModel.organization_id == org_id
                    )
                )
                await session.execute(
                    delete(WorkflowRunModel).where(
                        WorkflowRunModel.workflow_id.in_(workflow_ids)
                    )
                )
                await session.execute(
                    delete(WorkflowModel).where(WorkflowModel.id.in_(workflow_ids))
                )
                await session.execute(delete(UserModel).where(UserModel.id == user_id))
                await session.execute(
                    delete(OrganizationModel).where(OrganizationModel.id == org_id)
                )
                await session.commit()
