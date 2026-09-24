from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .curriculum_models import SubjectRequirement
from .curriculum_services import (
    TEACHER_CLASS_LINK_ENTITY,
    serialize_class_subject,
    serialize_subject,
    serialize_teaching_assignment,
    upsert_class_subject,
    upsert_student_subject_selection,
    upsert_subject,
    upsert_teaching_assignment,
)


SUBJECT_ENTITY = "academic_subject"
CLASS_SUBJECT_ENTITY = "academic_class_subject"
STUDENT_SELECTION_ENTITY = "academic_student_subject_selection"
TEACHING_ASSIGNMENT_ENTITY = "academic_teaching_assignment"

_ADMIN_ROLES = frozenset({Role.ADMINISTRATOR})
_PRINCIPAL_ROLES = frozenset({Role.PRINCIPAL})
_ACADEMIC_READ_ROLES = {Role.PROPRIETOR, Role.ADMINISTRATOR, Role.PRINCIPAL}
_REQUIREMENTS = set(SubjectRequirement.values)


def _positive_int(payload, key, *, maximum=30):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(f"{key} must be an integer.")
    if value < 1 or value > maximum:
        raise Rejected(f"{key} is outside the allowed range.")
    return value


class SubjectHandler(EntityHandler):
    entity_type = SUBJECT_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _ACADEMIC_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        external_id = text(ctx.payload, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the subject entity id.")
        return {
            "id": external_id,
            "code": text(ctx.payload, "code", max_len=40),
            "name": text(ctx.payload, "name", max_len=120),
            "description": text(ctx.payload, "description", max_len=2000, required=False),
            "isActive": boolean(ctx.payload, "isActive"),
        }

    def after_write(self, ctx, stored):
        subject = upsert_subject(membership=ctx.membership, payload=stored)
        SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=self.entity_type,
            entity_id=ctx.entity_id,
        ).update(payload=serialize_subject(subject))


class ClassSubjectHandler(EntityHandler):
    entity_type = CLASS_SUBJECT_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _ACADEMIC_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        external_id = text(ctx.payload, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the class-subject entity id.")
        existing = ctx.existing or {}
        if existing:
            for key in ("sessionId", "classId", "subjectId"):
                if existing.get(key) != ctx.payload.get(key):
                    raise Rejected("Session, class and subject are immutable for a curriculum record.")
        return {
            "id": external_id,
            "sessionId": text(ctx.payload, "sessionId", max_len=64),
            "classId": text(ctx.payload, "classId", max_len=64),
            "subjectId": text(ctx.payload, "subjectId", max_len=64),
            "requirement": choice(
                ctx.payload.get("requirement"), _REQUIREMENTS, "requirement"
            ),
            "periodsPerWeek": _positive_int(ctx.payload, "periodsPerWeek"),
            "isActive": boolean(ctx.payload, "isActive"),
        }

    def after_write(self, ctx, stored):
        offering = upsert_class_subject(membership=ctx.membership, payload=stored)
        SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=self.entity_type,
            entity_id=ctx.entity_id,
        ).update(payload=serialize_class_subject(offering))


class StudentSubjectSelectionHandler(EntityHandler):
    entity_type = STUDENT_SELECTION_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _ACADEMIC_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        external_id = text(ctx.payload, "id", max_len=128)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the subject-selection entity id.")
        existing = ctx.existing or {}
        if existing:
            for key in ("studentId", "classSubjectId"):
                if existing.get(key) != ctx.payload.get(key):
                    raise Rejected("Student and elective are immutable for a subject selection.")
        return {
            "id": external_id,
            "studentId": text(ctx.payload, "studentId", max_len=80),
            "classSubjectId": text(ctx.payload, "classSubjectId", max_len=64),
            "selected": boolean(ctx.payload, "selected"),
        }

    def after_write(self, ctx, stored):
        upsert_student_subject_selection(membership=ctx.membership, payload=stored)


class TeachingAssignmentHandler(EntityHandler):
    entity_type = TEACHING_ASSIGNMENT_ENTITY
    roles = _PRINCIPAL_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in {Role.PROPRIETOR, Role.PRINCIPAL} else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        external_id = text(ctx.payload, "id", max_len=64)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the teaching-assignment entity id.")
        existing = ctx.existing or {}
        class_subject_id = text(ctx.payload, "classSubjectId", max_len=64)
        if existing and existing.get("classSubjectId") != class_subject_id:
            raise Rejected("The class-subject responsibility is immutable; transfer the teacher instead.")
        teacher_staff_id = text(ctx.payload, "teacherStaffId", max_len=128)
        transfer_reason = text(
            ctx.payload, "transferReason", max_len=1000, required=False
        )
        if existing and existing.get("teacherStaffId") != teacher_staff_id and not transfer_reason:
            raise Rejected("A teacher reassignment requires a transfer reason.")
        return {
            "id": external_id,
            "classSubjectId": class_subject_id,
            "teacherStaffId": teacher_staff_id,
            "transferReason": transfer_reason,
            "isActive": boolean(ctx.payload, "isActive"),
        }

    def after_write(self, ctx, stored):
        assignment = upsert_teaching_assignment(
            membership=ctx.membership, payload=stored
        )
        SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=self.entity_type,
            entity_id=ctx.entity_id,
        ).update(payload=serialize_teaching_assignment(assignment))


class TeacherClassLinkHandler(EntityHandler):
    entity_type = TEACHER_CLASS_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx):
        raise Rejected("Teacher class links are managed by the SchoolOS server.")

    def clean(self, ctx):
        raise Rejected("Teacher class links are managed by the SchoolOS server.")

    def visible(self, membership, payload):
        if membership.role != Role.TEACHER:
            return None
        if payload.get("teacherMembershipId") != str(membership.id):
            return None
        return payload


HANDLERS = [
    SubjectHandler(),
    ClassSubjectHandler(),
    StudentSubjectSelectionHandler(),
    TeachingAssignmentHandler(),
    TeacherClassLinkHandler(),
]
