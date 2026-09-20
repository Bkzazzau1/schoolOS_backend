"""Comparing people by phone number and national ID.

The same rules the app uses (identity_normalizer.dart), so a number the app accepts
is one the server accepts, and "0803 123 4567", "+234 803 123 4567" and
"08031234567" are the same number.
"""

import re


def normalize_phone(value: str) -> str | None:
    """An 11-digit Nigerian mobile number (08031234567), or None if it is not one."""
    digits = re.sub(r"[\s\-().]", "", value or "")
    if digits.startswith("+234"):
        digits = "0" + digits[4:]
    elif digits.startswith("234") and len(digits) == 13:
        digits = "0" + digits[3:]
    return digits if re.fullmatch(r"0[789][01]\d{8}", digits) else None


def normalize_nin(value: str) -> str | None:
    """The 11-digit NIN, or None if it is not exactly 11 digits."""
    digits = re.sub(r"[\s\-]", "", value or "")
    return digits if re.fullmatch(r"\d{11}", digits) else None


def normalize_name(value: str) -> str:
    """Lower-case with spacing and punctuation collapsed, for comparing names."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())).strip()
