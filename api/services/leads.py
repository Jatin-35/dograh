"""In-app sales leads ("Hire an expert", "Enterprise") delivered to our team.

The lead forms used to post to Dograh's lead server; with that cut off a
submission went nowhere. Now the signed-in user's form is emailed to
``LEADS_EMAIL`` (default contact@botrixai.com) through the shared SMTP sender.
It is also always written to the log, so a lead survives a missing or failing
SMTP setup.
"""

import os
from typing import Literal

from loguru import logger

from api.services.notifications.email import send_email

LeadKind = Literal["hire_expert", "enterprise"]

DEFAULT_LEADS_EMAIL = "contact@botrixai.com"

_TITLES = {"hire_expert": "Hire an expert", "enterprise": "Enterprise"}

# Readable labels, in the order they appear in the email.
_FIELD_LABELS = {
    "name": "Name",
    "company": "Company",
    "email": "Email",
    "workEmail": "Work email",
    "jobTitle": "Job title",
    "phone": "Phone",
    "agentGoal": "What the agent should do",
    "volume": "Monthly call volume",
    "deployment": "Needs enterprise deployment",
    "country": "Country",
    "timezone": "Timezone",
    "source": "Opened from",
}


def leads_recipients() -> list[str]:
    raw = os.getenv("LEADS_EMAIL") or DEFAULT_LEADS_EMAIL
    return [r.strip() for r in raw.split(",") if r.strip()]


def format_lead(
    kind: LeadKind,
    fields: dict[str, str],
    account_email: str | None,
    organization_id: int | None,
) -> tuple[str, str]:
    """Subject and plain-text body for one lead."""
    who = (
        fields.get("company")
        or fields.get("name")
        or fields.get("email")
        or fields.get("workEmail")
        or "unknown"
    )
    subject = f"[BotrixAI lead] {_TITLES[kind]}: {who}"

    lines = []
    for key, label in _FIELD_LABELS.items():
        value = (fields.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    # Anything the form sends that has no label yet still reaches us.
    for key, value in fields.items():
        if key not in _FIELD_LABELS and str(value).strip():
            lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(f"Signed-in account: {account_email or 'unknown'}")
    lines.append(f"Organization id: {organization_id if organization_id is not None else 'none'}")
    return subject, "\n".join(lines)


async def deliver_lead(
    kind: LeadKind,
    fields: dict[str, str],
    account_email: str | None,
    organization_id: int | None,
) -> bool:
    """Log the lead and email it to the team; True when the email went out."""
    subject, body = format_lead(kind, fields, account_email, organization_id)
    logger.info(f"Lead received: {subject}\n{body}")
    sent = await send_email(leads_recipients(), subject, body)
    if not sent:
        logger.warning(
            f"Lead '{subject}' was not emailed (SMTP not configured or failed); "
            "it is only in this log"
        )
    return sent
