"""Keeping Scout's transcript inside the model's limits.

Scout persists its full message history and replays it on every turn
(`session_service.py` writes `messages` after each step and reloads it on the
next). That makes transcript size a *durability* problem, not just a
per-request one: one oversized message doesn't fail a turn, it kills the
thread for good, because every later turn replays the same history and fails
identically. This is the same failure shape as an unanswered `tool_call_id`
(see `_answer_dangling_tool_calls`), and it has the same answer — enforce the
invariant on load, not just on write, so a thread saved broken by an earlier
version heals itself the next time it runs.

Two limits are in play and they are not the same thing:

* A **per-string** cap the provider enforces on any single message's content
  (10,485,760 characters). A tool that returns a customer's raw HTTP response
  can blow this on its own — that is what `MAX_TOOL_RESULT_CHARS` prevents.
* The **context window**, which no single message has to exceed: forty
  medium results get there just as well. That is what `MAX_TRANSCRIPT_CHARS`
  and whole-turn trimming prevent.

On truncation and quality: cutting bytes off the end of a JSON blob is the
worst thing to do. The model is left holding `{"items":[{"id":1,"na` and
either invents the rest or reasons over a fragment believing it is complete.
So we shrink *structurally* — keep the shape, sample the values, and say
plainly what was removed and how big it was. The model almost always needs to
know the response looks like `{items: [...], next_cursor}`, not to read forty
thousand rows.
"""

import json
from dataclasses import dataclass
from typing import Any

# Per tool result. Roughly 6k tokens — comfortably enough for a schema plus a
# sample of any realistic API response, and small enough that a long thread of
# them still fits the window.
MAX_TOOL_RESULT_CHARS = 24_000

# Whole transcript, excluding the system prompt. ~70k tokens, leaving room for
# the tool schemas and the response inside a 128k window.
MAX_TRANSCRIPT_CHARS = 280_000

# The provider's hard per-string limit. Never a target — a message anywhere
# near this has already gone wrong — but worth checking before a request so
# the failure names itself instead of arriving as an opaque 400.
PROVIDER_MAX_CONTENT_CHARS = 10_485_760

# Progressively harsher (string cap, list sample, dict keys) settings. The
# first profile that fits the budget wins, so a result only loses as much
# detail as it actually has to.
_SHRINK_PROFILES: tuple[tuple[int, int, int], ...] = (
    (2_000, 5, 60),
    (600, 3, 30),
    (200, 2, 15),
    (80, 1, 8),
)

_TRIM_NOTE_PREFIX = "[Earlier turns in this conversation were dropped"


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _shrink(value: Any, *, str_cap: int, list_sample: int, dict_keys: int) -> Any:
    """Return a structurally similar value with the bulk removed.

    Shape is preserved deliberately: a dict stays a dict and a list stays a
    list, so the model can still see what kind of thing came back. Every
    removal is stated inline rather than left silent.
    """
    if isinstance(value, str):
        if len(value) <= str_cap:
            return value
        return f"{value[:str_cap]}… [truncated, {len(value)} chars total]"

    if isinstance(value, dict):
        items = list(value.items())
        shrunk = {
            key: _shrink(
                item, str_cap=str_cap, list_sample=list_sample, dict_keys=dict_keys
            )
            for key, item in items[:dict_keys]
        }
        if len(items) > dict_keys:
            shrunk["_omitted_keys"] = [str(k) for k, _ in items[dict_keys:]][:20]
            shrunk["_omitted_key_count"] = len(items) - dict_keys
        return shrunk

    if isinstance(value, (list, tuple)):
        shrunk_list = [
            _shrink(item, str_cap=str_cap, list_sample=list_sample, dict_keys=dict_keys)
            for item in list(value)[:list_sample]
        ]
        if len(value) > list_sample:
            shrunk_list.append(
                f"… {len(value) - list_sample} more items omitted "
                f"(list length {len(value)})"
            )
        return shrunk_list

    return value


