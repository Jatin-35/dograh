"""Map an incoming CRM payload onto our standard lead fields.

``field_mapping`` on an endpoint maps a standard field to a path in the
payload, e.g. ``{"phone": "data.lead.mobile", "name": "data.lead.name",
"custom": {"product": "data.product"}}``. A standard field without a mapping
is looked up under common names (``mobile``, ``phone_number``, ...), at the top
level and one level inside common wrappers (``data``, ``lead``, ...), so a
plain payload works without any setup.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

STANDARD_FIELDS = (
    "phone",
    "name",
    "email",
    "external_lead_id",
    "source",
    "city",
    "language_preference",
)

# Names a CRM commonly uses for each standard field, compared after
# lower-casing and dropping everything but letters and digits.
_ALIASES: dict[str, tuple[str, ...]] = {
    "phone": (
        "phone",
        "mobile",
        "phonenumber",
        "mobilenumber",
        "mobileno",
        "phoneno",
        "contact",
        "contactnumber",
        "contactno",
        "mobilephone",
        "cellphone",
        "phone1",
        "primaryphone",
        "whatsapp",
        "whatsappnumber",
    ),
    "name": ("name", "fullname", "leadname", "customername", "contactname"),
    "email": ("email", "emailaddress", "emailid", "mail"),
    "external_lead_id": (
        "leadid",
        "prospectid",
        "externalid",
        "externalleadid",
        "recordid",
        "id",
    ),
    "source": ("source", "leadsource", "utmsource", "sourcename", "channel"),
    "city": ("city", "town", "district"),
    "language_preference": ("language", "languagepreference", "preferredlanguage"),
}

# Wrappers CRMs nest the lead fields in.
_CONTAINERS = (
    "data",
    "lead",
    "leads",
    "contact",
    "fields",
    "payload",
    "current",
    "record",
)

MAX_VARIABLES = 50
MAX_VALUE_LENGTH = 500
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _norm_key(key: str) -> str:
    return re.sub(r"[^0-9a-z]", "", str(key).lower())


def variable_name(key: str) -> str:
    """A payload key as a prompt variable name: ``Lead Source`` → ``lead_source``."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", str(key)).strip("_").lower()


def get_path(payload: Any, path: str) -> Any:
    """Read ``data.lead.mobile`` / ``leads[0].phone`` from a payload.

    A segment that doesn't match exactly is matched case-insensitively, so a
    mapping keeps working if a CRM changes ``Mobile`` to ``mobile``.
    """
    if not path:
        return None
    current = payload
    for name, index in _PATH_TOKEN.findall(path):
        if current is None:
            return None
        if index:
            if not isinstance(current, list) or int(index) >= len(current):
                return None
            current = current[int(index)]
            continue
        if isinstance(current, list):
            # A bare name on a list means "its first element's" field.
            current = current[0] if current else None
            if current is None:
                return None
        if not isinstance(current, dict):
            return None
        if name in current:
            current = current[name]
            continue
        wanted = _norm_key(name)
        current = next(
            (value for key, value in current.items() if _norm_key(key) == wanted),
            None,
        )
    return current


def _scalar(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text[:MAX_VALUE_LENGTH] if text else None


def _as_text(value: Any) -> Optional[str]:
    """Like _scalar, but an explicitly mapped object is kept as JSON text."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:MAX_VALUE_LENGTH]
    return _scalar(value)


def _levels(payload: dict, depth: int = 2) -> list[dict]:
    """The payload plus the common wrappers inside it (up to two deep, e.g.
    ``data.lead``), outermost first."""
    levels = [payload]
    if depth == 0:
        return levels
    for key, value in payload.items():
        if _norm_key(key) in _CONTAINERS:
            if isinstance(value, list) and value and isinstance(value[0], dict):
                value = value[0]
            if isinstance(value, dict):
                levels.extend(_levels(value, depth - 1))
    return levels


def _by_key(level: dict) -> dict[str, Any]:
    """A level's values by normalized key. LeadSquared prefixes custom fields
    with ``mx_`` (``mx_City``), so each is also reachable without it; an
    exact key wins over a prefixed one."""
    by_key: dict[str, Any] = {}
    for key, value in level.items():
        norm = _norm_key(key)
        if str(key).lower().startswith("mx_") and len(norm) > 2:
            by_key.setdefault(norm[2:], value)
    for key, value in level.items():
        by_key[_norm_key(key)] = value
    return by_key


def _detect(payload: dict, standard_field: str) -> Any:
    aliases = _ALIASES[standard_field]
    for level in _levels(payload):
        by_key = _by_key(level)
        for alias in aliases:
            value = by_key.get(alias)
            if _scalar(value) is not None:
                return value
    return None


def _detect_name(payload: dict) -> Optional[str]:
    name = _scalar(_detect(payload, "name"))
    if name:
        return name
    for level in _levels(payload):
        by_key = _by_key(level)
        first = _scalar(by_key.get("firstname"))
        last = _scalar(by_key.get("lastname"))
        if first or last:
            return " ".join(p for p in (first, last) if p)
    return None


def _set_last(variables: dict[str, str], name: str, value: str) -> None:
    """Set a variable and move it to the end (the end survives the cap)."""
    variables.pop(name, None)
    variables[name] = value


@dataclass
class MappedLead:
    phone_raw: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    external_lead_id: Optional[str] = None
    source: Optional[str] = None
    city: Optional[str] = None
    language_preference: Optional[str] = None
    # Everything the voice agent can use as {{variable}}.
    variables: dict[str, str] = field(default_factory=dict)


def map_lead(payload: dict, field_mapping: Optional[dict] = None) -> MappedLead:
    """Extract standard fields and call variables from one lead payload."""
    field_mapping = field_mapping or {}
    lead = MappedLead()

    for standard_field in STANDARD_FIELDS:
        path = field_mapping.get(standard_field)
        if path:
            value = _as_text(get_path(payload, path))
        elif standard_field == "name":
            value = _detect_name(payload)
        else:
            value = _scalar(_detect(payload, standard_field))
        if standard_field == "phone":
            lead.phone_raw = value
        else:
            setattr(lead, standard_field, value)

    variables: dict[str, str] = {}
    # Every plain field the CRM sent, so prompts can use any of them.
    for level in reversed(_levels(payload)):
        for key, value in level.items():
            text = _scalar(value)
            name = variable_name(key)
            if text is not None and name:
                variables.setdefault(name, text)
    # LeadSquared prefixes its custom fields (mx_Budget): also offer each one
    # under its plain name ({{budget}}), unless the CRM sent that name too.
    for name in [n for n in variables if n.startswith("mx_") and len(n) > 3]:
        variables.setdefault(name[3:], variables[name])
    # Explicitly mapped custom fields override detected ones.
    custom = field_mapping.get("custom") or {}
    if isinstance(custom, dict):
        for var, path in custom.items():
            name = variable_name(var)
            text = _as_text(get_path(payload, path)) if path else None
            if name and text is not None:
                _set_last(variables, name, text)
    # Standard fields under their standard names.
    for standard_field in ("name", "email", "source", "city", "language_preference"):
        value = getattr(lead, standard_field)
        if value is not None:
            _set_last(variables, standard_field, value)

    # Over the cap, keep the most important: mapped and standard fields sit
    # at the end, so the oldest detected fields are dropped first.
    if len(variables) > MAX_VARIABLES:
        variables = {k: variables[k] for k in list(variables)[-MAX_VARIABLES:]}
    lead.variables = variables
    return lead
