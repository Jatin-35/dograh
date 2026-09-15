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


# ---------------------------------------------------------------------------
# Role-aware caps
# ---------------------------------------------------------------------------
#
# Shipped initially with one cap for every role, which rewrote a 57,892-char
# *user* message on production into a 2KB JSON envelope talking about "the
# full result" and "narrower queries". Tool output is machine noise nobody
# chose; a long user message is someone pasting the catalogue they want an
# agent built from. They must not be treated the same.


def test_a_long_user_paste_keeps_its_content():
    paste = "Build me an agent from this catalogue.\n\n" + ("Product line. " * 5_000)
    assert len(paste) > MAX_TOOL_RESULT_CHARS  # would have been mangled before
    messages = [{"role": "user", "content": paste}]

    assert compact_oversized_messages(messages) == 0
    assert messages[0]["content"] == paste


def test_a_long_user_paste_stays_prose_not_json():
    """The person's own words must not come back as a JSON object — the model
    reads that as data rather than as what they said."""
    from api.services.workflow_gen.transcript import (
        MAX_TEXT_MESSAGE_CHARS,
        compact_text_message,
    )

    paste = "Please read this.\n\n" + ("x" * (MAX_TEXT_MESSAGE_CHARS + 5_000))
    out = compact_text_message(paste)

    assert out.startswith("Please read this.")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "truncated to fit the conversation" in out
    assert "_truncated" not in out


def test_an_enormous_user_message_is_still_bounded():
    from api.services.workflow_gen.transcript import MAX_TEXT_MESSAGE_CHARS

    messages = [{"role": "user", "content": "z" * (MAX_TEXT_MESSAGE_CHARS * 3)}]

    assert compact_oversized_messages(messages) == 1
    assert len(messages[0]["content"]) < MAX_TEXT_MESSAGE_CHARS + 1_000


def test_tool_messages_keep_the_aggressive_cap():
    """The looser text budget must not leak onto tool results — that's the
    original 17MB bug."""
    messages = [
        {"role": "tool", "tool_call_id": "c", "content": json.dumps(_huge_api_response())}
    ]

    assert compact_oversized_messages(messages) == 1
    assert len(messages[0]["content"]) <= MAX_TOOL_RESULT_CHARS


def test_assistant_prose_is_treated_as_text_not_tool_output():
    messages = [{"role": "assistant", "content": "a" * (MAX_TOOL_RESULT_CHARS + 1_000)}]

    assert compact_oversized_messages(messages) == 0
    assert len(messages[0]["content"]) == MAX_TOOL_RESULT_CHARS + 1_000
    assert messages[0]["content"].startswith("aaa")


def test_a_none_content_assistant_message_is_skipped():
    messages = [{"role": "assistant", "content": None, "tool_calls": []}]
    assert compact_oversized_messages(messages) == 0


# ---------------------------------------------------------------------------
# Document-sized tool results
# ---------------------------------------------------------------------------


class TestDocumentResultsAreNotShrunkAway:
    """Reported from production: asked to rewrite a ~29,000-character node
    prompt, the assistant said the prompt came back truncated and refused to
    save — correctly, since a partial rewrite would delete what it never saw.

    `get_node` returned the whole thing. The generic 24,000-char tool cap then
    shrank it before the model ever read it, defeating the one tool that exists
    to return a long field in full.
    """

    PROMPT = "A" * 29_000

    def test_a_node_prompt_over_the_generic_cap_survives(self):
        from api.services.workflow_gen.transcript import (
            MAX_TOOL_RESULT_CHARS,
            compact_tool_result,
            result_cap_for,
        )

        payload = {"id": "node-agenda", "data": {"prompt": self.PROMPT}}
        assert len(self.PROMPT) > MAX_TOOL_RESULT_CHARS  # the situation

        shrunk = compact_tool_result(payload)
        assert "_truncated" in shrunk  # what production actually did

        kept = compact_tool_result(payload, max_chars=result_cap_for("get_node"))
        assert "_truncated" not in kept
        assert self.PROMPT in kept

    def test_an_ordinary_tool_still_gets_the_aggressive_cap(self):
        """The document budget is an exception, not a relaxation. A runaway
        `test_tool` response must still be shrunk — that cap is what stops one
        from poisoning a thread permanently."""
        from api.services.workflow_gen.transcript import (
            MAX_TOOL_RESULT_CHARS,
            compact_tool_result,
            result_cap_for,
        )

        assert result_cap_for("test_tool") == MAX_TOOL_RESULT_CHARS
        assert result_cap_for(None) == MAX_TOOL_RESULT_CHARS
        assert result_cap_for("list_workflows") == MAX_TOOL_RESULT_CHARS

        huge = compact_tool_result(
            {"data": {"rows": ["x" * 100] * 5_000}},
            max_chars=result_cap_for("test_tool"),
        )
        assert "_truncated" in huge

    def test_a_document_result_is_not_re_shrunk_on_reload(self):
        """A transcript is re-sanitized every time it loads. Without tool
        awareness here, the model would read a prompt in full on one turn and
        find it truncated on the next — worse than never having had it."""
        from api.services.workflow_gen.transcript import (
            compact_oversized_messages,
            compact_tool_result,
            result_cap_for,
        )

        content = compact_tool_result(
            {"id": "node-agenda", "data": {"prompt": self.PROMPT}},
            max_chars=result_cap_for("get_node"),
        )
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "get_node", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": content},
        ]

        compact_oversized_messages(messages)

        assert self.PROMPT in messages[1]["content"]
        assert "_truncated" not in messages[1]["content"]

    def test_an_ordinary_tools_reply_is_still_re_shrunk_on_reload(self):
        from api.services.workflow_gen.transcript import compact_oversized_messages

        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "test_tool", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "z" * 60_000},
        ]

        repaired = compact_oversized_messages(messages)

        assert repaired == 1
        assert len(messages[1]["content"]) < 60_000

    def test_a_reply_whose_call_is_gone_falls_back_to_the_safe_cap(self):
        """Defensive: a trimmed transcript can leave a tool reply whose
        originating call was dropped. Unknown provenance gets the tight cap,
        never the generous one."""
        from api.services.workflow_gen.transcript import compact_oversized_messages

        messages = [{"role": "tool", "tool_call_id": "orphan", "content": "z" * 60_000}]
        compact_oversized_messages(messages)
        assert len(messages[0]["content"]) < 60_000
