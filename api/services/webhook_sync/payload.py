"""Parse an incoming webhook body into lead payloads."""

import json
from typing import Any, Optional
from urllib.parse import parse_qsl

MAX_BODY_BYTES = 1_000_000
MAX_LEADS_PER_REQUEST = 500


class PayloadError(Exception):
    """The body can't be turned into leads; ``status_code`` is the reply."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _parse_form(text: str) -> dict[str, Any]:
    form: dict[str, Any] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        if key in form:
            existing = form[key]
            form[key] = (
                existing + [value] if isinstance(existing, list) else [existing, value]
            )
        else:
            form[key] = value
    return form


def _without_nul(value: Any) -> Any:
    """``value`` with NUL characters removed from every string, key or value.

    PostgreSQL text can't hold U+0000, so one stray NUL from a CRM (a
    copy-pasted or badly exported field) made storing the lead fail with a
    500 that the CRM retried forever, and the lead was lost.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_without_nul(k): _without_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_nul(v) for v in value]
    return value


def parse_leads(raw_body: bytes, content_type: Optional[str]) -> list[dict]:
    """One payload per lead. A JSON array means several leads; a JSON object
    or a form post means one. NUL characters are dropped (see _without_nul)."""
    return _without_nul(_parse_leads(raw_body, content_type))


def _parse_leads(raw_body: bytes, content_type: Optional[str]) -> list[dict]:
    if len(raw_body) > MAX_BODY_BYTES:
        raise PayloadError(413, "Request body is too large")
    try:
        text = raw_body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PayloadError(400, "Request body must be UTF-8")
    if not text.strip():
        raise PayloadError(400, "Request body is empty")

    media_type = (content_type or "").split(";")[0].strip().lower()
    if media_type == "application/x-www-form-urlencoded":
        data: Any = _parse_form(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if media_type in ("", "text/plain") and "=" in text:
                data = _parse_form(text)
            else:
                raise PayloadError(400, "Request body is not valid JSON")

    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        if not data:
            raise PayloadError(400, "The lead list is empty")
        if len(data) > MAX_LEADS_PER_REQUEST:
            raise PayloadError(
                413, f"At most {MAX_LEADS_PER_REQUEST} leads per request"
            )
        if not all(isinstance(item, dict) for item in data):
            raise PayloadError(400, "Every lead in the list must be a JSON object")
        return data
    raise PayloadError(400, "Request body must be a JSON object or a list of objects")
