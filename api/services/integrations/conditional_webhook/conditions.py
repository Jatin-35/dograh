"""Evaluate a Conditional Webhook's "Send only if" rules against a finished call.

Rules are data, not code: each names a variable from the call (for example
``gathered_context.whatsapp_consent``), a check, and an optional value. They
are evaluated against the same context the payload template renders from.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Protocol

from api.utils.template_renderer import get_nested_value

Operator = Literal[
    "is_true",
    "is_false",
    "equals",
    "not_equals",
    "contains",
    "is_empty",
    "is_not_empty",
]

# Extraction on Hindi/Hinglish calls can answer in kind, so "haan"/"nahi" count.
_TRUE_WORDS = {"true", "yes", "y", "1", "haan", "han", "haa", "ha", "ji haan"}
_FALSE_WORDS = {"false", "no", "n", "0", "nahi", "nahin", "na", "ji nahi"}
_CONTEXT_PREFIXES = ("gathered_context.", "initial_context.")


class Rule(Protocol):
    variable: str
    operator: str
    value: str | None


def resolve_variable(path: str, context: dict[str, Any]) -> Any:
    """Look a rule's variable up in the call context.

    Accepts ``gathered_context.x`` / ``initial_context.x`` / any other render
    context path, with or without ``{{ }}``. A bare name (``whatsapp_consent``)
    is looked up in gathered_context first, then initial_context.
    """
    path = (path or "").strip()
    if path.startswith("{{") and path.endswith("}}"):
        path = path[2:-2].strip()
    if not path:
        return None
    value = get_nested_value(context, path)
    if value is None and "." not in path:
        for prefix in _CONTEXT_PREFIXES:
            value = get_nested_value(context, prefix + path)
            if value is not None:
                break
    return value


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def rule_holds(rule: Rule, context: dict[str, Any]) -> bool:
    actual = resolve_variable(rule.variable, context)
    expected = rule.value or ""
    op = rule.operator

    if op == "is_true":
        return actual is True or (
            not isinstance(actual, bool) and _text(actual) in _TRUE_WORDS
        )
    if op == "is_false":
        return actual is False or (
            not isinstance(actual, bool)
            and actual is not None
            and _text(actual) in _FALSE_WORDS
        )
    if op == "is_empty":
        return _is_empty(actual)
    if op == "is_not_empty":
        return not _is_empty(actual)
    if op == "equals":
        return actual is not None and _text(actual) == _text(expected)
    if op == "not_equals":
        return actual is None or _text(actual) != _text(expected)
    if op == "contains":
        if actual is None:
            return False
        if isinstance(actual, (list, tuple, set)):
            return any(_text(item) == _text(expected) for item in actual)
        return _text(expected) in _text(actual)
    return False


def describe(rule: Rule) -> str:
    value = f" {rule.value!r}" if rule.value else ""
    return f"{rule.variable} {rule.operator.replace('_', ' ')}{value}"


def conditions_met(
    rules: Iterable[Rule], match: str, context: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Whether the rules hold, and the rules that failed (for the run log).

    No rules means always send, like a plain Webhook.
    """
    rules = list(rules)
    if not rules:
        return True, []
    failed = [describe(r) for r in rules if not rule_holds(r, context)]
    if match == "any":
        return len(failed) < len(rules), failed
    return not failed, failed
