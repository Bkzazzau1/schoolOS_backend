"""The Smart Money Collection audit trail. Details are scrubbed of anything that looks like a secret before they are stored."""

from apps.core.scrub import scrub

from .models import CollectionAuditEvent


def record(school, kind: str, *, actor=None, obj=None, object_type: str = "", object_id="", **detail) -> CollectionAuditEvent:
    if obj is not None:
        object_type = object_type or obj.__class__.__name__
        object_id = obj.pk
    return CollectionAuditEvent.objects.create(
        school=school, actor=actor, kind=kind, object_type=object_type, object_id=str(object_id), detail=scrub(detail)
    )
