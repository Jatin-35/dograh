"""Recognise a provider rejecting our key or account, whatever shape it takes.

Shared by the campaign circuit breaker (pause the campaign) and the provider
services (stop reconnecting and say why). Providers report a rejected key as
an HTTP status on the SDK exception, as a status in the error text, or only
in words (Google's 400 "API key not valid", WebSocket closes 1007/1008).
"""

import re
from typing import Optional

from pipecat.frames.frames import ErrorFrame

PROVIDER_PAUSE_STATUSES = {401, 402, 403}
_HTTP_STATUS_RE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:_code)?|error\s+code)\s*[:=]?\s*(\d{3})\b",
    re.IGNORECASE,
)


# Providers that don't put an HTTP status on a rejected key still say so:
# Google answers 400 "API key not valid", Gemini Live closes with 1007 and the
# same text, ElevenLabs and Murf close with 1008 "Invalid API key".
_AUTH_WORDING_RE = re.compile(
    r"api[ _-]?key (?:is )?not valid|invalid[ _-]?api[ _-]?key|incorrect api key"
    r"|api_key_invalid|authentication[ _]error|invalid (?:or missing )?authentication",
    re.IGNORECASE,
)
# Out of credit. OpenAI reports it as 429 "insufficient_quota" ("You exceeded
# your current quota, please check your plan and billing details"), which is
# not a rate limit to wait out, so it pauses like a 402.
_CREDIT_WORDING_RE = re.compile(
    r"insufficient[ _](?:quota|credits?|balance|funds)|exceeded your current quota"
    r"|check your plan and billing",
    re.IGNORECASE,
)

def _status_from_exception(exception) -> Optional[int]:
    """The HTTP status carried by a provider SDK exception, if any.

    Covers the shapes our providers raise: ``status_code`` (OpenAI, Anthropic,
    httpx-based SDKs), ``status`` (aiohttp), ``code`` (google-genai), and a
    ``response`` holding either (httpx, websockets' InvalidStatus). Walks the
    ``__cause__``/``__context__`` chain since wrappers often re-raise.
    """
    seen = set()
    while exception is not None and id(exception) not in seen:
        seen.add(id(exception))
        for holder in (exception, getattr(exception, "response", None)):
            if holder is None:
                continue
            for attr in ("status_code", "status", "code", "http_status"):
                value = getattr(holder, attr, None)
                if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                    return value
        exception = exception.__cause__ or exception.__context__
    return None


def provider_http_status(exception=None, message=""):
    """Read a provider error's HTTP status from the exception, then the text.

    Some providers only report the status in the error text (e.g. Deepgram's
    WebSocket errors), so the message and the exception text are scanned too.
    A rejected key or exhausted credit described only in words (no 401/402/
    403) is reported as 401 or 402 respectively, so it pauses the campaign.
    """
    status = _status_from_exception(exception) if exception is not None else None
    text = f"{message or ''} {exception or ''}"
    if status is None:
        statuses = [int(match) for match in _HTTP_STATUS_RE.findall(text)]
        status = next(
            (s for s in statuses if s in PROVIDER_PAUSE_STATUSES),
            statuses[0] if statuses else None,
        )
    if status in PROVIDER_PAUSE_STATUSES:
        return status
    if _CREDIT_WORDING_RE.search(text):
        return 402
    if _AUTH_WORDING_RE.search(text):
        return 401
    return status


class ProviderRejectionAwareWebsocket:
    """Mix into a pipecat websocket service that reconnects on error.

    When the provider closes the connection because it rejected our key or
    account (e.g. Murf's 1008 "Invalid api key used"), reconnecting can't
    help: report the rejection with its reason and stop. Without this the
    service retries, then reports only "connection failed 3 times", which
    hides the cause from the call's error handling and campaign protection.
    """

    async def _maybe_try_reconnect(self, error_message, report_error, error=None):
        if error is not None and provider_http_status(error, error_message) in PROVIDER_PAUSE_STATUSES:
            await report_error(ErrorFrame(error=error_message, exception=error))
            return False
        return await super()._maybe_try_reconnect(error_message, report_error, error)

