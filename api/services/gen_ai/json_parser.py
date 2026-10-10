"""Robust JSON parser for handling common LLM output mistakes."""

from __future__ import annotations

import json
import re
from typing import Any


def parse_llm_json(raw_content: str) -> dict[str, Any]:
    """Parse JSON from LLM output, handling common formatting issues.

    Handles the following common LLM mistakes:
    1. JSON wrapped in markdown code blocks (```json ... ``` or ``` ... ```)
    2. Extra whitespace or newlines around JSON
    3. Text before/after the JSON object

    Args:
        raw_content: The raw string output from the LLM.

    Returns:
        Parsed JSON as a dictionary. If parsing fails, returns {"raw": raw_content}.
    """
    if not raw_content or not raw_content.strip():
        return {}

    content = raw_content.strip()

    # Attempt 1: Direct parse (ideal case)
    parsed = _try_parse_json(content)
    if parsed is not None:
        return parsed

    # Attempt 2: Remove markdown code block wrappers
    # Matches ```json ... ``` or ``` ... ```
    code_block_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    code_block_match = re.search(code_block_pattern, content)
    if code_block_match:
        extracted = code_block_match.group(1).strip()
        parsed = _try_parse_json(extracted)
        if parsed is not None:
            return parsed

    # Attempt 3: Find JSON object by matching braces
    parsed = _extract_json_object(content)
    if parsed is not None:
        return parsed

    # Attempt 3b: An object cut off just before its closing braces. Some models
    # (Hopper's gemma-4-31b) stop one token early on every reply. Runs before
    # the array search, which would otherwise return a list nested inside it.
    parsed = _close_truncated_object(content)
    if parsed is not None:
        return parsed

    # Attempt 4: Find JSON array by matching brackets
    parsed = _extract_json_array(content)
    if parsed is not None:
        return parsed

    # All attempts failed - return raw content
    return {"raw": raw_content}


def _try_parse_json(content: str) -> dict[str, Any] | list | None:
    """Attempt to parse JSON, returning None on failure."""
    try:
        result = json.loads(content)
        if isinstance(result, (dict, list)):
            return result
        return None
    except json.JSONDecodeError:
        return None


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """Extract a JSON object from text by finding matching braces."""
    # Find the first opening brace
    start = content.find("{")
    if start == -1:
        return None

    # Find matching closing brace by counting braces
    depth = 0
    in_string = False
    escape_next = False
    end = -1

    for i, char in enumerate(content[start:], start=start):
        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return None

    json_str = content[start : end + 1]
    return _try_parse_json(json_str)


def _close_truncated_object(content: str) -> dict[str, Any] | None:
    """Parse an object that ends after a complete value but before its closers.

    Only closing ``}``/``]`` are added. A reply cut off inside a string or after
    a key still fails to parse and falls through to the raw fallback.
    """
    start = content.find("{")
    if start == -1:
        return None

    closers = []
    in_string = False
    escape_next = False
    for char in content[start:]:
        if escape_next:
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            closers.append("}" if char == "{" else "]")
        elif char in "}]":
            if not closers or closers.pop() != char:
                return None

    if in_string or not closers:
        return None

    body = content[start:].rstrip()
    if body.endswith(","):
        body = body[:-1]
    parsed = _try_parse_json(body + "".join(reversed(closers)))
    return parsed if isinstance(parsed, dict) else None


def _extract_json_array(content: str) -> list | None:
    """Extract a JSON array from text by finding matching brackets."""
    # Find the first opening bracket
    start = content.find("[")
    if start == -1:
        return None

    # Find matching closing bracket by counting brackets
    depth = 0
    in_string = False
    escape_next = False
    end = -1

    for i, char in enumerate(content[start:], start=start):
        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return None

    json_str = content[start : end + 1]
    return _try_parse_json(json_str)
