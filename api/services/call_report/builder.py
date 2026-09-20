"""Build a ``CallReport`` from a run's stored data.

``build_call_report`` is a pure function of a ``RunSnapshot`` — no database, no
network — so a report can be rebuilt any time from what the run already holds
(for instance after QA finishes late), and is cheap to test exhaustively.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

from api.services.call_report.analysis import extract_analysis
from api.services.call_report.capabilities import has_ticket_tool
from api.services.call_report.disconnect import classify_disconnect
from api.services.call_report.outcome import derive_outcome, is_successful_call
from api.services.call_report.sanitize import json_safe
from api.services.call_report.schema import (
    CallReport,
    CallSection,
    CapabilitiesSection,
    DisconnectSection,
    OutcomeSection,
    TicketSection,
)
from api.services.call_report.tickets import extract_tickets

# A finished call shorter than this is not counted as a successful one — a caller
# who hangs up in the first seconds got no service. Matches the QA minimum the
# agents are configured with.
MIN_SUCCESS_SECONDS = 10

# Browser/test sessions, not real calls: dashboards leave these out.
NON_TELEPHONY_MODES = frozenset({"webrtc", "smallwebrtc", "textchat"})

# Bounds on what an agent's extracted variables may put into a report.
MAX_CAPTURED_KEYS = 50
MAX_CAPTURED_TEXT = 500


@dataclass
class RunSnapshot:
    run_id: int
    workflow_id: int
    organization_id: int
    created_at: Optional[datetime]
    mode: str
    call_type: Optional[str]
    is_completed: bool
    initial_context: dict = field(default_factory=dict)
    gathered_context: dict = field(default_factory=dict)
    usage_info: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    # What the agent is set up to do, read from its pinned definition (see
    # capabilities.py): the names of the tools it can call, and whether it has an
    # active QA node. Left empty, capabilities are inferred from the call's own data.
    agent_tool_names: frozenset = frozenset()
    qa_enabled: bool = False

    def __post_init__(self) -> None:
        # Everything here comes out of JSON columns and model output. A column that
        # is not the shape it should be must not stop a report from being built, so
        # each is coerced at this one boundary and the rest of the code can trust it.
        for name in (
            "initial_context",
            "gathered_context",
            "usage_info",
            "annotations",
        ):
            if not isinstance(getattr(self, name), dict):
                setattr(self, name, {})
        self.events = (
            [event for event in self.events if isinstance(event, dict)]
            if isinstance(self.events, list)
            else []
        )
        if not isinstance(self.created_at, datetime):
            self.created_at = None
        if not isinstance(self.mode, str):
            self.mode = ""
        if self.call_type not in ("inbound", "outbound"):
            self.call_type = None

    @classmethod
    def from_run(
        cls,
        run: Any,
        organization_id: int,
        *,
        agent_tool_names: frozenset = frozenset(),
        qa_enabled: bool = False,
    ) -> "RunSnapshot":
        logs = run.logs if isinstance(run.logs, dict) else {}
        return cls(
            run_id=run.id,
            workflow_id=run.workflow_id,
            organization_id=organization_id,
            created_at=run.created_at,
            mode=run.mode or "",
            call_type=run.call_type,
            is_completed=bool(run.is_completed),
            initial_context=run.initial_context or {},
            gathered_context=run.gathered_context or {},
            usage_info=run.usage_info or {},
            annotations=run.annotations or {},
            events=logs.get("realtime_feedback_events") or [],
            agent_tool_names=agent_tool_names,
            qa_enabled=qa_enabled,
        )


def _duration_seconds(usage_info: dict) -> Optional[float]:
    raw = usage_info.get("call_duration_seconds")
    if isinstance(raw, bool):
        return None
    try:
        seconds = float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        return None
    # A duration that is not a real, non-negative number is no duration at all.
    return (
        seconds
        if seconds is not None and math.isfinite(seconds) and seconds >= 0
        else None
    )


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _phone_number(snapshot: RunSnapshot) -> Optional[str]:
    """The customer's number: the caller on inbound, the dialled number outbound."""
    initial = snapshot.initial_context
    gathered = snapshot.gathered_context
    if snapshot.call_type == "inbound":
        candidates = (
            initial.get("caller_number"),
            gathered.get("customer_phone_number"),
        )
    else:
        candidates = (
            initial.get("phone_number"),
            initial.get("called_number"),
            gathered.get("customer_phone_number"),
        )
    return next((c for c in map(_text_or_none, candidates) if c), None)


def _clean_captured(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value.strip()[:MAX_CAPTURED_TEXT]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth < 2 and isinstance(value, list):
        return [_clean_captured(item, depth + 1) for item in value[:20]]
    if depth < 2 and isinstance(value, dict):
        return {
            str(k): _clean_captured(v, depth + 1) for k, v in list(value.items())[:20]
        }
    return str(value)[:MAX_CAPTURED_TEXT]


def captured_data(gathered_context: dict) -> dict[str, Any]:
    """The variables the agent extracted during the call, minus empty ones."""
    extracted = gathered_context.get("extracted_variables")
    if not isinstance(extracted, dict):
        return {}
    captured: dict[str, Any] = {}
    for key, value in extracted.items():
        cleaned = _clean_captured(value)
        if cleaned in (None, "", [], {}):
            continue
        captured[str(key)] = cleaned
        if len(captured) >= MAX_CAPTURED_KEYS:
            break
    return captured


def build_call_report(snapshot: RunSnapshot) -> CallReport:
    started_at = snapshot.created_at or datetime.now(UTC)
    duration = _duration_seconds(snapshot.usage_info)

    reason = _text_or_none(snapshot.gathered_context.get("mapped_call_disposition"))
    category, label = classify_disconnect(reason)

    tickets = extract_tickets(snapshot.events)
    ticket = TicketSection(
        created=bool(tickets),
        closed=bool(tickets) and all(t.closed for t in tickets),
        count=len(tickets),
        tickets=tickets,
    )

    analysis = extract_analysis(snapshot.annotations)
    outcome_code, outcome_label = derive_outcome(ticket, analysis, category)

    report = CallReport(
        run_id=snapshot.run_id,
        workflow_id=snapshot.workflow_id,
        organization_id=snapshot.organization_id,
        call=CallSection(
            call_type=snapshot.call_type,
            mode=snapshot.mode,
            is_telephony=snapshot.mode not in NON_TELEPHONY_MODES,
            started_at=started_at.isoformat(),
            duration_seconds=duration,
            phone_number=_phone_number(snapshot),
        ),
        disconnect=DisconnectSection(reason=reason, category=category, label=label),
        outcome=OutcomeSection(
            code=outcome_code,
            label=outcome_label,
            successful=is_successful_call(
                is_completed=snapshot.is_completed,
                duration_seconds=duration,
                disconnect_category=category,
                min_seconds=MIN_SUCCESS_SECONDS,
            ),
        ),
        ticket=ticket,
        analysis=analysis,
        capabilities=CapabilitiesSection(
            # A ticket that exists proves the agent can raise them, whatever its
            # definition says now; the same for an analysis that ran.
            ticket=bool(tickets) or has_ticket_tool(snapshot.agent_tool_names),
            analysis=snapshot.qa_enabled or analysis.status != "not_run",
        ),
        captured=captured_data(snapshot.gathered_context),
    )
    # Last line of defence: nothing that cannot be stored or sent as JSON leaves here.
    return CallReport.model_validate(json_safe(report.model_dump(mode="json")))
