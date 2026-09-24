from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .models import ClassTeacherAssignment
from .services import (
    CLASS_TEACHER_ENTITY,
    CLASS_TEACHER_LINK_ENTITY,
    serialize_class_teacher_assignment,
    upsert_class_teacher_assignment,
)

_READ_ROLES = {Role.ADMINISTRATOR, Role.PRINCIPAL, Role.PROPRIETOR}


def _assignment_by_external(school, external_id):
    return (
        ClassTeacherAssignment.objects.select_related(
            "academic_class", "session", "teacher_membership__user"
        )
        .filter(school=school, external_id=external_id)
        .first()
    )


def _canonicalize(ctx: MutationContext, payload: dict) -> None:
    SyncRecord.objects.filter(
        school=ctx.membership.school, entity_type=ctx.entity_type, entity_id=ctx.entity_id
    ).update(payload=payload)


class ClassTeacherAssignmentHandler(EntityHandler):
    """Who is the class/form teacher of one whole class - a staffing
    decision, so its write authority mirrors Principal Teaching Assignment
    exactly (Principal Secondary-only, or Proprietor school-wide), not
    Administrator."""

    entity_type = CLASS_TEACHER_ENTITY
    roles = frozenset({Role.PRINCIPAL, Role.PROPRIETOR})

    def visible(self, membership, payload):
        if membership.role in _READ_ROLES:
            return payload
        if membership.role == Role.TEACHER and payload.get("teacherId") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("A class-teacher assignment is historical and cannot be deleted; end it with a handover instead.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the class-teacher assignment entity id.")
        return {
            "id": entity_id,
            "classId": text(p, "classId", max_len=64),
            "sessionId": text(p, "sessionId", max_len=64),
            "teacherId": text(p, "teacherId", max_len=64),
            "handoverReason": text(p, "handoverReason", max_len=1000, required=False),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_class_teacher_assignment(membership=ctx.membership, payload=stored)
        item = _assignment_by_external(ctx.membership.school, item.external_id)
        _canonicalize(ctx, serialize_class_teacher_assignment(item))


class ClassTeacherLinkHandler(EntityHandler):
    """A Teacher-readable roster of the classes they are currently the class
    teacher for. Server-derived from ClassTeacherAssignment; never written
    directly by a device."""

    entity_type = CLASS_TEACHER_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx: MutationContext) -> None:
        raise Rejected("Class-teacher links are derived from canonical assignments.")

    def visible(self, membership, payload):
        if membership.role in _READ_ROLES:
            return payload
        if membership.role == Role.TEACHER and payload.get("id") == str(membership.id):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        raise Rejected("Class-teacher links are server-generated.")


HANDLERS = [ClassTeacherAssignmentHandler(), ClassTeacherLinkHandler()]
