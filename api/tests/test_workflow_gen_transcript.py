"""Transcript hygiene for Scout.

The failure these guard against isn't a bad turn — it's a dead thread. Scout
persists its messages and replays them every turn, so an oversized or
malformed transcript fails identically forever once written. Each test here
pins one half of that: don't write a bad transcript, and repair one you're
handed.
"""

import json

import pytest

from api.services.workflow_gen.transcript import (
    MAX_TOOL_RESULT_CHARS,
    PROVIDER_MAX_CONTENT_CHARS,
    compact_oversized_messages,
    compact_tool_result,
    drop_orphan_tool_messages,
    sanitize_transcript,
    trim_to_budget,
)


def _huge_api_response(rows: int = 40_000) -> dict:
    """Shaped like the real culprit: a tool handing back a customer's whole
    HTTP response body."""
    return {
        "status": "success",
        "status_code": 200,
        "data": {
            "items": [
                {"id": i, "name": f"Customer {i}", "notes": "x" * 200}
                for i in range(rows)
            ],
            "next_cursor": "abc123",
            "total": rows,
        },
    }


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def test_a_small_result_is_passed_through_untouched():
    payload = {"status": "success", "data": {"ok": True}}
    assert json.loads(compact_tool_result(payload)) == payload


def test_the_17mb_case_is_brought_under_the_cap():
    payload = _huge_api_response()
    assert len(json.dumps(payload)) > 5_000_000  # the problem, reproduced

    content = compact_tool_result(payload)

    assert len(content) <= MAX_TOOL_RESULT_CHARS


def test_truncation_is_announced_with_the_original_size():
    """A silent cut is worse than the error: the model reasons over a fragment
    believing it's the whole response."""
    payload = _huge_api_response()
    original = len(json.dumps(payload))

    compacted = json.loads(compact_tool_result(payload))

    assert compacted["_truncated"]["original_chars"] == original
    assert "showing" in compacted["_truncated"]
    assert "advice" in compacted["_truncated"]


def test_the_shape_survives_so_the_model_can_still_work():
    """Byte-slicing leaves `{"items":[{"id":1,"na` — useless. The model needs
    the schema and a sample far more than it needs all the rows."""
    compacted = json.loads(compact_tool_result(_huge_api_response()))
    result = compacted["result"]

    assert result["status"] == "success"
    assert result["status_code"] == 200
    assert "items" in result["data"]
    assert "next_cursor" in result["data"]
    # A real sample row, with real field names.
    first = result["data"]["items"][0]
    assert first["id"] == 0
    assert set(first) >= {"id", "name", "notes"}


def test_list_truncation_states_how_many_were_dropped():
    compacted = json.loads(compact_tool_result(_huge_api_response(rows=500)))
    items = compacted["result"]["data"]["items"]
    assert "more items omitted" in items[-1]
    assert "500" in items[-1]


def test_a_single_enormous_string_falls_back_to_a_labelled_prefix():
    """No structure to shrink — must still come back bounded and labelled,
    not blow the cap."""
    content = compact_tool_result({"blob": "y" * 2_000_000})

    assert len(content) <= MAX_TOOL_RESULT_CHARS
    assert "_truncated" in json.loads(content)


def test_deeply_nested_bulk_is_reached():
    payload = {"a": {"b": {"c": {"d": ["z" * 5_000 for _ in range(1_000)]}}}}
    assert len(compact_tool_result(payload)) <= MAX_TOOL_RESULT_CHARS


def test_non_serializable_values_do_not_raise():
    class Odd:
        def __repr__(self):
            return "odd"

    assert "odd" in compact_tool_result({"thing": Odd()})


# ---------------------------------------------------------------------------
# Repairing a transcript that was already saved poisoned
# ---------------------------------------------------------------------------


