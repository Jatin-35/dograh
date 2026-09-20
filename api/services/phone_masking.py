"""Mask customer phone numbers for the users who should not see them in full.

Masking is a per-organization switch (``MASK_PHONE_NUMBERS``), off unless a
superadmin turns it on, and it never applies to superadmins — they are the
platform's own staff. That keeps masking from silently changing what an
organization that has not asked for it sees today.

Scope, honestly: this masks the *structured* number fields (reports, the run
list, run detail context, CSV export). It cannot scrub a number a caller speaks
into the transcript or the recording, nor tool arguments in the raw call logs.
"""

import re
from typing import Any, Optional

from api.db import db_client
from api.db.models import UserModel
from api.enums import OrganizationConfigurationKey

MASK_CHAR = "•"

# Keys, at the top level of a context dict, that hold a customer's number.
PHONE_CONTEXT_KEYS = (
    "phone_number",
    "caller_number",
    "called_number",
    "customer_phone_number",
)

_NATIONAL_DIGITS = 10
_MIN_DIGITS_TO_KEEP_ENDS = 8


def mask_phone_number(value: Optional[str]) -> Optional[str]:
    """Mask a number as ``+91 98••••3210``: country code, first two digits, last four.

    The country code is whatever precedes the last ten digits; a number with ten
    or fewer digits is treated as national. Shorter numbers keep only their last
    two digits. Anything with too few digits to be a number is fully masked.
    """
    if not value:
        return value

    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 3:
        return MASK_CHAR * 4

    if len(digits) > _NATIONAL_DIGITS:
        country, national = digits[:-_NATIONAL_DIGITS], digits[-_NATIONAL_DIGITS:]
        prefix = f"+{country} "
    else:
        national, prefix = digits, ""

    if len(national) >= _MIN_DIGITS_TO_KEEP_ENDS:
        hidden = len(national) - 2 - 4
        return f"{prefix}{national[:2]}{MASK_CHAR * hidden}{national[-4:]}"

    return f"{prefix}{MASK_CHAR * (len(national) - 2)}{national[-2:]}"


def mask_phone_fields(context: Optional[dict]) -> Optional[dict]:
    """A copy of ``context`` with its known phone-number keys masked."""
    if not isinstance(context, dict):
        return context
    masked = dict(context)
    for key in PHONE_CONTEXT_KEYS:
        if isinstance(masked.get(key), str):
            masked[key] = mask_phone_number(masked[key])
    return masked


def phone_masking_enabled_from_configuration_value(value: object) -> bool:
    """Read the stored MASK_PHONE_NUMBERS value. Anything unrecognised means off."""
    if not isinstance(value, dict):
        return False
    return bool(value.get("enabled", False))


async def is_phone_masking_enabled_for_organization(
    organization_id: Optional[int],
) -> bool:
    if organization_id is None:
        return False
    value = await db_client.get_configuration_value(
        organization_id, OrganizationConfigurationKey.MASK_PHONE_NUMBERS.value
    )
    return phone_masking_enabled_from_configuration_value(value)


async def set_phone_masking_for_organization(
    organization_id: int, enabled: bool
) -> bool:
    """Turn masking on or off for one organization. Returns the stored state."""
    await db_client.upsert_configuration(
        organization_id,
        OrganizationConfigurationKey.MASK_PHONE_NUMBERS.value,
        {"enabled": bool(enabled)},
    )
    return bool(enabled)


async def should_mask_phone_numbers(user: UserModel) -> bool:
    """Whether numbers must be masked for this user in their selected organization."""
    if getattr(user, "is_superuser", False):
        return False
    return await is_phone_masking_enabled_for_organization(
        user.selected_organization_id
    )


def apply_run_masking(run: dict[str, Any]) -> dict[str, Any]:
    """Mask the number fields of a run-list / run-detail dict, in place."""
    for key in ("phone_number", "caller_number", "called_number"):
        if run.get(key):
            run[key] = mask_phone_number(run[key])
    for key in ("initial_context", "gathered_context"):
        if key in run:
            run[key] = mask_phone_fields(run[key])
    return run
