"""Turn the stored disconnect reason into a stable category plus a display label.

The raw value is whatever ended the call: an engine reason (``user_hangup``,
``user_qualified``, ``pipeline_error`` …) or, for calls that never connected, a
telephony status (``failed``, ``busy`` …). Categories are what dashboards group
by; the raw value stays on the report for anyone who needs it.
"""

from typing import Optional

NOT_FINISHED = "not_finished"
OTHER = "other"

# raw reason -> (category, label)
_KNOWN: dict[str, tuple[str, str]] = {
    "user_hangup": ("customer_hung_up", "Customer hung up"),
    "user_qualified": ("agent_completed", "Agent completed the flow"),
    "end_call_tool": ("agent_ended", "Agent ended the call"),
    "user_disqualified": ("agent_ended", "Agent ended the call"),
    "user_idle_max_duration_exceeded": ("silence_timeout", "Silence timeout"),
    "call_duration_exceeded": ("max_duration", "Maximum duration reached"),
    "call_transferred": ("transferred", "Transferred"),
    "transfer_call": ("transferred", "Transferred"),
    "voicemail_detected": ("voicemail", "Voicemail"),
    "pipeline_error": ("system_error", "System error"),
    "unexpected_error": ("system_error", "System error"),
    "system_connect_error": ("system_error", "System error"),
    "system_cancelled": ("system_cancelled", "Cancelled by the system"),
    # Telephony statuses for calls that never connected.
    "failed": ("not_connected", "Call did not connect"),
    "error": ("not_connected", "Call did not connect"),
    "busy": ("not_connected", "Line busy"),
    "no-answer": ("not_connected", "No answer"),
    "canceled": ("not_connected", "Call cancelled"),
}

# category -> label, for dashboards that group by category rather than raw reason.
CATEGORY_LABELS: dict[str, str] = {
    "customer_hung_up": "Customer hung up",
    "agent_completed": "Agent completed the flow",
    "agent_ended": "Agent ended the call",
    "silence_timeout": "Silence timeout",
    "max_duration": "Maximum duration reached",
    "transferred": "Transferred",
    "voicemail": "Voicemail",
    "system_error": "System error",
    "system_cancelled": "Cancelled by the system",
    "not_connected": "Call did not connect",
    OTHER: "Other",
    NOT_FINISHED: "Not finished",
}

# Categories that mean the call itself did not work — used to decide whether a
# finished call counts as a successful one.
FAILURE_CATEGORIES = frozenset({"system_error", "not_connected", NOT_FINISHED})


def classify_disconnect(reason: Optional[str]) -> tuple[str, str]:
    """Return ``(category, label)`` for a raw disconnect reason."""
    if not reason:
        return NOT_FINISHED, "Not finished"

    known = _KNOWN.get(str(reason).strip().lower())
    if known:
        return known

    # An agent-supplied free-text reason (end_call tool with a custom reason).
    return OTHER, str(reason).strip()
