"""How a transfer number is sent to the telephony provider.

Providers may rewrite numbers: VoiceLink reduces every number to the bare
10-digit form its carrier needs for mobiles, which turns a landline such as
08043061549 into 8043061549 and the transfer never connects. A Transfer Call
tool's ``number_format`` lets a number be sent in the exact shape its line
needs; "auto" keeps the provider's own handling (the default, unchanged).
"""

import re
from typing import Literal, Optional

NumberFormat = Literal["auto", "keep_zero", "with_91", "with_plus_91", "as_typed"]
NUMBER_FORMATS: tuple[str, ...] = ("auto", "keep_zero", "with_91", "with_plus_91", "as_typed")

_SEPARATORS = re.compile(r"[\s\-().]")
_PHONE_LIKE = re.compile(r"^\+?\d+$")


def _national(digits: str) -> str:
    """The number without India's country code or trunk 0, when it has one."""
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 13 and digits.startswith("091"):
        return digits[3:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def format_transfer_number(destination: str, number_format: Optional[str]) -> Optional[str]:
    """The number to send as-is, or None to leave it to the provider ("auto").

    Only 10-digit Indian numbers get a prefix added; anything else a prefix
    can't apply to (toll-free 1800…, short codes, extensions) is sent with
    just separators removed. SIP endpoints and templates that didn't resolve
    to a number are never touched.
    """
    if not number_format or number_format == "auto":
        return None
    cleaned = _SEPARATORS.sub("", (destination or "").strip())
    if not _PHONE_LIKE.match(cleaned):
        return None  # PJSIP/1234, sip:…, anything that isn't a phone number
    if number_format == "as_typed":
        return cleaned
    national = _national(cleaned.lstrip("+"))
    if len(national) != 10:
        return cleaned
    if number_format == "keep_zero":
        return f"0{national}"
    if number_format == "with_91":
        return f"91{national}"
    if number_format == "with_plus_91":
        return f"+91{national}"
    return None
