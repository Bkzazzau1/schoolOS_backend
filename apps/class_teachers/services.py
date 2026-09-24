import uuid

from django.db import transaction
from django.utils import timezone

from apps.academics.models import AcademicClass, AcademicLifecycleStatus, AcademicSession
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .models import ClassTeacherAssignment

CLASS_TEACHER_ENTITY = "class_teacher_assignment"
CLASS_TEACHER_LINK_ENTITY = "class_teacher_link"


def _assert_assignment_authority(membership: Membership, academic_class: AcademicClass):
    if membership.role not in {Role.PRINCIPAL, Role.PROPRIETOR} or not membership.is_active:
        raise Rejected("Only the Proprietor or Principal can assign a class teacher.")
    if membership.role == Role.PRINCIPAL and academic_class.section.strip().casefold() != "secondary":
        raise Rejected("This Principal can assign a class teacher only for Secondary classes.")


def _academic_class(school, value):
    try:
        item = AcademicClass.objects.get(id=value, school=school)
    except (AcademicClass.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class does not exist in this school.")
    if not item.is_active:
        raise Rejected("A class teacher can be assigned only to an active class.")
    return item


def _session(school, value):
    try:
        item = AcademicSession.objects.get(id=value, school=school)
    except (AcademicSession.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic session does not exist in this school.")
    if item.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A class teacher cannot be assigned in a closed academic session.")
    return item


def _teacher_membership(school, value):
    try:
        item = Membership.objects.get(id=value, school=school, role=Role.TEACHER, is_active=True)
    except (Membership.DoesNotExist, ValueError, TypeError):
        raise Rejected("Teacher membership does not exist in this school.")
    return item


def current_class_teacher(academic_class: AcademicClass, session: AcademicSession, *, required=True):
    assignment = (
        ClassTeacherAssignment.objects.filter(
            academic_class=academic_class, session=session, ended_at__isnull=True
        )
        .select_related("teacher_membership__user")
        .first()
    )
    member = assignment.teacher_membership if assignment is not None else None
    if member is None or not member.is_active or member.role != Role.TEACHER:
        if required:
            raise Rejected("This class has no active class-teacher authority.")
        return None
    return member


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _assignment_version(item: ClassTeacherAssignment) -> int:
    version = 1
    node = item.previous_assignment
    while node is not None:
        version += 1
        node = node.previous_assignment
    return version


def serialize_class_teacher_assignment(item: ClassTeacherAssignment) -> dict:
    return {
        "id": item.external_id,
        "canonicalAssignmentId": str(item.id),
        "classId": str(item.academic_class_id),
        "className": item.academic_class.name,
        "section": item.academic_class.section,
        "sessionId": str(item.session_id),
        "session": item.session.name,
        "teacherId": str(item.teacher_membership_id),
        "teacherName": _membership_name(item.teacher_membership),
        "version": _assignment_version(item),
        "startedAt": item.started_at.isoformat(),
        "endedAt": item.ended_at.isoformat() if item.ended_at else None,
    }


def _sync_record(*, school, entity_type, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=entity_type, entity_id=entity_id)
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school, entity_type=entity_type, entity_id=entity_id, payload=payload, updated_by=actor
        )
    if record.payload == payload and not record.deleted:
        return record
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])
    return record


def publish_class_teacher_link(teacher: Membership, *, actor=None):
    """A Teacher-readable roster of the classes they are currently the class
    teacher for, mirroring how TeacherRoster's own class-subject link works -
    the Teacher app needs some sync entity to discover this role at all."""
    classes = (
        ClassTeacherAssignment.objects.filter(teacher_membership=teacher, ended_at__isnull=True)
        .select_related("academic_class", "session")
        .order_by("academic_class__name")
    )
    payload = {
        "id": str(teacher.id),
        "classes": [
            {
                "classId": str(item.academic_class_id),
                "className": item.academic_class.name,
                "section": item.academic_class.section,
                "sessionId": str(item.session_id),
                "session": item.session.name,
            }
            for item in classes
        ],
    }
    return _sync_record(
        school=teacher.school, entity_type=CLASS_TEACHER_LINK_ENTITY, entity_id=str(teacher.id), payload=payload, actor=actor
    )


@transaction.atomic
def upsert_class_teacher_assignment(*, membership: Membership, payload: dict) -> ClassTeacherAssignment:
    school = membership.school
    academic_class = _academic_class(school, payload["classId"])
    _assert_assignment_authority(membership, academic_class)
    session = _session(school, payload["sessionId"])
    teacher = _teacher_membership(school, payload["teacherId"])

    existing = (
        ClassTeacherAssignment.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    if existing is not None and (
        existing.academic_class_id != academic_class.id or existing.session_id != session.id
    ):
        raise Rejected("An assignment cannot be moved to another class or session. Create a new assignment.")

    conflicting = ClassTeacherAssignment.objects.select_for_update().filter(
        academic_class=academic_class, session=session, ended_at__isnull=True
    )
    if existing is not None:
        conflicting = conflicting.exclude(pk=existing.pk)
    if conflicting.exists():
        raise Rejected("This class already has an active class teacher for this session.")

    now = timezone.now()
    old_teacher = None
    if existing is None:
        item = ClassTeacherAssignment.objects.create(
            school=school,
            external_id=payload["id"],
            academic_class=academic_class,
            session=session,
            teacher_membership=teacher,
            started_at=now,
            assigned_by=membership,
            handover_reason=payload.get("handoverReason", ""),
        )
    elif existing.teacher_membership_id == teacher.id:
        item = existing
    else:
        old_teacher = existing.teacher_membership
        archived = ClassTeacherAssignment.objects.create(
            school=school,
            external_id=f"hist-{uuid.uuid4()}",
            academic_class=existing.academic_class,
            session=existing.session,
            teacher_membership=existing.teacher_membership,
            started_at=existing.started_at,
            ended_at=now,
            assigned_by=existing.assigned_by,
            handover_reason=payload.get("handoverReason", ""),
            previous_assignment=existing.previous_assignment,
        )
        existing.teacher_membership = teacher
        existing.started_at = now
        existing.ended_at = None
        existing.assigned_by = membership
        existing.handover_reason = payload.get("handoverReason", "")
        existing.previous_assignment = archived
        existing.save(
            update_fields=[
                "teacher_membership",
                "started_at",
                "ended_at",
                "assigned_by",
                "handover_reason",
                "previous_assignment",
                "updated_at",
            ]
        )
        item = existing

    if old_teacher is not None:
        publish_class_teacher_link(old_teacher, actor=membership)
    publish_class_teacher_link(teacher, actor=membership)
    return item
