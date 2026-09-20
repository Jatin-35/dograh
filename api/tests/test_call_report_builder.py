"""Tests for the pure call-report builder (no database).

Fixtures mirror what production actually stores: tool results are ``str(dict)``
(single-quoted Python repr, with a double-quoted URL inside), and QA output is the
raw JSON text the reviewer returned — including the two-node shape of run 413.
"""

import json
from datetime import UTC, datetime

from api.services.call_report.builder import RunSnapshot, build_call_report
from api.services.call_report.analysis import extract_analysis
from api.services.call_report.capabilities import (
    has_ticket_tool,
    qa_enabled_in_definition,
    tool_function_name,
    tool_uuids_in_definition,
)
from api.services.call_report.disconnect import classify_disconnect
from api.services.call_report.tickets import extract_tickets

CREATE = "create_complaint2"
CLOSE = "closed_ticket"


def _create_result(ticket_id: str) -> str:
    return (
        "{'status': 'success', 'status_code': 200, 'data': {'status': 'success', "
        "'message': 'Complaint created successfully.', "
        "'speak': 'Complaint created successfully.', 'http_status': 201, "
        "'data': {'d': {'__metadata': {'id': "
        f"\"https://ucp.think-gas.com/sap/opu/odata/sap/ZCM_COMPLAINTS_SRV/CreateComplaintSet('{ticket_id}')\", "
        "'type': 'ZCM_COMPLAINTS_SRV.CreateComplaint'}, "
        f"'ExObjectId': '{ticket_id}', 'ExMessage': 'SR: {ticket_id} created succesfully'}}}}}}}}"
    )


CLOSE_RESULT = (
    "{'status': 'success', 'status_code': 200, 'data': {'status': 'success', "
    "'message': 'Ticket closed.', 'http_status': 201, 'data': {'d': {'ExObjectId': ''}}}}"
)


def _start(tool, call_id, arguments=None):
    payload = {"function_name": tool, "tool_call_id": call_id}
    if arguments is not None:
        payload["arguments"] = arguments
    return {"type": "rtf-function-call-start", "payload": payload}


def _end(tool, call_id, result, ts):
    return {
        "type": "rtf-function-call-end",
        "timestamp": ts,
        "payload": {"function_name": tool, "tool_call_id": call_id, "result": result},
    }


