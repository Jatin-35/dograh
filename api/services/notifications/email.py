"""Plain-text email over SMTP, for operational alerts.

Configured by environment (all optional; without SMTP_HOST nothing is sent and
callers fall back to what they show in the dashboard):

- ``SMTP_HOST``, ``SMTP_PORT`` (default 587)
- ``SMTP_USERNAME``, ``SMTP_PASSWORD``
- ``SMTP_FROM`` (default: ``SMTP_USERNAME``)
- ``SMTP_SECURITY``: ``starttls`` (default), ``ssl`` (implicit TLS, usually
  port 465) or ``none``

smtplib is blocking, so a send runs in a worker thread.
"""

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable

from loguru import logger

SEND_TIMEOUT_SECONDS = 20


def email_configured() -> bool:
    return bool(os.getenv("SMTP_HOST"))


def _send_blocking(recipients: list[str], subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME") or None
    password = os.getenv("SMTP_PASSWORD") or None
    sender = os.getenv("SMTP_FROM") or username
    security = os.getenv("SMTP_SECURITY", "starttls").lower()
    if not sender:
        raise RuntimeError("Set SMTP_FROM (or SMTP_USERNAME) to send email")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    if security == "ssl":
        client = smtplib.SMTP_SSL(
            host, port, timeout=SEND_TIMEOUT_SECONDS, context=context
        )
    else:
        client = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_SECONDS)
    with client:
        if security == "starttls":
            client.starttls(context=context)
        if username and password:
            client.login(username, password)
        client.send_message(message)


async def send_email(recipients: Iterable[str], subject: str, body: str) -> bool:
    """Send one email; True when sent. Never raises: an alert email that
    can't go out is logged, not allowed to break the caller."""
    to = sorted({r.strip() for r in recipients if r and r.strip()})
    if not to or not email_configured():
        return False
    try:
        await asyncio.to_thread(_send_blocking, to, subject, body)
        return True
    except Exception as e:
        logger.warning(
            f"Could not send email '{subject}' to {len(to)} recipient(s): {e}"
        )
        return False
