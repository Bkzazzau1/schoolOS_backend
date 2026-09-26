"""The connection audit trail. A detail that looks like a secret is dropped before it is stored:
the trail must be safe to read by anyone allowed to read it, whatever a caller passes in."""

from apps.core.scrub import scrub

from .models import BankAuditEvent


def record(school, kind: str, *, actor=None, connection=None, **detail) -> BankAuditEvent:
    return BankAuditEvent.objects.create(
        school=school, actor=actor, connection=connection, kind=kind, detail=scrub(detail)
    )