class TestTicketExtraction:
    def test_reads_id_and_creation_time_from_repr_result(self):
        events = [
            _end(
                CREATE,
                "c1",
                _create_result("7000002403"),
                "2026-09-17T09:37:52.783+00:00",
            )
        ]

        tickets = extract_tickets(events)

        assert len(tickets) == 1
        assert tickets[0].ticket_id == "7000002403"
        assert tickets[0].created_at == "2026-09-17T09:37:52.783+00:00"
        assert tickets[0].closed is False
        assert tickets[0].source == "think_gas_sap"

    def test_close_marks_newest_open_ticket_and_records_time(self):
        events = [
            _end(
                CREATE,
                "c1",
                _create_result("7000002402"),
                "2026-09-17T08:59:54.540+00:00",
            ),
            _end(CLOSE, "c2", CLOSE_RESULT, "2026-09-17T09:01:18.145+00:00"),
        ]

        (ticket,) = extract_tickets(events)

        assert ticket.closed is True
        assert ticket.closed_at == "2026-09-17T09:01:18.145+00:00"

    def test_duplicate_create_leaves_the_unclosed_ticket_open(self):
        # Run 363: two tickets four seconds apart, one closure.
        events = [
            _end(
                CREATE,
                "c1",
                _create_result("7000002382"),
                "2026-09-16T04:25:59.792+00:00",
            ),
            _end(
                CREATE,
                "c2",
                _create_result("7000002383"),
                "2026-09-16T04:26:03.968+00:00",
            ),
            _end(CLOSE, "c3", CLOSE_RESULT, "2026-09-16T04:26:13.306+00:00"),
        ]

        first, second = extract_tickets(events)

        assert (first.ticket_id, first.closed) == ("7000002382", False)
        assert (second.ticket_id, second.closed) == ("7000002383", True)

    def test_close_naming_a_ticket_closes_that_one(self):
        events = [
            _end(
                CREATE, "c1", _create_result("7000002382"), "2026-09-16T04:25:59+00:00"
            ),
            _end(
                CREATE, "c2", _create_result("7000002383"), "2026-09-16T04:26:03+00:00"
            ),
            _start(CLOSE, "c3", {"ExObjectId": "7000002382"}),
            _end(CLOSE, "c3", CLOSE_RESULT, "2026-09-16T04:26:13+00:00"),
        ]

        first, second = extract_tickets(events)

        assert (first.closed, second.closed) == (True, False)

    def test_real_run_363_closure_closes_the_ticket_named_by_lvobjectid(self):
        # Arguments exactly as stored for run 363: the ticket is named by
        # LvObjectId, and the free-text notes carry a garbled, different number.
        closure_arguments = {
            "Lvnotes": "Rahul, 70009477382, Ludhiana, request being closed by voicebot.",
            "LvObjectId": "7000002383",
            "csrf_token": "not-a-real-token",
        }
        events = [
            _end(
                CREATE,
                "c1",
                _create_result("7000002382"),
                "2026-09-16T04:25:59.792+00:00",
            ),
            _end(
                CREATE,
                "c2",
                _create_result("7000002383"),
                "2026-09-16T04:26:03.968+00:00",
            ),
            _start(CLOSE, "c3", closure_arguments),
            _end(CLOSE, "c3", CLOSE_RESULT, "2026-09-16T04:26:13.306+00:00"),
        ]

        first, second = extract_tickets(events)

        assert (first.ticket_id, first.closed) == ("7000002382", False)
        assert (second.ticket_id, second.closed) == ("7000002383", True)

    def test_a_longer_number_containing_a_ticket_id_does_not_count_as_naming_it(self):
        events = [
            _end(
                CREATE, "c1", _create_result("7000002382"), "2026-09-16T04:25:59+00:00"
            ),
            _end(
                CREATE, "c2", _create_result("7000002383"), "2026-09-16T04:26:03+00:00"
            ),
            # "70000023820" merely starts with the first ticket's ID.
            _start(
                CLOSE, "c3", {"Lvnotes": "ref 70000023820", "LvObjectId": "7000002383"}
            ),
            _end(CLOSE, "c3", CLOSE_RESULT, "2026-09-16T04:26:13+00:00"),
        ]

        first, second = extract_tickets(events)

        assert (first.closed, second.closed) == (False, True)

    def test_failed_create_is_not_a_ticket(self):
        failed = (
            "{'status': 'error', 'status_code': 500, "
            "'data': {'status': 'error', 'http_status': 500}}"
        )

        assert (
            extract_tickets([_end(CREATE, "c1", failed, "2026-09-17T09:00:00+00:00")])
            == []
        )

    def test_empty_id_is_not_a_ticket(self):
        empty = (
            "{'status': 'success', 'status_code': 200, "
            "'data': {'status': 'success', 'http_status': 201, 'data': {'d': {'ExObjectId': ''}}}}"
        )

        assert (
            extract_tickets([_end(CREATE, "c1", empty, "2026-09-17T09:00:00+00:00")])
            == []
        )

    def test_json_result_is_read_too(self):
        result = json.dumps(
            {
                "status": "success",
                "status_code": 200,
                "data": {"ticket_id": "T-42", "http_status": 201},
            }
        )

        (ticket,) = extract_tickets(
            [_end(CREATE, "c1", result, "2026-09-17T09:00:00+00:00")]
        )

        assert ticket.ticket_id == "T-42"

    def test_truncated_result_falls_back_to_pattern(self):
        cut = _create_result("7000002403")[:-25]  # no longer parseable

        (ticket,) = extract_tickets(
            [_end(CREATE, "c1", cut, "2026-09-17T09:00:00+00:00")]
        )

        assert ticket.ticket_id == "7000002403"

    def test_other_tools_are_ignored(self):
        events = [
            _end(
                "query_taluka_tg",
                "c1",
                _create_result("7000002403"),
                "2026-09-17T09:00:00+00:00",
            )
        ]

        assert extract_tickets(events) == []

    def test_unrelated_tool_results_are_never_parsed(self, monkeypatch):
        from api.services.call_report import tickets

        def _fail(_text):
            raise AssertionError(
                "parsed the result of a tool that is not a ticket tool"
            )

        monkeypatch.setattr(tickets, "_parse_result", _fail)
        events = [
            _end(
                "retrieve_from_knowledge_base",
                "c1",
                "x" * 100_000,
                "2026-09-17T09:00:00+00:00",
            )
        ]

        assert extract_tickets(events) == []

    def test_close_without_any_ticket_is_ignored(self):
        assert (
            extract_tickets(
                [_end(CLOSE, "c1", CLOSE_RESULT, "2026-09-17T09:00:00+00:00")]
            )
            == []
        )


