"""The connection audit trail. A detail that looks like a secret is dropped before it is stored:
the trail must be safe to read by anyone allowed to read it, whatever a caller passes in."""

from .models import BankAuditEvent

_SECRET_WORDS = ("secret", "password", "passcode", "token", "key", "credential", "pin", "otp", "authorization")
_MAX = 200


def _scrub(value):
    if isinstance(value, dict):
        return {
            k: _scrub(v) for k, v in value.items() if not any(word in str(k).lower() for word in _SECRET_WORDS)
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value][:50]
    if isinstance(value, str):
        return value[:_MAX]
    return value


def record(school, kind: str, *, actor=None, connection=None, **detail) -> BankAuditEvent:
    return BankAuditEvent.objects.create(
        school=school, actor=actor, connection=connection, kind=kind, detail=_scrub(detail)
    )
