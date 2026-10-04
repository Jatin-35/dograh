"""What we keep of a raw webhook request for debugging: never the secret."""

import logging
import re
from typing import Mapping, Optional

MAX_LOGGED_BODY_CHARS = 64_000
# Request logs are deleted after this many days.
RETENTION_DAYS = 30
# A lead's full original CRM payload is cleared after this many days; the
# lead's own fields (name, phone, email, variables) are kept.
PAYLOAD_RETENTION_DAYS = 30
REDACTED = "[redacted]"
RECEIVER_PATH_MARKER = "/webhooks/inbound/"
_QUERY_TOKEN = re.compile(r"([?&]token=)[^&\s]*", re.I)

# Any header that could carry a credential.
_SENSITIVE_HEADER = re.compile(
    r"auth|token|secret|signature|api[-_]?key|cookie|password|session", re.I
)
# Headers not worth keeping (the proxy chain adds many).
_DROPPED_HEADERS = {"connection", "accept-encoding", "content-length", "host"}


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in headers.items():
        name = key.lower()
        if name in _DROPPED_HEADERS:
            continue
        kept[name] = REDACTED if _SENSITIVE_HEADER.search(name) else value[:500]
    return kept


def loggable_body(raw_body: bytes) -> tuple[Optional[str], bool]:
    """The body as text, truncated; returns (text, was_truncated)."""
    if not raw_body:
        return None, False
    # PostgreSQL text can't hold NUL; drop it so the request is still logged.
    text = raw_body.decode("utf-8", errors="replace").replace("\x00", "")
    if len(text) > MAX_LOGGED_BODY_CHARS:
        return text[:MAX_LOGGED_BODY_CHARS], True
    return text, False


def client_ip(headers: Mapping[str, str], peer: Optional[str]) -> Optional[str]:
    """The caller's address behind our proxies (first X-Forwarded-For hop)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    forwarded = lowered.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    address = lowered.get("x-real-ip") or lowered.get("cf-connecting-ip") or peer
    return address[:64] if address else None


def redact_query_token(path: str) -> str:
    """``/webhooks/inbound/<id>?token=SECRET`` → ``...?token=[redacted]``."""
    return _QUERY_TOKEN.sub(lambda m: m.group(1) + REDACTED, path)


class _AccessLogTokenFilter(logging.Filter):
    """Keeps URL-token secrets out of the server's access log.

    uvicorn's access record carries (client, method, path+query, http version,
    status) as args; the path is rewritten for receiver requests only.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and RECEIVER_PATH_MARKER in args[2]
            and "token=" in args[2].lower()
        ):
            record.args = args[:2] + (redact_query_token(args[2]),) + args[3:]
        return True


_ACCESS_LOG_FILTER = _AccessLogTokenFilter()


def install_access_log_redaction() -> None:
    """Attach the filter to uvicorn's access logger (idempotent)."""
    access_logger = logging.getLogger("uvicorn.access")
    if _ACCESS_LOG_FILTER not in access_logger.filters:
        access_logger.addFilter(_ACCESS_LOG_FILTER)
