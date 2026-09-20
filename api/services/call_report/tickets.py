"""Ticket facts read straight from tool results — no LLM involved.

A QA reviewer only ever sees a tool's *name* in the transcript, never its result,
so it cannot report a ticket ID or when it was created. The call events do keep
the full result of every tool call and stamp it with a time, so the ticket ID and
its creation/closing times are recovered exactly from there.

Which tools count as "create" or "close" a ticket is configuration, not code
paths: add a ``TicketSource`` to ``TICKET_SOURCES`` for a new ticket system.
"""

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from pipecat.utils.enums import RealtimeFeedbackType

from api.services.call_report.schema import TicketInfo


@dataclass(frozen=True)
class TicketSource:
    name: str
    create_tools: frozenset[str]
    close_tools: frozenset[str]
    # Keys that hold the ticket ID somewhere inside a create tool's result.
    id_keys: tuple[str, ...] = ("ExObjectId", "ticket_id", "object_id")


THINK_GAS_SAP = TicketSource(
    name="think_gas_sap",
    create_tools=frozenset({"create_complaint2", "create_complaint_with_fresh_csrf"}),
    close_tools=frozenset({"closed_ticket"}),
)

TICKET_SOURCES: tuple[TicketSource, ...] = (THINK_GAS_SAP,)

# A real ticket reference is short; anything longer is a pattern that matched noise.
MAX_TICKET_ID_LENGTH = 100

_OK_STATUSES = frozenset({"success", "ok", "completed", "created"})

# Regex fallbacks for a result that is not parseable (e.g. truncated). The value
# must be quoted or bare but non-empty, so an empty ``'ExObjectId': ''`` never
# runs on into the next key.
_ID_KEY_PATTERN = r"{key}['\"]?\s*[:=]\s*['\"]?([0-9A-Za-z-]+)"
_SR_PATTERN = re.compile(r"\bSR:\s*(\d+)")
_HTTP_STATUS_PATTERN = re.compile(r"http_status['\"]?\s*[:=]\s*(\d{3})")


def _parse_result(text: Any) -> Any:
    """Parse a stored tool result.

    Results are stored with ``str(result)``, so a dict comes back as a Python
    repr (single quotes) rather than JSON; try both.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(stripped)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            continue
    return None


def _find_first(obj: Any, keys: Iterable[str]) -> Optional[str]:
    """Depth-first search for the first non-empty value under any of ``keys``."""
    wanted = tuple(keys)
    if isinstance(obj, dict):
        for key in wanted:
            value = obj.get(key)
            if (
                isinstance(value, (str, int))
                and not isinstance(value, bool)
                and str(value).strip()
            ):
                return str(value).strip()
        for value in obj.values():
            found = _find_first(value, wanted)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_first(value, wanted)
            if found:
                return found
    return None


def _extract_ticket_id(text: Any, obj: Any, source: TicketSource) -> Optional[str]:
    found = _find_first(obj, source.id_keys)
    if found:
        return found

    if isinstance(text, str):
        for key in source.id_keys:
            match = re.search(_ID_KEY_PATTERN.format(key=re.escape(key)), text)
            if match:
                return match.group(1)
        match = _SR_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def _is_success(text: Any, obj: Any) -> bool:
    """True when the tool result reports success and no failing status anywhere."""
    if isinstance(obj, dict):
        levels = [obj]
        inner = obj.get("data")
        if isinstance(inner, dict):
            levels.append(inner)

        positive = False
        for level in levels:
            status = str(level.get("status", "")).strip().lower()
            if status:
                if status not in _OK_STATUSES:
                    return False
                positive = True
            for code_key in ("status_code", "http_status"):
                code = level.get(code_key)
                if isinstance(code, int):
                    if not 200 <= code < 300:
                        return False
                    positive = True
        return positive

    if isinstance(text, str):
        match = _HTTP_STATUS_PATTERN.search(text)
        if match:
            return 200 <= int(match.group(1)) < 300
    return False


def _event_time(event: dict) -> Optional[str]:
    payload = event.get("payload")
    candidates = (
        event.get("timestamp"),
        payload.get("timestamp") if isinstance(payload, dict) else None,
    )
    return next((c for c in candidates if isinstance(c, str) and c), None)


def extract_tickets(
    events: Iterable[dict], sources: Iterable[TicketSource] = TICKET_SOURCES
) -> list[TicketInfo]:
    """Recover every ticket created (and closed) during the call, in order."""
    # Call events are stored JSON: skip anything that is not the shape it should be.
    events = [event for event in events if isinstance(event, dict)]
    sources = tuple(sources)

    # Arguments live on the call-start event; the result on the call-end event.
    arguments_by_call: dict[str, Any] = {}
    for event in events:
        if event.get("type") == RealtimeFeedbackType.FUNCTION_CALL_START.value:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            call_id = payload.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                arguments_by_call[call_id] = payload.get("arguments")

    tickets: list[TicketInfo] = []

    # Only a ticket tool's result is ever parsed: other tools (a knowledge-base
    # lookup can return a large payload) are skipped before any parsing happens.
    watched: set[str] = set()
    for source in sources:
        watched |= source.create_tools | source.close_tools

    for event in events:
        if event.get("type") != RealtimeFeedbackType.FUNCTION_CALL_END.value:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        tool = payload.get("function_name")
        result_text = payload.get("result")
        if not isinstance(tool, str) or tool not in watched or result_text is None:
            continue

        result = _parse_result(result_text)

        for source in sources:
            if tool in source.create_tools:
                if not _is_success(result_text, result):
                    continue
                ticket_id = _extract_ticket_id(result_text, result, source)
                if (
                    ticket_id
                    and len(ticket_id) <= MAX_TICKET_ID_LENGTH
                    and all(t.ticket_id != ticket_id for t in tickets)
                ):
                    tickets.append(
                        TicketInfo(
                            ticket_id=ticket_id,
                            created_at=_event_time(event),
                            source=source.name,
                        )
                    )
            elif tool in source.close_tools:
                if not _is_success(result_text, result):
                    continue
                _mark_closed(
                    tickets,
                    source.name,
                    closed_at=_event_time(event),
                    arguments=(
                        arguments_by_call.get(payload["tool_call_id"])
                        if isinstance(payload.get("tool_call_id"), str)
                        else None
                    ),
                    result_text=str(result_text),
                )

    return tickets


def _mentions(haystack: str, ticket_id: str) -> bool:
    """True if ``ticket_id`` appears as a whole token, not inside a longer number."""
    pattern = rf"(?<![0-9A-Za-z]){re.escape(ticket_id)}(?![0-9A-Za-z])"
    return re.search(pattern, haystack) is not None


def _mark_closed(
    tickets: list[TicketInfo],
    source_name: str,
    *,
    closed_at: Optional[str],
    arguments: Any,
    result_text: str,
) -> None:
    """Attach a closure to the ticket it names, else to the newest open one.

    If the close call carries a ticket ID (in its arguments or result) that is
    the ticket closed. Only when it names none does the closure fall back to the
    most recently created ticket that is still open — the flow closes what it just
    opened.
    """
    open_tickets = [t for t in tickets if t.source == source_name and not t.closed]
    if not open_tickets:
        return

    haystack = result_text
    if arguments is not None:
        try:
            haystack += " " + json.dumps(arguments, default=str)
        except (TypeError, ValueError):
            haystack += " " + str(arguments)

    named = [t for t in open_tickets if _mentions(haystack, t.ticket_id)]
    targets = named or [open_tickets[-1]]

    for ticket in targets:
        ticket.closed = True
        ticket.closed_at = closed_at
