"""Tests for the dashboard statistics: range/timezone rules (pure), the aggregation
query, its filters and organization isolation (database), and the route."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.routes.call_stats import router as call_stats_router
from api.services.auth.depends import get_user
from api.services.call_report.service import build_and_store_call_report
from api.services.call_report.stats import (
    StatsRangeError,
    build_stats_response,
    resolve_range,
)

IST = "Asia/Kolkata"


class TestResolveRange:
    def test_a_local_day_becomes_inclusive_utc_bounds(self):
        start, end, _ = resolve_range(date(2026, 9, 18), date(2026, 9, 18), IST)

        assert start == datetime(2026, 9, 17, 18, 30, tzinfo=UTC)
        assert end.date() == date(2026, 9, 18)
        assert (end.hour, end.minute, end.second) == (18, 29, 59)

    def test_end_date_is_inclusive_of_the_whole_day(self):
        start, end, _ = resolve_range(date(2026, 9, 1), date(2026, 9, 7), "UTC")

        assert (end - start).days == 6
        assert end > datetime(2026, 9, 7, 23, 59, tzinfo=UTC)

    @pytest.mark.parametrize(
        "start, end, tz",
        [
            (date(2026, 9, 10), date(2026, 9, 9), "UTC"),  # reversed
            (date(2025, 1, 1), date(2026, 9, 9), "UTC"),  # longer than a year
            (date(2026, 9, 1), date(2026, 9, 9), "Mars/Olympus"),  # unknown timezone
        ],
    )
    def test_unservable_ranges_are_rejected(self, start, end, tz):
        with pytest.raises(StatsRangeError):
            resolve_range(start, end, tz)


def _raw(**overrides):
    raw = {
        "totals": {
            "total": 4,
            "inbound": 3,
            "outbound": 1,
            "successful": 2,
            "failed": 1,
            "avg_duration": 69.04,
            "total_duration": 276,
            "with_ticket": 2,
            "ticket_closed": 1,
            "tickets_created": 2,
            "analysed": 2,
            "ticket_capable": True,
            "analysis_capable": True,
        },
        "outcome": [("ticket_closed", 1), ("failed", 1)],
        "disconnect": [("customer_hung_up", 3), ("system_error", 1)],
        "sentiment": [("positive", 1)],
        "satisfaction": [(True, 1), (None, 1)],
        "reason": [("recharge", 1)],
        "daily": [(date(2026, 9, 2), 3, 2)],
        "hourly": [(9, 3), (14, 1)],
    }
    raw.update(overrides)
    return raw


class TestBuildStatsResponse:
    def test_missing_days_and_hours_are_filled_with_zeros(self):
        body = build_stats_response(
            _raw(),
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 4),
            timezone="UTC",
        )

        assert [d["date"] for d in body["daily"]] == [
            "2026-09-01",
            "2026-09-02",
            "2026-09-03",
            "2026-09-04",
        ]
        assert [d["calls"] for d in body["daily"]] == [0, 3, 0, 0]
        assert len(body["hourly"]) == 24
        assert body["hourly"][9]["calls"] == 3 and body["hourly"][3]["calls"] == 0

    def test_kpis_and_rates(self):
        kpis = build_stats_response(
            _raw(),
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 4),
            timezone="UTC",
        )["kpis"]

        assert kpis["total_calls"] == 4
        assert kpis["success_rate"] == 50.0
        assert kpis["avg_duration_seconds"] == 69.0
        assert kpis["calls_with_open_ticket"] == 1

    def test_an_empty_range_has_no_rate_or_average(self):
        empty = _raw(
            totals={
                "total": 0,
                "inbound": 0,
                "outbound": 0,
                "successful": 0,
                "failed": 0,
                "avg_duration": None,
                "total_duration": 0,
                "with_ticket": 0,
                "ticket_closed": 0,
                "tickets_created": 0,
                "analysed": 0,
                "ticket_capable": False,
                "analysis_capable": False,
            },
            outcome=[],
            disconnect=[],
            sentiment=[],
            satisfaction=[],
            reason=[],
            daily=[],
            hourly=[],
        )

        body = build_stats_response(
            empty,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            timezone="UTC",
        )

        assert body["kpis"]["success_rate"] is None
        assert body["kpis"]["avg_duration_seconds"] is None
        assert [s["count"] for s in body["satisfaction"]] == [0, 0, 0]

    def test_capabilities_are_passed_through(self):
        body = build_stats_response(
            _raw(),
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 4),
            timezone="UTC",
        )
        assert body["capabilities"] == {"tickets": True, "analysis": True}

    def test_labels_and_satisfaction_buckets(self):
        body = build_stats_response(
            _raw(),
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 4),
            timezone="UTC",
        )

        assert body["outcomes"][0] == {
            "key": "ticket_closed",
            "label": "Ticket created and closed",
            "count": 1,
        }
        assert body["disconnections"][1]["label"] == "System error"
        assert {s["key"]: s["count"] for s in body["satisfaction"]} == {
            "yes": 1,
            "no": 0,
            "unclear": 1,
        }


CREATE_RESULT = (
    "{'status': 'success', 'status_code': 200, 'data': {'status': 'success', "
    "'http_status': 201, 'data': {'d': {'ExObjectId': '%s'}}}}"
)
CLOSE_RESULT = "{'status': 'success', 'status_code': 200, 'data': {'http_status': 201}}"


def _qa(**fields):
    import json

    return {"qa_5": {"node_results": {"1": {"raw_response": json.dumps(fields)}}}}


def _events(*, closed: bool):
    events = [
        {
            "type": "rtf-function-call-end",
            "timestamp": "2026-09-17T09:00:00+00:00",
            "payload": {
                "function_name": "create_complaint2",
                "tool_call_id": "c1",
                "result": CREATE_RESULT % uuid.uuid4().hex[:10],
            },
        }
    ]
    if closed:
        events.append(
            {
                "type": "rtf-function-call-end",
                "timestamp": "2026-09-17T09:00:10+00:00",
                "payload": {
                    "function_name": "closed_ticket",
                    "tool_call_id": "c2",
                    "result": CLOSE_RESULT,
                },
            }
        )
    return {"realtime_feedback_events": events}


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


@dataclass
class Ids:
    organization_id: int
    user_id: int
    workflow_id: int


async def _create_org(db_session_factory) -> Ids:
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
        workflow = WorkflowModel(
            name=f"test-workflow-{uuid.uuid4().hex[:8]}",
            user_id=user.id,
            organization_id=org.id,
            workflow_definition={"nodes": [], "edges": []},
            template_context_variables={},
        )
        session.add(workflow)
        await session.flush()
        await session.commit()
        return Ids(org.id, user.id, workflow.id)


async def _add_call(
    db_session_factory, ids: Ids, *, workflow_id=None, **overrides
) -> int:
    fields = dict(
        name=f"test-run-{uuid.uuid4().hex[:8]}",
        workflow_id=workflow_id or ids.workflow_id,
        mode="voicelink",
        call_type="inbound",
        is_completed=True,
        created_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
        initial_context={"caller_number": "+911234567890"},
        gathered_context={"mapped_call_disposition": "user_hangup"},
        usage_info={"call_duration_seconds": 100},
        annotations={},
        logs={},
    )
    fields.update(overrides)
    async with db_session_factory() as session:
        run = WorkflowRunModel(**fields)
        session.add(run)
        await session.flush()
        await session.commit()
        run_id = run.id
    await build_and_store_call_report(run_id)
    return run_id


async def _cleanup(db_session_factory, *ids: Ids) -> None:
    async with db_session_factory() as session:
        for item in ids:
            organization_workflows = select(WorkflowModel.id).where(
                WorkflowModel.organization_id == item.organization_id
            )
            await session.execute(
                delete(WorkflowRunModel).where(
                    WorkflowRunModel.workflow_id.in_(organization_workflows)
                )
            )
            await session.execute(
                delete(WorkflowModel).where(
                    WorkflowModel.organization_id == item.organization_id
                )
            )
            await session.execute(delete(UserModel).where(UserModel.id == item.user_id))
            await session.execute(
                delete(OrganizationModel).where(
                    OrganizationModel.id == item.organization_id
                )
            )
        await session.commit()


async def _fetch(organization_id: int, **params) -> httpx.Response:
    app = FastAPI()
    app.include_router(call_stats_router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=1, is_superuser=False, selected_organization_id=organization_id
    )
    query = {
        "start_date": "2026-09-17",
        "end_date": "2026-09-17",
        "timezone": "UTC",
        **params,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/organizations/reports/call-stats", params=query)


async def _seed_scenario(db_session_factory, ids: Ids) -> None:
    # r1: resolved inbound call, ticket raised and closed.
    await _add_call(
        db_session_factory,
        ids,
        usage_info={"call_duration_seconds": 120},
        annotations=_qa(
            overall_sentiment="positive",
            issue_type="recharge",
            issue_resolved=True,
            human_transfer=False,
            customer_experience={"user_satisfied": True},
        ),
        logs=_events(closed=True),
    )
    # r2: transferred to a person, ticket left open.
    await _add_call(
        db_session_factory,
        ids,
        usage_info={"call_duration_seconds": 60},
        gathered_context={"mapped_call_disposition": "user_qualified"},
        annotations=_qa(
            overall_sentiment="neutral",
            issue_type="meter_balance",
            issue_resolved=False,
            human_transfer=True,
        ),
        logs=_events(closed=False),
    )
    # r3: hung up in six seconds — finished, but too short to count as successful.
    await _add_call(db_session_factory, ids, usage_info={"call_duration_seconds": 6})
    # r4: outbound call that hit a system error.
    await _add_call(
        db_session_factory,
        ids,
        call_type="outbound",
        usage_info={"call_duration_seconds": 90},
        gathered_context={"mapped_call_disposition": "pipeline_error"},
    )
    # r5: a browser test session — left out unless asked for.
    await _add_call(db_session_factory, ids, mode="smallwebrtc", call_type="outbound")


class TestCallStatsQuery:
    async def test_aggregates_a_realistic_day(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            await _seed_scenario(db_session_factory, ids)

            body = (await _fetch(ids.organization_id)).json()

            k = body["kpis"]
            assert (k["total_calls"], k["inbound_calls"], k["outbound_calls"]) == (
                4,
                3,
                1,
            )
            assert (k["successful_calls"], k["failed_calls"], k["success_rate"]) == (
                2,
                1,
                50.0,
            )
            assert k["avg_duration_seconds"] == 69.0
            assert k["total_duration_seconds"] == 276.0
            assert (k["calls_with_ticket"], k["tickets_created"]) == (2, 2)
            assert (k["calls_with_closed_ticket"], k["calls_with_open_ticket"]) == (
                1,
                1,
            )
            assert k["analysed_calls"] == 2

            assert {i["key"]: i["count"] for i in body["outcomes"]} == {
                "ticket_closed": 1,
                "ticket_open_transferred": 1,
                "no_outcome": 1,
                "failed": 1,
            }
            assert {i["key"]: i["count"] for i in body["sentiment"]} == {
                "positive": 1,
                "neutral": 1,
            }
            assert {i["key"]: i["count"] for i in body["reasons"]} == {
                "recharge": 1,
                "meter_balance": 1,
            }
            assert {i["key"]: i["count"] for i in body["satisfaction"]} == {
                "yes": 1,
                "no": 0,
                "unclear": 1,
            }
            assert {i["key"]: i["count"] for i in body["disconnections"]} == {
                "customer_hung_up": 2,
                "agent_completed": 1,
                "system_error": 1,
            }
            assert body["daily"] == [
                {"date": "2026-09-17", "calls": 4, "successful": 2}
            ]
            assert body["hourly"][9]["calls"] == 4
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_capabilities_follow_what_the_agents_in_scope_can_produce(
        self, db_session_factory
    ):
        with_extras = await _create_org(db_session_factory)
        plain = await _create_org(db_session_factory)
        try:
            await _seed_scenario(db_session_factory, with_extras)  # tickets and QA
            await _add_call(db_session_factory, plain)  # no ticket tool, no QA

            rich = (await _fetch(with_extras.organization_id)).json()
            bare = (await _fetch(plain.organization_id)).json()

            assert rich["capabilities"] == {"tickets": True, "analysis": True}
            assert bare["capabilities"] == {"tickets": False, "analysis": False}
        finally:
            await _cleanup(db_session_factory, with_extras, plain)

    async def test_test_calls_are_included_only_on_request(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            await _seed_scenario(db_session_factory, ids)

            body = (await _fetch(ids.organization_id, include_test_calls="true")).json()

            assert body["kpis"]["total_calls"] == 5
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_call_type_and_agent_filters(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            await _seed_scenario(db_session_factory, ids)
            async with db_session_factory() as session:
                second = WorkflowModel(
                    name=f"second-{uuid.uuid4().hex[:6]}",
                    user_id=ids.user_id,
                    organization_id=ids.organization_id,
                    workflow_definition={"nodes": [], "edges": []},
                    template_context_variables={},
                )
                session.add(second)
                await session.flush()
                await session.commit()
                second_id = second.id
            await _add_call(db_session_factory, ids, workflow_id=second_id)

            inbound = (await _fetch(ids.organization_id, call_type="inbound")).json()
            first_agent = (
                await _fetch(ids.organization_id, workflow_id=ids.workflow_id)
            ).json()
            second_agent = (
                await _fetch(ids.organization_id, workflow_id=second_id)
            ).json()

            assert inbound["kpis"]["total_calls"] == 4  # 3 + the second agent's call
            assert first_agent["kpis"]["total_calls"] == 4
            assert second_agent["kpis"]["total_calls"] == 1
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_days_and_hours_follow_the_requested_timezone(
        self, db_session_factory
    ):
        ids = await _create_org(db_session_factory)
        try:
            # 20:00 UTC on the 17th is 01:30 IST on the 18th.
            await _add_call(
                db_session_factory,
                ids,
                created_at=datetime(2026, 9, 17, 20, 0, tzinfo=UTC),
            )

            on_the_17th = (await _fetch(ids.organization_id, timezone=IST)).json()
            on_the_18th = (
                await _fetch(
                    ids.organization_id,
                    timezone=IST,
                    start_date="2026-09-18",
                    end_date="2026-09-18",
                )
            ).json()

            assert on_the_17th["kpis"]["total_calls"] == 0
            assert on_the_18th["kpis"]["total_calls"] == 1
            assert on_the_18th["daily"] == [
                {"date": "2026-09-18", "calls": 1, "successful": 1}
            ]
            assert on_the_18th["hourly"][1]["calls"] == 1
        finally:
            await _cleanup(db_session_factory, ids)

    async def test_another_organizations_calls_are_never_counted(
        self, db_session_factory
    ):
        mine = await _create_org(db_session_factory)
        theirs = await _create_org(db_session_factory)
        try:
            await _add_call(db_session_factory, mine)
            await _add_call(db_session_factory, theirs)
            await _add_call(db_session_factory, theirs)

            assert (await _fetch(mine.organization_id)).json()["kpis"][
                "total_calls"
            ] == 1
            assert (await _fetch(theirs.organization_id)).json()["kpis"][
                "total_calls"
            ] == 2
        finally:
            await _cleanup(db_session_factory, mine, theirs)

    async def test_calls_outside_the_range_are_not_counted(self, db_session_factory):
        ids = await _create_org(db_session_factory)
        try:
            await _add_call(
                db_session_factory,
                ids,
                created_at=datetime(2026, 9, 16, 23, 59, tzinfo=UTC),
            )
            await _add_call(
                db_session_factory,
                ids,
                created_at=datetime(2026, 9, 18, 0, 0, 1, tzinfo=UTC),
            )

            assert (await _fetch(ids.organization_id)).json()["kpis"][
                "total_calls"
            ] == 0
        finally:
            await _cleanup(db_session_factory, ids)


class TestCallStatsRoute:
    async def test_bad_input_is_a_client_error(self, db_session_factory):
        assert (await _fetch(1, timezone="Mars/Olympus")).status_code == 400
        assert (
            await _fetch(1, start_date="2026-09-18", end_date="2026-09-17")
        ).status_code == 400
        assert (await _fetch(1, start_date="not-a-date")).status_code == 422
        assert (await _fetch(1, call_type="sideways")).status_code == 422

    async def test_a_user_without_an_organization_is_rejected(self, db_session_factory):
        app = FastAPI()
        app.include_router(call_stats_router)
        app.dependency_overrides[get_user] = lambda: SimpleNamespace(
            id=1, is_superuser=False, selected_organization_id=None
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get(
                "/organizations/reports/call-stats",
                params={
                    "start_date": "2026-09-17",
                    "end_date": "2026-09-17",
                    "timezone": "UTC",
                },
            )

        assert response.status_code == 400