def test_an_already_poisoned_message_is_rewritten_in_place():
    """The thread that's dead right now. Healing on load is what revives it —
    without this it replays the same oversized history forever."""
    poisoned = json.dumps(_huge_api_response())
    messages = [
        {"role": "user", "content": "test the tool"},
        {"role": "tool", "tool_call_id": "call_1", "content": poisoned},
    ]

    repaired = compact_oversized_messages(messages)

    assert repaired == 1
    assert len(messages[1]["content"]) <= MAX_TOOL_RESULT_CHARS
    assert json.loads(messages[1]["content"])["_truncated"]["original_chars"] == len(poisoned)


def test_repair_leaves_healthy_messages_alone():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "tool_call_id": "c", "content": '{"ok": true}'},
    ]
    before = [dict(m) for m in messages]

    assert compact_oversized_messages(messages) == 0
    assert messages == before


def test_repair_handles_content_that_is_not_json():
    messages = [{"role": "tool", "tool_call_id": "c", "content": "q" * 100_000}]

    assert compact_oversized_messages(messages) == 1
    assert len(messages[0]["content"]) <= MAX_TOOL_RESULT_CHARS


# ---------------------------------------------------------------------------
# Orphans — the 400 that isn't about size
# ---------------------------------------------------------------------------


def test_a_tool_reply_without_its_call_is_removed():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "vanished", "content": "{}"},
    ]

    assert drop_orphan_tool_messages(messages) == 1
    assert all(m.get("role") != "tool" for m in messages)


def test_a_matched_pair_is_kept():
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "x"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ]

    assert drop_orphan_tool_messages(messages) == 0
    assert len(messages) == 2


# ---------------------------------------------------------------------------
# Budget trimming
# ---------------------------------------------------------------------------


def _turn(marker: str, size: int = 20_000) -> list[dict]:
    return [
        {"role": "user", "content": f"{marker} question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"call_{marker}", "function": {"name": "list_tools"}}],
        },
        {"role": "tool", "tool_call_id": f"call_{marker}", "content": "z" * size},
        {"role": "assistant", "content": f"{marker} answer"},
    ]


def test_a_transcript_within_budget_is_untouched():
    messages = [{"role": "system", "content": "sys"}, *_turn("a", size=100)]
    before = [dict(m) for m in messages]

    assert trim_to_budget(messages) == 0
    assert messages == before


def test_oldest_turns_are_dropped_until_it_fits():
    messages = [{"role": "system", "content": "sys"}, *[m for i in range(30) for m in _turn(str(i))]]

    dropped = trim_to_budget(messages)

    assert dropped > 0
    assert len(json.dumps(messages)) <= 400_000
    # The newest turn always survives.
    assert messages[-1]["content"] == "29 answer"


def test_the_system_prompt_is_never_dropped():
    messages = [{"role": "system", "content": "SYSTEM"}, *[m for i in range(30) for m in _turn(str(i))]]

    trim_to_budget(messages)

    assert messages[0]["content"] == "SYSTEM"


def test_trimming_never_strands_a_tool_call_from_its_reply():
    """Trimming by message count instead of whole turns is exactly how the
    orphan 400 gets reintroduced. Turns are the only safe cut point."""
    messages = [{"role": "system", "content": "sys"}, *[m for i in range(30) for m in _turn(str(i))]]

    trim_to_budget(messages)

    called = {
        c["id"]
        for m in messages
        if m.get("role") == "assistant"
        for c in m.get("tool_calls") or []
    }
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert answered == called


def test_the_model_is_told_that_history_was_dropped():
    messages = [{"role": "system", "content": "sys"}, *[m for i in range(30) for m in _turn(str(i))]]

    trim_to_budget(messages)

    note = next(m for m in messages[1:] if m.get("role") == "system")
    assert "earlier turn(s) omitted" in note["content"]
    assert "ask rather" in note["content"]


