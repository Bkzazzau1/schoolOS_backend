from apps.core.scrub import scrub

from .models import MediaAuditEvent


def record(school, kind: str, *, actor=None, obj=None, object_type: str = "", object_id="", **detail) -> MediaAuditEvent:
    if obj is not None:
        object_type = object_type or obj.__class__.__name__
        object_id = obj.pk
    return MediaAuditEvent.objects.create(
        school=school, actor=actor, kind=kind, object_type=object_type, object_id=str(object_id), detail=scrub(detail)
    )
