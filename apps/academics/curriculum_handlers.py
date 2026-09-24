from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .curriculum_services import (
    TEACHER_CLASS_LINK_ENTITY,
    serialize_class_subject,
    serialize_subject,
    serialize_teaching_assignment,
    serialize_topic,
    set_student_elective,
    upsert_class_subject,
    upsert_curriculum_topic,
    upsert_subject,
    upsert_teaching_assignment,
)
from .models import ClassSubject, CurriculumRequirement, TeachingAssignment


SUBJECT_ENTITY = "academic_subject"
CLASS_SUBJECT_ENTITY = "academic_class_subject"
TOPIC_ENTITY = "academic_curriculum_topic"
TEACHING_ASSIGNMENT_ENTITY = "principal_teaching_assignment"
TEACHING_TRANSFER_ENTITY = "principal_assignment_transfer"
STUDENT_SELECTION_ENTITY = "academic_student_subject_selection"

_CURRICULUM_WRITE_ROLES = frozenset({Role.ADMINISTRATOR, Role.PRINCIPAL})
_ASSIGNMENT_WRITE_ROLES = frozenset({Role.PRINCIPAL, Role.PROPRIETOR})
_CURRICULUM_READ_ROLES = {Role.ADMINISTRATOR, Role.PRINCIPAL, Role.PROPRIETOR}
_REQUIREMENTS = set(CurriculumRequirement.values)
_TRANSFER_SCOPE = [
    "Lesson plans and submitted teaching work",
    "Syllabus and curriculum-progress history",
    "Assessment setup and score-entry history",
    "Class teaching notes attached to the assignment",
    "Timetable and period context",
    "Assignment history and previous handover records",
]


def _optional_text(payload: dict, key: str, *, max_len: int = 250) -> str:
    return text(payload, key, max_len=max_len, required=False)


