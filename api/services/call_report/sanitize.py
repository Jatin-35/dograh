"""Make a report safe to store and to send.

A report is stored in a Postgres JSONB column and returned as JSON, and both
refuse things that can turn up in what a model or a tool wrote: a NUL character
(``\\u0000``) cannot be stored in JSONB at all, and NaN or Infinity are not valid
JSON. Either one would make an otherwise fine report impossible to save, and the
call would silently be missing from the dashboard.
"""

import math
from typing import Any


def json_safe(value: Any) -> Any:
    """A copy of ``value`` with NUL characters removed and non-finite numbers
    replaced by None, recursively (dict keys included)."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {json_safe(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
