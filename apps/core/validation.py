"""Small checks for the JSON payloads the app syncs.

Each helper returns the cleaned value or raises Rejected with a message that
names the field, so a handler reads as a list of what it requires.
"""

import re
from collections.abc import Iterable
from typing import Any

from .errors import Rejected

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_email(value: str) -> bool:
    return bool(_EMAIL.match(value))


def text(payload: dict, key: str, *, max_len: int = 200, required: bool = True) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Rejected(f"{key} must be text.")
    value = value.strip()
    if required and not value:
        raise Rejected(f"{key} is required.")
    if len(value) > max_len:
        raise Rejected(f"{key} is too long.")
    return value


def integer(payload: dict, key: str, *, minimum: int = 0, maximum: int = 10**10) -> int:
    value = payload.get(key)
    # bool is a subclass of int, and "true" must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(f"{key} must be a whole number.")
    if not minimum <= value <= maximum:
        raise Rejected(f"{key} must be between {minimum} and {maximum}.")
    return value


def boolean(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise Rejected(f"{key} must be true or false.")
    return value


def choice(value: Any, options: Iterable[str], label: str) -> str:
    if value not in set(options):
        raise Rejected(f"{label} is not a valid choice.")
    return value


def string_list(
    payload: dict,
    key: str,
    *,
    allowed: Iterable[str],
    min_items: int = 1,
    max_items: int = 100,
) -> list[str]:
    """A de-duplicated, sorted list whose every item is in `allowed`."""
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Rejected(f"{key} must be a list.")
    unique = sorted(set(value))
    if len(unique) < min_items:
        raise Rejected(f"Choose at least {min_items} for {key}.")
    if len(unique) > max_items:
        raise Rejected(f"Too many values for {key}.")
    unknown = set(unique) - set(allowed)
    if unknown:
        raise Rejected(f"{key} has an unknown value: {sorted(unknown)[0]}.")
    return unique
