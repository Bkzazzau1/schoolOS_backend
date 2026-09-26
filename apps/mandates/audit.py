"""The Mandates & Direct Debit audit trail. Details are scrubbed of anything that looks like a secret before they are stored, and callers never
pass a bank account number."""

from apps.core.scrub import scrub

from .models import MandateAuditEvent


def record(school, kind: str, *, actor=None, obj=None, object_type: str = "", object_id="", **detail) -> MandateAuditEvent:
    if obj is not None:
        object_type = object_type or obj.__class__.__name__
        object_id = obj.pk
    return MandateAuditEvent.objects.create(school=school, actor=actor, kind=kind, object_type=object_type, object_id=str(object_id), detail=scrub(detail))
