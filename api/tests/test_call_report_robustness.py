"""Robustness of the call report against data that is not what it should be.

Everything a report is built from comes out of JSON columns and model output, so it
can be any shape. A report that cannot be built or stored means the call silently
never appears on the dashboard (or the run page returns an error), so the builder
must degrade to a partial report and never raise, and what it produces must always
be storable in JSONB and sendable as JSON.
"""

import asyncio
import json
import math
import random
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.models import (
    CallReportModel,
    OrganizationModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.services.call_report.builder import RunSnapshot, build_call_report
from api.services.call_report.sanitize import json_safe
from api.services.call_report.schema import CallReport
from api.services.call_report.service import build_and_store_call_report

NUL = chr(0)


def _snapshot(**overrides) -> RunSnapshot:
    base = dict(
        run_id=1,
        workflow_id=1,
        organization_id=1,
        created_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
        mode="voicelink",
        call_type="inbound",
        is_completed=True,
        initial_context={"caller_number": "+911234567890"},
        gathered_context={"mapped_call_disposition": "user_hangup"},
        usage_info={"call_duration_seconds": 60},
        annotations={},
        events=[],
    )
    base.update(overrides)
    return RunSnapshot(**base)


class TestJsonSafe:
    def test_nul_characters_and_non_finite_numbers_are_removed_recursively(self):
        dirty = {
            "a" + NUL + "b": ["x" + NUL, float("nan"), float("inf"), 1.5, {"k": NUL}],
            "n": float("-inf"),
        }

        assert json_safe(dirty) == {"ab": ["x", None, None, 1.5, {"k": ""}], "n": None}

    def test_ordinary_values_are_left_alone(self):
        value = {"s": "text", "i": 3, "f": 2.5, "b": True, "n": None, "l": [1, "a"]}

        assert json_safe(value) == value


class TestBuilderNeverRaises:
    @pytest.mark.parametrize(
        "field, junk",
        [
            ("annotations", ["not", "a", "dict"]),
            ("annotations", "a string"),
            ("gathered_context", ["list"]),
            ("initial_context", "text"),
            ("usage_info", 42),
            ("events", "not a list"),
            ("events", [1, "two", None, ["three"]]),
        ],
    )
    def test_a_column_of_the_wrong_shape_gives_a_partial_report(self, field, junk):
        report = build_call_report(_snapshot(**{field: junk}))

        assert isinstance(report, CallReport)
        assert report.analysis.status == "not_run"
        assert report.ticket.count == 0

    def test_events_that_are_not_the_shape_they_should_be_are_skipped(self):
        events = [
            {"type": "rtf-function-call-end", "payload": "not a dict"},
            {
                "type": "rtf-function-call-end",
                "payload": {"function_name": {"a": 1}, "result": "x"},
            },
            {
                "type": "rtf-function-call-end",
                "payload": {"function_name": ["create_complaint2"], "result": "x"},
            },
            {
                "type": "rtf-function-call-start",
                "payload": {"tool_call_id": {"unhashable": True}},
            },
            {"type": "rtf-function-call-start", "payload": [1, 2]},
        ]

        assert build_call_report(_snapshot(events=events)).ticket.count == 0

    def test_a_ticket_with_a_garbage_timestamp_keeps_the_ticket_and_drops_the_time(
        self,
    ):
        result = (
            "{'status': 'success', 'status_code': 200, 'data': {'status': 'success', "
            "'http_status': 201, 'data': {'d': {'ExObjectId': '7000002403'}}}}"
        )
        events = [
            {
                "type": "rtf-function-call-end",
                "timestamp": {"not": "a string"},
                "payload": {
                    "function_name": "create_complaint2",
                    "tool_call_id": "c1",
                    "result": result,
                },
            }
        ]

        (ticket,) = build_call_report(_snapshot(events=events)).ticket.tickets

        assert ticket.ticket_id == "7000002403"
        assert ticket.created_at is None

    def test_an_id_pattern_that_matched_noise_is_not_a_ticket(self):
        noise = "'ExObjectId': '" + "9" * 500 + "'"
        events = [
            {
                "type": "rtf-function-call-end",
                "payload": {
                    "function_name": "create_complaint2",
                    "result": "http_status: 201 " + noise,
                },
            }
        ]

        assert build_call_report(_snapshot(events=events)).ticket.count == 0

    @pytest.mark.parametrize(
        "duration", [float("nan"), float("inf"), -5, "abc", True, [1], {"a": 1}]
    )
    def test_a_duration_that_is_not_a_real_number_is_no_duration(self, duration):
        report = build_call_report(
            _snapshot(usage_info={"call_duration_seconds": duration})
        )

        assert report.call.duration_seconds is None
        assert report.outcome.successful is False

    @pytest.mark.parametrize("junk", [{"a": 1}, ["x"], 12345, True, "", "   "])
    def test_a_phone_number_or_disconnect_reason_of_the_wrong_type_is_ignored(
        self, junk
    ):
        report = build_call_report(
            _snapshot(
                initial_context={"caller_number": junk, "phone_number": junk},
                gathered_context={
                    "mapped_call_disposition": junk,
                    "customer_phone_number": junk,
                },
            )
        )

        assert report.call.phone_number is None
        assert report.disconnect.reason is None
        assert report.disconnect.category == "not_finished"

    def test_an_unknown_call_type_is_not_carried_into_the_report(self):
        assert build_call_report(_snapshot(call_type="sideways")).call.call_type is None

    def test_a_qa_result_of_the_wrong_shape_is_an_error_status_not_a_crash(self):
        annotations = {
            "qa_5": {
                "node_results": {
                    "1": {
                        "raw_response": {"not": "a string"},
                        "tags": [1, None, {"tag": 5}],
                    }
                }
            }
        }

        report = build_call_report(_snapshot(annotations=annotations))

        assert report.analysis.status in {"error", "analysed"}

    def test_nul_characters_and_nan_never_reach_the_stored_report(self):
        raw = json.dumps(
            {
                "summary": "bad" + NUL + "text",
                "overall_sentiment": "positive",
                "issue_type": "x" + NUL,
            }
        )
        report = build_call_report(
            _snapshot(
                annotations={"qa_5": {"node_results": {"1": {"raw_response": raw}}}},
                gathered_context={
                    "mapped_call_disposition": "user_hangup",
                    "extracted_variables": {
                        "note": "a" + NUL + "b",
                        "score": float("nan"),
                        "ok": 1.5,
                    },
                },
            )
        )

        dumped = json.dumps(report.model_dump(mode="json"), allow_nan=False)
        assert NUL not in dumped and "\\u0000" not in dumped
        assert report.analysis.summary == "badtext"
        assert report.captured == {"note": "ab", "ok": 1.5}


# --------------------------------------------------------------------- fuzzing

JUNK = [
    None,
    "",
    "x",
    " ",
    "abc" * 50,
    0,
    -1,
    3.5,
    float("nan"),
    float("inf"),
    True,
    False,
    [],
    {},
    ["a"],
    [1, None],
    {"a": None},
    NUL + "bad",
    "a" + NUL + "b",
    "ok",
    10**30,
    -(10**30),
    "9" * 400,
]
KEYS = [
    "status",
    "data",
    "tag",
    "summary",
    "raw_response",
    "node_results",
    "result",
    "payload",
    "function_name",
    "timestamp",
    "ExObjectId",
    "http_status",
    "overall_sentiment",
    "issue_type",
    "tags",
    "call_quality_score",
]


def _junk(rng: random.Random, depth: int = 0):
    kind = rng.random()
    if depth < 3 and kind < 0.25:
        return {
            rng.choice(KEYS): _junk(rng, depth + 1) for _ in range(rng.randint(0, 4))
        }
    if depth < 3 and kind < 0.4:
        return [_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return rng.choice(JUNK)


def _hostile_snapshot(rng: random.Random) -> RunSnapshot:
    events = []
    for _ in range(rng.randint(0, 8)):
        events.append(
            rng.choice(
                [
                    _junk(rng),
                    {
                        "type": "rtf-function-call-end",
                        "timestamp": _junk(rng),
                        "payload": {
                            "function_name": rng.choice(
                                ["create_complaint2", "closed_ticket", _junk(rng)]
                            ),
                            "tool_call_id": _junk(rng),
                            "result": _junk(rng),
                        },
                    },
                    {
                        "type": "rtf-function-call-start",
                        "payload": {
                            "function_name": _junk(rng),
                            "tool_call_id": _junk(rng),
                            "arguments": _junk(rng),
                        },
                    },
                ]
            )
        )
    return RunSnapshot(
        run_id=1,
        workflow_id=1,
        organization_id=1,
        created_at=rng.choice([datetime(2026, 9, 17, tzinfo=UTC), None]),
        mode=rng.choice(["voicelink", "", "smallwebrtc"]),
        call_type=rng.choice(["inbound", "outbound", None, "weird"]),
        is_completed=rng.choice([True, False]),
        initial_context=rng.choice(
            [{}, {"caller_number": _junk(rng), "phone_number": _junk(rng)}]
        ),
        gathered_context=rng.choice(
            [
                {},
                {
                    "mapped_call_disposition": _junk(rng),
                    "extracted_variables": _junk(rng),
                },
            ]
        ),
        usage_info=rng.choice([{}, {"call_duration_seconds": _junk(rng)}]),
        annotations=rng.choice(
            [
                {},
                {"qa_5": _junk(rng)},
                {"qa_1": {"node_results": _junk(rng)}},
                _junk(rng),
            ]
        ),
        events=events,
    )


def test_a_thousand_hostile_snapshots_never_crash_and_always_produce_storable_json():
    rng = random.Random(20260920)

    for iteration in range(1000):
        report = build_call_report(_hostile_snapshot(rng))

        assert isinstance(report, CallReport), iteration
        # Strict JSON: no NaN/Infinity. And nothing Postgres JSONB refuses.
        dumped = json.dumps(report.model_dump(mode="json"), allow_nan=False)
        assert "\\u0000" not in dumped, iteration
        assert report.call.duration_seconds is None or math.isfinite(
            report.call.duration_seconds
        )


# ----------------------------------------------------------------- the database


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


async def _make_run(db_session_factory, **overrides):
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
        fields = dict(
            name=f"test-run-{uuid.uuid4().hex[:8]}",
            workflow_id=workflow.id,
            mode="voicelink",
            call_type="inbound",
            is_completed=True,
            initial_context={"caller_number": "+911234567890"},
            gathered_context={"mapped_call_disposition": "user_hangup"},
            usage_info={"call_duration_seconds": 60},
            annotations={},
            logs={},
        )
        fields.update(overrides)
        run = WorkflowRunModel(**fields)
        session.add(run)
        await session.flush()
        await session.commit()
        return org.id, user.id, workflow.id, run.id


async def _cleanup(db_session_factory, org_id, user_id, workflow_id) -> None:
    async with db_session_factory() as session:
        await session.execute(
            delete(WorkflowRunModel).where(WorkflowRunModel.workflow_id == workflow_id)
        )
        await session.execute(
            delete(WorkflowModel).where(WorkflowModel.id == workflow_id)
        )
        await session.execute(delete(UserModel).where(UserModel.id == user_id))
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == org_id)
        )
        await session.commit()


