"""Phone-number normalisation with libphonenumber (Apache-2.0)."""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType


@dataclass
class PhoneResult:
    raw: str
    e164: str | None
    valid: bool
    error: str | None = None
    kind: str | None = None


_KINDS = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "landline_or_mobile",
    PhoneNumberType.TOLL_FREE: "toll_free",
    PhoneNumberType.VOIP: "voip",
}


def normalize_phone(raw: str | None, default_region: str = "IN") -> PhoneResult:
    value = (raw or "").strip()
    if not value:
        return PhoneResult(value, None, False, "empty")
    if len(value) > 40:
        return PhoneResult(value[:40], None, False, "too long")
    cleaned = value.lstrip("'")  # neutralised spreadsheet values
    try:
        num = phonenumbers.parse(cleaned, default_region)
    except NumberParseException as exc:
        return PhoneResult(value, None, False, str(exc.args[0]) if exc.args else "unparseable")
    if not phonenumbers.is_valid_number(num):
        return PhoneResult(value, None, False, "not a valid number for its region")
    e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    return PhoneResult(value, e164, True, None, _KINDS.get(phonenumbers.number_type(num), "other"))


def mask_phone(e164: str | None) -> str:
    if not e164:
        return ""
    return e164[:4] + "•" * max(0, len(e164) - 7) + e164[-3:]
