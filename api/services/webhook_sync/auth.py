"""Authenticate a request against a webhook endpoint's secret.

Three modes, because CRMs differ in what they can send:

- ``api_key``: the secret in an ``X-API-Key`` header.
- ``hmac``: ``X-Botrix-Timestamp`` (unix seconds) plus ``X-Botrix-Signature``
  = hex HMAC-SHA256, keyed with the secret, of ``"<timestamp>." + raw body``
  (a ``sha256=`` prefix is accepted). The timestamp must be within 5 minutes
  of our clock, so a captured request can't be replayed later. Needs a CRM,
  Zapier/n8n step or custom code that can sign.
- ``url_token``: the secret as ``?token=`` in the URL, for CRMs that can't
  set headers at all. The least secure mode: URLs end up in more places than
  headers do. It is redacted from our access logs and can be rotated.
"""

import hashlib
import hmac
import secrets
import time
from typing import Mapping, Optional

AUTH_TYPES = ("api_key", "hmac", "url_token")
API_KEY_HEADER = "x-api-key"
SIGNATURE_HEADER = "x-botrix-signature"
TIMESTAMP_HEADER = "x-botrix-timestamp"
TOKEN_PARAM = "token"
# How far a signed request's timestamp may be from our clock, either way.
MAX_TIMESTAMP_SKEW_SECONDS = 300


def generate_secret() -> str:
    return secrets.token_urlsafe(32)


def sign(secret: str, raw_body: bytes, timestamp: str) -> str:
    """The signature a client puts in X-Botrix-Signature."""
    message = timestamp.encode() + b"." + raw_body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _same(provided: str, expected: str) -> bool:
    return hmac.compare_digest(provided.encode(), expected.encode())


def auth_failure(
    auth_type: str,
    secret: str,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    raw_body: bytes,
    now: Optional[float] = None,
) -> Optional[str]:
    """None if the request's credential is valid for the auth mode, else why
    not (for the request log; the client only ever sees a generic 401)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if auth_type == "api_key":
        provided = lowered.get(API_KEY_HEADER, "").strip()
        if not provided:
            return "Missing X-API-Key header"
        return None if _same(provided, secret) else "Wrong API key"
    if auth_type == "hmac":
        provided = lowered.get(SIGNATURE_HEADER, "").strip().lower()
        if provided.startswith("sha256="):
            provided = provided[len("sha256=") :]
        timestamp = lowered.get(TIMESTAMP_HEADER, "").strip()
        if not provided or not timestamp:
            return "Missing X-Botrix-Signature or X-Botrix-Timestamp header"
        if not timestamp.isdigit():
            return "X-Botrix-Timestamp must be unix seconds"
        current = time.time() if now is None else now
        if abs(current - int(timestamp)) > MAX_TIMESTAMP_SKEW_SECONDS:
            return "Signature timestamp is more than 5 minutes off"
        expected = sign(secret, raw_body, timestamp)
        return None if _same(provided, expected) else "Wrong signature"
    if auth_type == "url_token":
        provided = query.get(TOKEN_PARAM, "")
        if not provided:
            return "Missing token in the URL"
        return None if _same(provided, secret) else "Wrong token"
    return "Unknown auth mode"


def is_authorized(
    auth_type: str,
    secret: str,
    headers: Mapping[str, str],
    query: Mapping[str, str],
    raw_body: bytes,
    now: Optional[float] = None,
) -> bool:
    return auth_failure(auth_type, secret, headers, query, raw_body, now) is None
