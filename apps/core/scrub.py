"""Keeping secrets out of audit trails.

An audit detail is written by code and read by people, so it must be safe to read whatever a caller
passes in. Anything whose key looks like a secret is dropped, long text is cut, and lists are capped.
"""

_SECRET_WORDS = ("secret", "password", "passcode", "token", "key", "credential", "pin", "otp", "authorization")
_MAX_TEXT = 200
_MAX_ITEMS = 50


def scrub(value):
    if isinstance(value, dict):
        return {
            k: scrub(v) for k, v in value.items() if not any(word in str(k).lower() for word in _SECRET_WORDS)
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value][:_MAX_ITEMS]
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    return value