# Run 413: node 1 only saw the greeting, node 2 saw the whole story.
_NODE_1 = json.dumps(
    {
        "tags": [],
        "overall_sentiment": "neutral",
        "call_quality_score": 10,
        "summary": "The agent greeted the caller. The caller expressed a balance recharge issue.",
        "issue_type": "recharge",
        "issue_resolved": None,
        "human_transfer": False,
        "customer_experience": {"user_satisfied": None, "user_mood": "neutral"},
    }
)
_NODE_2 = (
    "```json\n"
    + json.dumps(
        {
            "tags": [
                {
                    "tag": "CALLER_NUMBER_LOOKUP_MISSED",
                    "reason": "asked registered number first",
                },
                {"tag": "TOOL_PROGRESS_UPDATE_MISSED", "reason": "no progress updates"},
            ],
            "overall_sentiment": "neutral",
            "call_quality_score": 8,
            "summary": "Created a complaint ticket and transferred to senior staff.",
            "issue_type": "meter_balance",
            "issue_resolved": False,
            "human_transfer": True,
            "customer_experience": {"user_satisfied": None, "user_mood": "neutral"},
        }
    )
    + "\n```"
)


def _annotations(*node_results):
    return {
        "qa_5": {
            "node_results": {
                str(i + 1): {"node_name": f"node {i + 1}", "raw_response": raw}
                for i, raw in enumerate(node_results)
            },
            "model": "test",
        },
        "tags": ["ignored-top-level"],
    }


class TestAnalysisRollup:
    def test_last_node_wins_and_transfer_is_any(self):
        section = extract_analysis(_annotations(_NODE_1, _NODE_2))

        assert section.status == "analysed"
        assert section.reason_for_call == "meter_balance"
        assert section.resolved is False
        assert section.human_transfer is True
        assert section.sentiment == "neutral"
        assert section.quality_score == 8
        assert (
            section.summary
            == "Created a complaint ticket and transferred to senior staff."
        )
        assert section.tags == [
            "CALLER_NUMBER_LOOKUP_MISSED",
            "TOOL_PROGRESS_UPDATE_MISSED",
        ]
        assert [n.node_name for n in section.nodes] == ["node 1", "node 2"]

    def test_unknown_reason_does_not_override_a_real_one(self):
        later = json.dumps({"issue_type": "unknown", "summary": "x"})

        section = extract_analysis(_annotations(_NODE_1, later))

        assert section.reason_for_call == "recharge"

    def test_generic_keys_are_understood_too(self):
        raw = json.dumps(
            {
                "reason_for_call": "Billing Query",
                "satisfied": "yes",
                "sentiment": "POSITIVE",
            }
        )

        section = extract_analysis(_annotations(raw))

        assert section.reason_for_call == "billing_query"
        assert section.satisfied is True
        assert section.sentiment == "positive"

    def test_invalid_values_are_dropped_not_stored(self):
        raw = json.dumps(
            {"overall_sentiment": "ecstatic", "call_quality_score": 99, "summary": "ok"}
        )

        (node,) = extract_analysis(_annotations(raw)).nodes

        assert node.sentiment is None
        assert node.quality_score is None
        assert node.summary == "ok"

    def test_unparseable_response_falls_back_to_persisted_fields(self):
        annotations = {
            "qa_5": {
                "node_results": {
                    "1": {
                        "node_name": "n",
                        "raw_response": "not json at all",
                        "overall_sentiment": "negative",
                        "summary": "kept",
                        "score": 4,
                        "tags": [{"tag": "USER_FRUSTRATED", "reason": "r"}],
                    }
                }
            }
        }

        section = extract_analysis(annotations)

        assert section.status == "analysed"
        assert (section.sentiment, section.summary, section.quality_score) == (
            "negative",
            "kept",
            4,
        )
        assert section.tags == ["USER_FRUSTRATED"]

    def test_status_when_qa_did_not_produce_results(self):
        assert extract_analysis({}).status == "not_run"
        assert extract_analysis(None).status == "not_run"

        skipped = extract_analysis(
            {"qa_5": {"skipped": True, "reason": "call too short"}}
        )
        assert (skipped.status, skipped.skipped_reason) == ("skipped", "call too short")

        assert extract_analysis({"qa_5": {"error": "boom"}}).status == "error"


