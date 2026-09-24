from django.db import transaction
from django.db.models import Q

from apps.academics.models import AcademicLifecycleStatus, TeachingAssignment
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .models import TimetableEntry, TimetableOverride
from .services import serialize_entry, serialize_override


TEACHER_TIMETABLE_LINK_ENTITY = "teacher_timetable_schedule"


def teacher_timetable_payload(teacher: Membership) -> dict:
    """Private current-term schedule for exactly one Teacher membership.

    The whole payload is replaced whenever schedule authority changes. This is
    deliberate: a handover must remove lessons from the previous teacher's
    device instead of merely making future global records invisible to them.
    """

    entries = TimetableEntry.objects.none()
    if teacher.role == Role.TEACHER and teacher.is_active:
        assigned_subject_ids = TeachingAssignment.objects.filter(
            teacher_membership=teacher,
            ended_at__isnull=True,
            class_subject__is_active=True,
        ).values_list("class_subject_id", flat=True)
        entries = (
            TimetableEntry.objects.filter(
                school=teacher.school,
                term__status=AcademicLifecycleStatus.ACTIVE,
                class_subject_id__in=assigned_subject_ids,
                is_active=True,
            )
            .select_related(
                "school",
                "term__session",
                "class_subject__academic_class",
                "class_subject__subject",
            )
            .order_by("day_of_week", "starts_at", "period_number")
        )

    entry_ids = list(entries.values_list("id", flat=True))
    overrides = (
        TimetableOverride.objects.filter(
            school=teacher.school,
            timetable_entry__term__status=AcademicLifecycleStatus.ACTIVE,
        )
        .filter(
            Q(timetable_entry_id__in=entry_ids)
            | Q(substitute_teacher_membership=teacher)
        )
        .select_related(
            "timetable_entry__school",
            "timetable_entry__term__session",
            "timetable_entry__class_subject__academic_class",
            "timetable_entry__class_subject__subject",
            "substitute_teacher_membership__user",
        )
        .order_by("lesson_date", "timetable_entry__starts_at")
    )

    return {
        "teacherMembershipId": str(teacher.id),
        "entries": [serialize_entry(item) for item in entries],
        "overrides": [serialize_override(item) for item in overrides],
    }


@transaction.atomic
def publish_teacher_timetable_link(teacher: Membership, *, actor=None) -> None:
    if teacher.role != Role.TEACHER:
        return
    payload = teacher_timetable_payload(teacher)
    entity_id = str(teacher.id)
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=teacher.school,
            entity_type=TEACHER_TIMETABLE_LINK_ENTITY,
            entity_id=entity_id,
        )
        .first()
    )
    if record is None:
        SyncRecord.objects.create(
            school=teacher.school,
            entity_type=TEACHER_TIMETABLE_LINK_ENTITY,
            entity_id=entity_id,
            payload=payload,
            version=1,
            deleted=False,
            updated_by=actor,
        )
        return
    if record.payload == payload and not record.deleted:
        return
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])


@transaction.atomic
def publish_school_teacher_timetable_links(school, *, actor=None) -> None:
    for teacher in Membership.objects.filter(
        school=school,
        role=Role.TEACHER,
    ).select_related("school", "user"):
        publish_teacher_timetable_link(teacher, actor=actor)
