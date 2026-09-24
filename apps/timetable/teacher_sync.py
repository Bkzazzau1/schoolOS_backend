from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.academics.models import AcademicLifecycleStatus, TeachingAssignment
from apps.lesson_attendance.services import (
    effective_teacher_for_occurrence,
    eligible_student_payload,
)
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .models import TimetableEntry, TimetableOverride
from .services import serialize_entry, serialize_override


TEACHER_TIMETABLE_LINK_ENTITY = "teacher_timetable_schedule"


def _week_bounds():
    today = timezone.localdate()
    week_start = today - timedelta(days=today.isoweekday() - 1)
    return week_start, week_start + timedelta(days=6)


def _current_week_occurrence_date(item: TimetableEntry):
    week_start, _ = _week_bounds()
    lesson_date = week_start + timedelta(days=item.day_of_week - 1)
    if lesson_date < item.term.starts_on or lesson_date > item.term.ends_on:
        return None
    return lesson_date


def _teacher_entry_payload(item: TimetableEntry) -> dict:
    payload = serialize_entry(item)
    payload["eligibleStudents"] = eligible_student_payload(item.class_subject)
    occurrence_date = _current_week_occurrence_date(item)
    payload["eligibleStudentsByDate"] = {}
    if occurrence_date is not None:
        payload["eligibleStudentsByDate"] = {
            occurrence_date.isoformat(): eligible_student_payload(
                item.class_subject,
                on_date=occurrence_date,
            )
        }
    return payload


def _teacher_override_payload(item: TimetableOverride) -> dict:
    payload = serialize_override(item)
    lesson = payload["lesson"]
    lesson["eligibleStudents"] = eligible_student_payload(
        item.timetable_entry.class_subject
    )
    lesson["eligibleStudentsByDate"] = {
        item.lesson_date.isoformat(): eligible_student_payload(
            item.timetable_entry.class_subject,
            on_date=item.lesson_date,
        )
    }
    return payload


def _attendance_occurrences(teacher: Membership) -> list[dict]:
    """Resolve this week's lesson authority date-by-date for one Teacher."""

    week_start, week_end = _week_bounds()
    assignment_subject_ids = TeachingAssignment.objects.filter(
        teacher_membership=teacher,
        started_at__date__lte=week_end,
    ).filter(
        Q(ended_at__isnull=True) | Q(ended_at__date__gte=week_start)
    ).values_list("class_subject_id", flat=True)

    entries = (
        TimetableEntry.objects.filter(
            school=teacher.school,
            term__status=AcademicLifecycleStatus.ACTIVE,
            is_active=True,
        )
        .filter(
            Q(class_subject_id__in=assignment_subject_ids)
            | Q(
                overrides__substitute_teacher_membership=teacher,
                overrides__lesson_date__gte=week_start,
                overrides__lesson_date__lte=week_end,
            )
        )
        .select_related(
            "school",
            "term__session",
            "class_subject__session",
            "class_subject__academic_class",
            "class_subject__subject",
        )
        .distinct()
    )

    occurrences = []
    for item in entries:
        lesson_date = week_start + timedelta(days=item.day_of_week - 1)
        if lesson_date < item.term.starts_on or lesson_date > item.term.ends_on:
            continue
        effective_teacher, override = effective_teacher_for_occurrence(
            item,
            lesson_date,
        )
        if effective_teacher is None or effective_teacher.id != teacher.id:
            continue

        payload = serialize_entry(item)
        payload["lessonDate"] = lesson_date.isoformat()
        payload["teacherId"] = str(teacher.id)
        payload["eligibleStudents"] = eligible_student_payload(
            item.class_subject,
            on_date=lesson_date,
        )
        if override is not None:
            if override.room:
                payload["room"] = override.room
            payload["status"] = (
                "substitution"
                if override.substitute_teacher_membership_id
                else "scheduled"
            )
            payload["note"] = override.note
        else:
            payload["status"] = "scheduled"
            payload["note"] = None
        occurrences.append(payload)

    occurrences.sort(
        key=lambda item: (
            item["lessonDate"],
            item["startTime"],
            item["periodNumber"],
            item["className"],
        )
    )
    return occurrences


def teacher_timetable_payload(teacher: Membership) -> dict:
    """Private current-term schedule for exactly one Teacher membership.

    `entries`/`overrides` power the timetable screen. `attendanceOccurrences`
    resolves one week's lesson ownership explicitly by date. The accompanying
    week key lets clients reject an old occurrence snapshot after a week rolls
    over and safely fall back to recurring schedule authority.
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
                "class_subject__session",
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
            "timetable_entry__class_subject__session",
            "timetable_entry__class_subject__academic_class",
            "timetable_entry__class_subject__subject",
            "substitute_teacher_membership__user",
        )
        .order_by("lesson_date", "timetable_entry__starts_at")
    )
    week_start, _ = _week_bounds()

    return {
        "teacherMembershipId": str(teacher.id),
        "entries": [_teacher_entry_payload(item) for item in entries],
        "overrides": [_teacher_override_payload(item) for item in overrides],
        "attendanceWeekStart": week_start.isoformat(),
        "attendanceOccurrences": (
            _attendance_occurrences(teacher)
            if teacher.role == Role.TEACHER and teacher.is_active
            else []
        ),
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