class TestDisconnect:
    def test_known_reasons(self):
        assert classify_disconnect("user_hangup")[0] == "customer_hung_up"
        assert classify_disconnect("user_qualified")[0] == "agent_completed"
        assert classify_disconnect("pipeline_error")[0] == "system_error"
        assert classify_disconnect("failed")[0] == "not_connected"

    def test_blank_is_not_finished_and_free_text_is_other(self):
        assert classify_disconnect(None) == ("not_finished", "Not finished")
        assert classify_disconnect("customer wants callback") == (
            "other",
            "customer wants callback",
        )


def _snapshot(**overrides):
    base = dict(
        run_id=413,
        workflow_id=7,
        organization_id=5,
        created_at=datetime(2026, 9, 17, 9, 34, 18, tzinfo=UTC),
        mode="voicelink",
        call_type="inbound",
        is_completed=True,
        initial_context={
            "caller_number": "+911234567890",
            "called_number": "+910000000000",
        },
        gathered_context={"mapped_call_disposition": "user_hangup"},
        usage_info={"call_duration_seconds": 214},
        annotations=_annotations(_NODE_1, _NODE_2),
        events=[
            _end(
                CREATE,
                "c1",
                _create_result("7000002403"),
                "2026-09-17T09:37:52.783+00:00",
            )
        ],
    )
    base.update(overrides)
    return RunSnapshot(**base)


class TestBuildCallReport:
    def test_run_413_end_to_end(self):
        report = build_call_report(_snapshot())

        assert report.call.call_type == "inbound"
        assert report.call.phone_number == "+911234567890"
        assert report.call.duration_seconds == 214
        assert report.call.is_telephony is True
        assert report.disconnect.category == "customer_hung_up"
        assert report.ticket.created and not report.ticket.closed
        assert report.ticket.tickets[0].ticket_id == "7000002403"
        assert report.outcome.code == "ticket_open_transferred"
        assert report.outcome.successful is True
        assert report.analysis.reason_for_call == "meter_balance"

    def test_ticket_closed_outcome(self):
        events = [
            _end(
                CREATE, "c1", _create_result("7000002402"), "2026-09-17T08:59:54+00:00"
            ),
            _end(CLOSE, "c2", CLOSE_RESULT, "2026-09-17T09:01:18+00:00"),
        ]

        report = build_call_report(_snapshot(events=events))

        assert report.ticket.closed is True
        assert report.outcome.code == "ticket_closed"

    def test_outbound_uses_dialled_number(self):
        report = build_call_report(
            _snapshot(
                call_type="outbound",
                initial_context={
                    "phone_number": "+919999999999",
                    "called_number": "+911111111111",
                },
            )
        )

        assert report.call.phone_number == "+919999999999"

    def test_browser_test_call_is_not_telephony(self):
        assert (
            build_call_report(_snapshot(mode="smallwebrtc")).call.is_telephony is False
        )

    def test_short_or_failed_calls_are_not_successful(self):
        assert (
            build_call_report(
                _snapshot(usage_info={"call_duration_seconds": 5})
            ).outcome.successful
            is False
        )
        assert (
            build_call_report(
                _snapshot(
                    gathered_context={"mapped_call_disposition": "pipeline_error"}
                )
            ).outcome.successful
            is False
        )
        assert (
            build_call_report(_snapshot(is_completed=False)).outcome.successful is False
        )

    def test_bare_run_produces_a_report(self):
        report = build_call_report(
            _snapshot(
                annotations={},
                events=[],
                gathered_context={},
                usage_info={},
                initial_context={},
            )
        )

        assert report.analysis.status == "not_run"
        assert report.disconnect.category == "not_finished"
        assert report.outcome.code == "no_outcome"
        assert report.call.duration_seconds is None
        assert report.call.phone_number is None


