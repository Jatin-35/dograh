"""Per-turn latency breakdown as a run-log event.

Our pipecat's ``UserBotLatencyObserver`` already measures, for each interval
from the caller going quiet to the bot starting to speak, how long the turn
decision took and each service's time to first byte. Only the total was saved
(``rtf-latency-measured``); this keeps the parts too, so a slow turn shows
whether STT, turn detection, the LLM, TTS or a tool was responsible.
"""

from __future__ import annotations

from typing import Any

LATENCY_BREAKDOWN_EVENT = "rtf-latency-breakdown"


def _secs(value) -> float | None:
    return None if value is None else round(float(value), 3)


def build_latency_breakdown_event(breakdown) -> dict[str, Any]:
    """The logs-buffer event for one ``LatencyBreakdown``."""
    text = breakdown.text_aggregation
    return {
        "type": LATENCY_BREAKDOWN_EVENT,
        "payload": {
            # Caller actually quiet -> turn released (VAD silence, STT
            # finalization and any turn analyzer wait).
            "user_turn_secs": _secs(breakdown.user_turn_secs),
            "ttfb": [
                {"processor": t.processor, "model": t.model, "secs": _secs(t.duration_secs)}
                for t in breakdown.ttfb
            ],
            "text_aggregation_secs": _secs(text.duration_secs) if text else None,
            "function_calls": [
                {"name": f.function_name, "secs": _secs(f.duration_secs)}
                for f in breakdown.function_calls
            ],
        },
    }
