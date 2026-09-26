"""The finance audit trail. Details are scrubbed of anything that looks like a secret before they are
stored, so the trail is safe to read by anyone allowed to read it."""

from apps.core.scrub import scrub

from .models import FinanceAuditEvent


def record(school, kind: str, *, actor=None, obj=None, object_type: str = "", object_id="", **detail) -> FinanceAuditEvent:
    """Write one event. Pass the object it is about (`obj`), or its type and id by hand."""
    if obj is not None:
        object_type = object_type or obj.__class__.__name__
        object_id = obj.pk
    return FinanceAuditEvent.objects.create(
        school=school, actor=actor, kind=kind, object_type=object_type, object_id=str(object_id), detail=scrub(detail)
    )