def compact_tool_result(payload: Any, *, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Serialize a tool result, shrinking it if it's too big to keep.

    Returns JSON text ready to be a message's `content`. A result that fits is
    returned untouched — the common case pays nothing.
    """
    serialized = _dumps(payload)
    if len(serialized) <= max_chars:
        return serialized

    original_chars = len(serialized)
    for str_cap, list_sample, dict_keys in _SHRINK_PROFILES:
        candidate = _dumps(
            {
                "_truncated": {
                    "reason": "The full result was too large to keep in this conversation.",
                    "original_chars": original_chars,
                    "showing": "the result's structure with sampled values",
                    "advice": (
                        "Values and list entries shown are examples, not the complete "
                        "data. If you need specifics that were omitted, ask for a "
                        "narrower query rather than assuming what's missing."
                    ),
                },
                "result": _shrink(
                    payload,
                    str_cap=str_cap,
                    list_sample=list_sample,
                    dict_keys=dict_keys,
                ),
            }
        )
        if len(candidate) <= max_chars:
            return candidate

    # Nothing structural fit — a single enormous scalar, most likely. Fall
    # back to a prefix, still labelled so the model knows it's partial.
    header = {
        "_truncated": {
            "reason": "The full result was too large to keep in this conversation.",
            "original_chars": original_chars,
            "showing": "a prefix of the raw serialized result",
            "advice": "Treat this as an incomplete fragment; ask for a narrower query.",
        },
        "result_prefix": "",
    }
    room = max_chars - len(_dumps(header)) - 16
    header["result_prefix"] = serialized[: max(room, 0)]
    return _dumps(header)


def _recompact_content(content: str, max_chars: int) -> str:
    """Shrink an already-serialized message content string."""
    try:
        return compact_tool_result(json.loads(content), max_chars=max_chars)
    except (json.JSONDecodeError, ValueError):
        return compact_tool_result(content, max_chars=max_chars)


def compact_oversized_messages(
    messages: list[dict[str, Any]], *, max_chars: int = MAX_TOOL_RESULT_CHARS
) -> int:
    """Shrink any message content already over the cap. Returns how many.

    This is what heals a thread poisoned by an earlier version: the giant
    payload is rewritten in place before the transcript is sent anywhere, and
    the compacted form is what gets persisted back.
    """
    repaired = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= max_chars:
            continue
        message["content"] = _recompact_content(content, max_chars)
        repaired += 1
    return repaired


def drop_orphan_tool_messages(messages: list[dict[str, Any]]) -> int:
    """Remove `tool` replies whose originating tool_call is no longer present.

    The mirror image of `_answer_dangling_tool_calls`, and rejected by the API
    just as firmly. Trimming whole turns shouldn't produce one, but a
    transcript edited by any other means can, so it's checked rather than
    assumed.
    """
    known_ids: set[str] = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if call_id:
                known_ids.add(call_id)

    kept = [
        message
        for message in messages
        if message.get("role") != "tool" or message.get("tool_call_id") in known_ids
    ]
    removed = len(messages) - len(kept)
    messages[:] = kept
    return removed


def _turn_boundaries(body: list[dict[str, Any]]) -> list[int]:
    """Indices where a turn starts — each user message, plus any leading
    fragment that precedes the first one."""
    starts = [i for i, m in enumerate(body) if m.get("role") == "user"]
    if not starts:
        return []
    if starts[0] != 0:
        starts.insert(0, 0)
    return starts


def trim_to_budget(
    messages: list[dict[str, Any]], *, max_chars: int = MAX_TRANSCRIPT_CHARS
) -> int:
    """Drop whole oldest turns until the transcript fits. Returns turns dropped.

    Whole turns, never individual messages. Cutting mid-turn is how you strand
    an assistant `tool_calls` from its `tool` reply (or the reverse), which is
    the 400 this module exists to prevent — so the unit of removal is the only
    boundary at which that can't happen.

    The most recent turn is never dropped, however big it is; a single
    oversized turn is the per-result cap's problem, not this function's.
    """
    prefix = 1 if messages and messages[0].get("role") == "system" else 0
    body = messages[prefix:]
    sizes = [len(_dumps(m)) for m in body]
    if sum(sizes) <= max_chars:
        return 0

    # A note from a previous trim shouldn't accumulate or count toward budget.
    body = [
        m
        for m in body
        if not (
            m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(_TRIM_NOTE_PREFIX)
        )
    ]
    sizes = [len(_dumps(m)) for m in body]

    starts = _turn_boundaries(body)
    if len(starts) < 2:
        # One turn or no user message at all — nothing safe to drop.
        messages[:] = messages[:prefix] + body
        return 0

    # Sizes are measured once and decremented as turns fall away; re-summing
    # the whole body per dropped turn is quadratic on long threads.
    remaining = sum(sizes)
    dropped = 0
    while len(starts) > 1 and remaining > max_chars:
        cut = starts[1]
        remaining -= sum(sizes[:cut])
        body = body[cut:]
        sizes = sizes[cut:]
        starts = [s - cut for s in starts[1:]]
        dropped += 1

    if dropped:
        body.insert(
            0,
            {
                "role": "system",
                "content": (
                    f"{_TRIM_NOTE_PREFIX} to stay within the model's context limit — "
                    f"{dropped} earlier turn(s) omitted. Recent history is intact. If "
                    "the user refers to something you can't see, say so and ask rather "
                    "than guessing what was discussed."
                ),
            },
        )

    messages[:] = messages[:prefix] + body
    return dropped


@dataclass
class TranscriptRepair:
    compacted_messages: int = 0
    removed_orphans: int = 0
    dropped_turns: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.compacted_messages or self.removed_orphans or self.dropped_turns)

    def describe(self) -> str:
        return (
            f"compacted={self.compacted_messages} "
            f"orphans_removed={self.removed_orphans} "
            f"turns_dropped={self.dropped_turns}"
        )


def sanitize_transcript(
    messages: list[dict[str, Any]],
    *,
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
    max_transcript_chars: int = MAX_TRANSCRIPT_CHARS,
) -> TranscriptRepair:
    """Make a transcript safe to send, in place.

    Order matters. Compact first, so trimming measures the sizes we'll
    actually send rather than the bloated ones — otherwise a single giant
    message would cause turns to be dropped that didn't need to be. Trim next.
    Remove orphans last, so anything the trim stranded is caught.
    """
    report = TranscriptRepair()
    report.compacted_messages = compact_oversized_messages(
        messages, max_chars=max_tool_result_chars
    )
    report.dropped_turns = trim_to_budget(messages, max_chars=max_transcript_chars)
    report.removed_orphans = drop_orphan_tool_messages(messages)
    return report