class TestStoring:
    async def test_a_run_whose_text_contains_a_nul_character_still_gets_its_report(
        self, db_session_factory
    ):
        # A model can write a NUL escape inside its JSON answer. The run stores that as
        # harmless text; it only becomes a NUL character once the answer is parsed, and
        # the report's JSONB column refuses NUL. (A raw NUL cannot be in a run at all:
        # Postgres rejects it even in a JSON column.)
        raw = json.dumps(
            {"summary": "bad" + NUL + "text", "overall_sentiment": "positive"}
        )
        org_id, user_id, workflow_id, run_id = await _make_run(
            db_session_factory,
            annotations={"qa_5": {"node_results": {"1": {"raw_response": raw}}}},
        )
        try:
            report = await build_and_store_call_report(run_id)

            assert report is not None
            async with db_session_factory() as session:
                row = (
                    await session.execute(
                        select(CallReportModel).where(
                            CallReportModel.workflow_run_id == run_id
                        )
                    )
                ).scalar_one()
            assert row.report["analysis"]["summary"] == "badtext"
        finally:
            await _cleanup(db_session_factory, org_id, user_id, workflow_id)

    async def test_many_simultaneous_rebuilds_of_one_call_leave_exactly_one_report(
        self, db_session_factory
    ):
        org_id, user_id, workflow_id, run_id = await _make_run(db_session_factory)
        try:
            results = await asyncio.gather(
                *(build_and_store_call_report(run_id) for _ in range(12)),
                return_exceptions=True,
            )

            assert not [r for r in results if isinstance(r, Exception)]
            async with db_session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(CallReportModel).where(
                                CallReportModel.workflow_run_id == run_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(rows) == 1
        finally:
            await _cleanup(db_session_factory, org_id, user_id, workflow_id)