class TestAgentCapabilities:
    def test_tool_names_are_sanitised_the_way_the_model_sees_them(self):
        assert tool_function_name("Create Complaint2") == "create_complaint2"
        assert tool_function_name("closed-ticket!") == "closed_ticket"
        assert tool_function_name("  __get  csrf__token ") == "get_csrf_token"

    def test_ticket_tool_is_recognised_by_its_call_name(self):
        assert has_ticket_tool({"query_taluka_tg", "create_complaint2"}) is True
        assert has_ticket_tool({"query_taluka_tg", "send_template_message"}) is False
        assert has_ticket_tool(set()) is False

    def test_qa_node_counts_unless_it_is_switched_off(self):
        on = {"nodes": [{"type": "qa", "data": {"qa_enabled": True}}]}
        default_on = {"nodes": [{"type": "qa", "data": {"name": "QA"}}]}
        off = {"nodes": [{"type": "qa", "data": {"qa_enabled": False}}]}
        none = {"nodes": [{"type": "startCall", "data": {}}]}

        assert qa_enabled_in_definition(on) is True
        assert qa_enabled_in_definition(default_on) is True
        assert qa_enabled_in_definition(off) is False
        assert qa_enabled_in_definition(none) is False
        assert qa_enabled_in_definition(None) is False

    def test_tool_uuids_are_collected_from_every_node(self):
        definition = {
            "nodes": [
                {"type": "startCall", "data": {"tool_uuids": ["a", "b"]}},
                {"type": "agentNode", "data": {"tool_uuids": ["b", "c"]}},
                {"type": "endCall", "data": {}},
                {"type": "qa", "data": None},
            ]
        }

        assert tool_uuids_in_definition(definition) == {"a", "b", "c"}
        assert tool_uuids_in_definition(None) == set()

    def test_an_agent_with_neither_ticket_tool_nor_qa_gets_neither_section(self):
        report = build_call_report(_snapshot(annotations={}, events=[]))

        assert report.capabilities.ticket is False
        assert report.capabilities.analysis is False

    def test_the_agents_own_setup_makes_sections_apply_before_they_have_data(self):
        report = build_call_report(
            _snapshot(
                annotations={},
                events=[],
                agent_tool_names=frozenset({"create_complaint2"}),
                qa_enabled=True,
            )
        )

        assert report.capabilities.ticket is True
        assert report.capabilities.analysis is True

    def test_data_that_exists_proves_the_capability(self):
        # Nothing known about the agent, but this call raised a ticket and was analysed.
        report = build_call_report(_snapshot())

        assert report.capabilities.ticket is True
        assert report.capabilities.analysis is True


class TestCapturedData:
    def test_extracted_variables_are_captured_and_empty_ones_dropped(self):
        report = build_call_report(
            _snapshot(
                gathered_context={
                    "mapped_call_disposition": "user_hangup",
                    "extracted_variables": {
                        "interested_item": "water tank",
                        "product_category": "Water Tank",
                        "conversation_language": "hi",
                        "budget": None,
                        "notes": "   ",
                        "empty_list": [],
                        "follow_up": False,
                        "quantity": 0,
                    },
                }
            )
        )

        assert report.captured == {
            "interested_item": "water tank",
            "product_category": "Water Tank",
            "conversation_language": "hi",
            "follow_up": False,
            "quantity": 0,
        }

    def test_only_extracted_variables_are_captured_not_the_whole_context(self):
        report = build_call_report(
            _snapshot(
                gathered_context={
                    "mapped_call_disposition": "user_hangup",
                    "customer_phone_number": "+911234567890",
                    "call_tags": ["x"],
                }
            )
        )

        assert report.captured == {}

    def test_long_values_and_huge_maps_are_bounded(self):
        many = {f"var_{i}": "x" * 2000 for i in range(200)}

        captured = build_call_report(
            _snapshot(gathered_context={"extracted_variables": many})
        ).captured

        assert len(captured) == 50
        assert all(len(value) == 500 for value in captured.values())

    def test_malformed_extracted_variables_are_ignored(self):
        assert (
            build_call_report(
                _snapshot(gathered_context={"extracted_variables": "not a dict"})
            ).captured
            == {}
        )
