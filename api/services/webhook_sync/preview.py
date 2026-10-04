"""Preview how a sample CRM payload maps onto a lead, for the mapping screen.

Uses the receiver's own parser, mapper and phone normalizer, so what the
dashboard shows is exactly what a real request would store.
"""

from typing import Any, Optional

from api.services.webhook_sync.mapping import STANDARD_FIELDS, map_lead
from api.services.webhook_sync.payload import parse_leads
from api.services.webhook_sync.phone import normalize_indian_mobile

MAX_PATHS = 300
_MAX_PATH_VALUE = 120


def leaf_paths(payload: Any, prefix: str = "") -> list[dict[str, str]]:
    """Every plain value in the payload with its path (``data.lead.mobile``,
    ``leads[0].phone``), so a field can be mapped by picking it."""
    paths: list[dict[str, str]] = []

    def walk(value: Any, path: str) -> None:
        if len(paths) >= MAX_PATHS:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            # The first element stands for the list; CRMs send one lead per item.
            if value:
                walk(value[0], f"{path}[0]")
        elif value is not None:
            text = str(value)
            if len(text) > _MAX_PATH_VALUE:
                text = text[:_MAX_PATH_VALUE] + "…"
            paths.append({"path": path, "value": text})

    walk(payload, prefix)
    return paths


def preview_mapping(
    sample: str, content_type: Optional[str], field_mapping: dict
) -> dict[str, Any]:
    """Map the first lead of ``sample``. Raises ``PayloadError`` when the
    sample isn't something the receiver would accept."""
    items = parse_leads(sample.encode("utf-8"), content_type)
    first = items[0]
    mapped = map_lead(first, field_mapping)
    phone = normalize_indian_mobile(mapped.phone_raw) if mapped.phone_raw else None
    fields = {name: getattr(mapped, name, None) for name in STANDARD_FIELDS}
    fields.pop("phone", None)
    if not mapped.phone_raw:
        outcome = "rejected"
        reason = "No phone number found; map the phone field"
    elif phone is None:
        outcome = "invalid_number"
        reason = "Not a valid Indian mobile number"
    else:
        outcome = "received"
        reason = None
    return {
        "lead_count": len(items),
        "phone_raw": mapped.phone_raw,
        "phone": phone,
        "outcome": outcome,
        "reason": reason,
        "fields": fields,
        "variables": mapped.variables,
        "paths": leaf_paths(first),
    }
