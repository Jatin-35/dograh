"""Indian mobile number normalization for incoming leads."""

import re
from typing import Any, Optional

# A 10-digit Indian mobile number starts with 6, 7, 8 or 9.
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")

# Country-code / trunk prefixes in front of the 10 digits.
_PREFIXES = ("0091", "091", "91", "0")


def normalize_indian_mobile(raw: Any) -> Optional[str]:
    """Return the number as E.164 (+91XXXXXXXXXX), or None if it is not a
    valid Indian mobile number.

    Accepts spaces, dashes, dots and brackets, and the +91 / 91 / 0 / 0091
    prefixes. CRMs sometimes send numbers as JSON numbers, including floats
    such as ``9876543210.0``.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        if not raw.is_integer():
            return None
        raw = int(raw)

    digits = re.sub(r"\D", "", str(raw).strip())
    if len(digits) > 10:
        for prefix in _PREFIXES:
            if digits.startswith(prefix) and len(digits) - len(prefix) == 10:
                digits = digits[len(prefix) :]
                break

    if not _INDIAN_MOBILE.match(digits):
        return None
    return f"+91{digits}"