def _positive_int(payload: dict, key: str, *, maximum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(f"{key} must be an integer.")
    if value < 1 or value > maximum:
        raise Rejected(f"{key} is outside the allowed range.")
    return value


def _canonicalize_record(ctx: MutationContext, payload: dict) -> None:
    SyncRecord.objects.filter(
        school=ctx.membership.school,
        entity_type=ctx.entity_type,
        entity_id=ctx.entity_id,
    ).update(payload=payload)


def _assignment(pk):
    return TeachingAssignment.objects.select_related(
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "teacher_membership",
        "previous_assignment__teacher_membership",
    ).filter(pk=pk).first()


def _assignment_by_external(school, external_id):
    return TeachingAssignment.objects.select_related(
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "teacher_membership",
        "previous_assignment__teacher_membership",
    ).filter(school=school, external_id=external_id).first()


class SubjectHandler(EntityHandler):
    entity_type = SUBJECT_ENTITY
    roles = _CURRICULUM_WRITE_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _CURRICULUM_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the subject entity id.")
        return {
            "id": entity_id,
            "code": text(p, "code", max_len=40),
            "name": text(p, "name", max_len=120),
            "shortName": _optional_text(p, "shortName", max_len=40),
            "section": _optional_text(p, "section", max_len=80),
            "isActive": boolean(p, "isActive"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_subject(membership=ctx.membership, payload=stored)
        _canonicalize_record(ctx, serialize_subject(item))


class ClassSubjectHandler(EntityHandler):
    entity_type = CLASS_SUBJECT_ENTITY
    roles = _CURRICULUM_WRITE_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _CURRICULUM_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the class-subject entity id.")
        return {
            "id": entity_id,
            "sessionId": text(p, "sessionId", max_len=64),
            "classId": text(p, "classId", max_len=64),
            "subjectId": text(p, "subjectId", max_len=64),
            "requirement": choice(
                p.get("requirement"),
                _REQUIREMENTS,
                "requirement",
            ),
            "periodsPerWeek": _positive_int(
                p,
                "periodsPerWeek",
                maximum=30,
            ),
            "isActive": boolean(p, "isActive"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_class_subject(membership=ctx.membership, payload=stored)
        item = ClassSubject.objects.select_related(
            "session",
            "academic_class",
            "subject",
        ).get(pk=item.pk)
        _canonicalize_record(ctx, serialize_class_subject(item))


class CurriculumTopicHandler(EntityHandler):
    entity_type = TOPIC_ENTITY
    roles = _CURRICULUM_WRITE_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _CURRICULUM_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the curriculum-topic entity id.")
        return {
            "id": entity_id,
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "termId": text(p, "termId", max_len=64),
            "sequence": _positive_int(p, "sequence", maximum=500),
            "title": text(p, "title", max_len=200),
            "description": _optional_text(
                p,
                "description",
                max_len=4000,
            ),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_curriculum_topic(membership=ctx.membership, payload=stored)
        _canonicalize_record(ctx, serialize_topic(item))


class TeachingAssignmentHandler(EntityHandler):
    """Canonicalize the existing Principal Teaching Assignment sync surface."""

    entity_type = TEACHING_ASSIGNMENT_ENTITY
    roles = _ASSIGNMENT_WRITE_ROLES

    def visible(self, membership, payload):
        if membership.role in _CURRICULUM_READ_ROLES:
            return payload
        if (
            membership.role == Role.TEACHER
            and payload.get("teacherId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the teaching-assignment entity id.")

        class_subject_id = text(p, "classSubjectId", max_len=64)
        return {
            "id": entity_id,
            "classSubjectId": class_subject_id,
            "teacherId": text(p, "teacherId", max_len=64),
            "periodsPerWeek": _positive_int(
                p,
                "periodsPerWeek",
                maximum=30,
            ),
            "handoverReason": _optional_text(
                p,
                "handoverReason",
                max_len=1000,
            ),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_teaching_assignment(
            membership=ctx.membership,
            payload=stored,
        )
        item = _assignment(item.id)
        _canonicalize_record(ctx, serialize_teaching_assignment(item))


class TeachingTransferHandler(EntityHandler):
    """Server-validated handover history written only after assignment change."""

    entity_type = TEACHING_TRANSFER_ENTITY
    roles = _ASSIGNMENT_WRITE_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _CURRICULUM_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the teaching-transfer entity id.")
        assignment_id = text(p, "assignmentId", max_len=64)
        reason = text(p, "reason", max_len=1000)
        assignment = _assignment_by_external(
            ctx.membership.school,
            assignment_id,
        )
        if assignment is None:
            raise Rejected("Teaching assignment does not exist.")
        previous = assignment.previous_assignment
        if (
            previous is None
            or previous.teacher_membership_id == assignment.teacher_membership_id
        ):
            raise Rejected(
                "No canonical teacher handover exists for this assignment."
            )
        version = serialize_teaching_assignment(assignment)["version"]
        return {
            "id": entity_id,
            "assignmentId": assignment.external_id,
            "className": assignment.class_subject.academic_class.name,
            "subject": assignment.class_subject.subject.name,
            "fromTeacherId": str(previous.teacher_membership_id),
            "toTeacherId": str(assignment.teacher_membership_id),
            "reason": reason,
            "transferredByMembershipId": str(ctx.membership.id),
            "transferredAt": ctx.now,
            "recordScope": _TRANSFER_SCOPE,
            "previousAssignmentVersion": max(1, version - 1),
            "newAssignmentVersion": version,
        }


class StudentSubjectSelectionHandler(EntityHandler):
    entity_type = STUDENT_SELECTION_ENTITY
    roles = _CURRICULUM_WRITE_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in _CURRICULUM_READ_ROLES else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the elective-selection entity id.")
        return {
            "id": entity_id,
            "studentId": text(p, "studentId", max_len=80),
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "selected": boolean(p, "selected"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        eligibility = set_student_elective(
            membership=ctx.membership,
            payload=stored,
        )
        canonical = {**stored, "eligibility": eligibility}
        _canonicalize_record(ctx, canonical)


class TeacherClassLinkHandler(EntityHandler):
    """Private read-only assignment link for exactly one Teacher membership."""

    entity_type = TEACHER_CLASS_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx) -> None:
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
    CurriculumTopicHandler(),
    TeachingAssignmentHandler(),
    TeachingTransferHandler(),
    StudentSubjectSelectionHandler(),
    TeacherClassLinkHandler(),
]
