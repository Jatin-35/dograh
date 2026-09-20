"""Shape aggregated call-report numbers into the dashboard's response.

Kept apart from the query (``CallReportClient.get_call_stats``) so the rules a
dashboard depends on — how a local date range becomes UTC bounds, which days and
hours are filled with zeros, how a rate is computed — are plain functions that
can be tested without a database.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.services.call_report.disconnect import CATEGORY_LABELS
from api.services.call_report.outcome import OUTCOME_LABELS

MAX_RANGE_DAYS = 366


class StatsRangeError(ValueError):
    """The requested date range or timezone cannot be served."""


def resolve_range(
    start_date: date, end_date: date, timezone: str
) -> tuple[datetime, datetime, ZoneInfo]:
    """Turn a local date range into inclusive UTC bounds.

    ``end_date`` is inclusive: it covers that whole local day, so "today" is
    ``start_date == end_date == today``.
    """
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise StatsRangeError(f"Unknown timezone '{timezone}'") from exc

    if end_date < start_date:
        raise StatsRangeError("end_date must not be before start_date")
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise StatsRangeError(f"The range cannot exceed {MAX_RANGE_DAYS} days")

    start = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(end_date, time.max, tzinfo=tz).astimezone(UTC)
    return start, end, tz


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def _items(rows: list[tuple[Any, int]], labels: Optional[dict[str, str]] = None):
    return [
        {
            "key": str(key),
            "label": (labels or {}).get(
                str(key), str(key).replace("_", " ").capitalize()
            ),
            "count": count,
        }
        for key, count in rows
    ]


def _satisfaction(rows: list[tuple[Any, int]]) -> list[dict]:
    """Yes / No / Unclear — a call QA could not judge is "unclear", not dropped."""
    names = {True: "yes", False: "no", None: "unclear"}
    counts = {"yes": 0, "no": 0, "unclear": 0}
    for value, count in rows:
        counts[names[value]] += count
    return [
        {"key": key, "label": key.capitalize(), "count": counts[key]}
        for key in ("yes", "no", "unclear")
    ]


def build_stats_response(
    raw: dict, *, start_date: date, end_date: date, timezone: str
) -> dict:
    t = raw["totals"]
    total = t["total"]
    days = {row_date: (calls, ok) for row_date, calls, ok in raw["daily"]}
    hours = dict(raw["hourly"])

    daily = []
    cursor = start_date
    while cursor <= end_date:
        calls, successful = days.get(cursor, (0, 0))
        daily.append(
            {"date": cursor.isoformat(), "calls": calls, "successful": successful}
        )
        cursor += timedelta(days=1)

    avg = t["avg_duration"]
    return {
        "range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "timezone": timezone,
        },
        "kpis": {
            "total_calls": total,
            "inbound_calls": t["inbound"],
            "outbound_calls": t["outbound"],
            "successful_calls": t["successful"],
            "failed_calls": t["failed"],
            "success_rate": _rate(t["successful"], total),
            "avg_duration_seconds": round(float(avg), 1) if avg is not None else None,
            "total_duration_seconds": round(float(t["total_duration"]), 1),
            "calls_with_ticket": t["with_ticket"],
            "tickets_created": int(t["tickets_created"]),
            "calls_with_closed_ticket": t["ticket_closed"],
            "calls_with_open_ticket": t["with_ticket"] - t["ticket_closed"],
            "analysed_calls": t["analysed"],
        },
        # Which parts of the dashboard can ever have data for the agents in scope.
        "capabilities": {
            "tickets": bool(t["ticket_capable"]),
            "analysis": bool(t["analysis_capable"]),
        },
        "outcomes": _items(raw["outcome"], OUTCOME_LABELS),
        "disconnections": _items(raw["disconnect"], CATEGORY_LABELS),
        "sentiment": _items(raw["sentiment"]),
        "satisfaction": _satisfaction(raw["satisfaction"]),
        "reasons": _items(raw["reason"]),
        "daily": daily,
        "hourly": [{"hour": h, "calls": hours.get(h, 0)} for h in range(24)],
    }
