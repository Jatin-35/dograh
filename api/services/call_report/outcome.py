"""Derive one business outcome per call from signals that are already reliable.

Nothing here asks an LLM to pick a category: the outcome follows from whether a
ticket exists and is closed, whether the call went to a human, whether QA judged
the issue resolved, and how the call ended.
"""

from typing import Optional

from api.services.call_report.disconnect import FAILURE_CATEGORIES, NOT_FINISHED
from api.services.call_report.schema import AnalysisSection, TicketSection

# code -> label. Kept here so a dashboard and the run page never disagree.
OUTCOME_LABELS: dict[str, str] = {
    "ticket_closed": "Ticket created and closed",
    "ticket_open_transferred": "Ticket open, transferred to a person",
    "ticket_open": "Ticket created, still open",
    "transferred": "Transferred to a person",
    "resolved": "Resolved",
    "not_resolved": "Not resolved",
    "failed": "Call failed",
    "no_outcome": "No outcome recorded",
}


def derive_outcome(
    ticket: TicketSection,
    analysis: AnalysisSection,
    disconnect_category: str,
) -> tuple[str, str]:
    """Return ``(code, label)``."""
    transferred = bool(analysis.human_transfer) or disconnect_category == "transferred"

    if ticket.created:
        if ticket.closed:
            code = "ticket_closed"
        elif transferred:
            code = "ticket_open_transferred"
        else:
            code = "ticket_open"
    elif transferred:
        code = "transferred"
    elif analysis.resolved is True:
        code = "resolved"
    elif (
        disconnect_category in FAILURE_CATEGORIES
        and disconnect_category != NOT_FINISHED
    ):
        code = "failed"
    elif analysis.resolved is False:
        code = "not_resolved"
    else:
        code = "no_outcome"

    return code, OUTCOME_LABELS[code]


def is_successful_call(
    *,
    is_completed: bool,
    duration_seconds: Optional[float],
    disconnect_category: str,
    min_seconds: float,
) -> bool:
    """A "completed" call: it finished, ran long enough, and did not fail."""
    return (
        bool(is_completed)
        and (duration_seconds or 0) >= min_seconds
        and disconnect_category not in FAILURE_CATEGORIES
    )