def test_the_trim_note_does_not_accumulate():
    messages = [{"role": "system", "content": "sys"}, *[m for i in range(30) for m in _turn(str(i))]]
    trim_to_budget(messages)
    messages.extend(_turn("later"))
    trim_to_budget(messages)

    notes = [
        m
        for m in messages[1:]
        if m.get("role") == "system" and "earlier turn(s) omitted" in (m.get("content") or "")
    ]
    assert len(notes) == 1


def test_a_single_oversized_turn_is_kept_rather_than_emptied():
    """Better a too-big single turn (which per-result caps handle) than a
    transcript trimmed to nothing."""
    messages = [{"role": "system", "content": "sys"}, *_turn("only", size=400_000)]

    trim_to_budget(messages)

    assert any(m.get("role") == "user" for m in messages)


# ---------------------------------------------------------------------------
# The whole pipeline
# ---------------------------------------------------------------------------


def test_sanitize_fixes_a_transcript_that_has_everything_wrong():
    messages = [
        {"role": "system", "content": "sys"},
        *[m for i in range(20) for m in _turn(str(i))],
        {"role": "user", "content": "now test it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_big", "function": {"name": "test_tool"}}],
        },
        {"role": "tool", "tool_call_id": "call_big", "content": json.dumps(_huge_api_response())},
        {"role": "tool", "tool_call_id": "ghost", "content": "{}"},
    ]

    report = sanitize_transcript(messages)

    assert report.compacted_messages == 1
    assert report.removed_orphans == 1
    assert report.changed
    assert all(
        len(m.get("content") or "") <= MAX_TOOL_RESULT_CHARS
        for m in messages
        if isinstance(m.get("content"), str)
    )
    called = {
        c["id"]
        for m in messages
        if m.get("role") == "assistant"
        for c in m.get("tool_calls") or []
    }
    answered = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    assert answered == called


def test_sanitize_is_idempotent():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c", "function": {"name": "test_tool"}}],
        },
        {"role": "tool", "tool_call_id": "c", "content": json.dumps(_huge_api_response())},
    ]

    sanitize_transcript(messages)
    snapshot = json.dumps(messages)
    second = sanitize_transcript(messages)

    assert not second.changed
    assert json.dumps(messages) == snapshot


def test_sanitized_output_is_far_below_the_provider_limit():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c", "function": {"name": "test_tool"}}],
        },
        {"role": "tool", "tool_call_id": "c", "content": json.dumps(_huge_api_response())},
    ]
    sanitize_transcript(messages)

    tool_content = next(m["content"] for m in messages if m.get("role") == "tool")
    assert len(tool_content) < PROVIDER_MAX_CONTENT_CHARS / 100


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_client_errors_are_not_retried(status):
    """Re-sending a malformed request cannot help; it just burns ~7s of
    backoff and buries the real cause."""
    from api.services.workflow_gen.llm_client import _is_retryable

    assert _is_retryable(_StatusError(status)) is False


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_transient_failures_are_retried(status):
    from api.services.workflow_gen.llm_client import _is_retryable

    assert _is_retryable(_StatusError(status)) is True


def test_network_failures_are_retried():
    import httpx
    from openai import APIConnectionError, APITimeoutError

    from api.services.workflow_gen.llm_client import _is_retryable

    request = httpx.Request("POST", "https://example.invalid")
    assert _is_retryable(APITimeoutError(request)) is True
    assert _is_retryable(APIConnectionError(request=request)) is True


@pytest.mark.asyncio
async def test_an_oversized_request_is_refused_before_it_is_sent():
    """Fail fast and name the message, rather than three doomed round trips
    ending in an opaque provider 400."""
    from api.services.workflow_gen import llm_client

    messages = [{"role": "tool", "content": "x" * (PROVIDER_MAX_CONTENT_CHARS + 1)}]

    with pytest.raises(llm_client.WorkflowGenLLMError) as exc:
        await llm_client.complete(messages, [])

    assert "too large" in str(exc.value)
    assert "messages[0]" in str(exc.value)
