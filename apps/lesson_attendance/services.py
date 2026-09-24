from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.academics.models import (
    AcademicLifecycleStatus,
    CurriculumRequirement,
    CurriculumTopic,
    EnrollmentAcademicContext,
    TeachingAssignment,
)
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.students.models import EnrollmentStatus
from apps.sync.models import SyncRecord
from apps.timetable.models import TimetableEntry, TimetableOverride
from apps.timetable.services import serialize_entry

from .models import (
    AttendanceMark,
    LessonAttendanceEntry,
    LessonAttendanceRegister,
    LessonAttendanceState,
)


LESSON_ATTENDANCE_ENTITY = "teacher_lesson_attendance_register"


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def eligible_students_for_class_subject(class_subject, *, on_date=None):
    """Return the canonical roster eligible for this class-subject.

    Compulsory subjects inherit the class roster. Electives require an explicit
    StudentSubjectSelection on the same immutable enrollment context.
    """

    contexts = EnrollmentAcademicContext.objects.filter(
        session=class_subject.session,
        academic_class=class_subject.academic_class,
    ).select_related("enrollment__student")
    if on_date is None:
        contexts = contexts.filter(enrollment__status=EnrollmentStatus.ACTIVE)
    else:
        contexts = contexts.filter(enrollment__started_at__date__lte=on_date).filter(
            Q(enrollment__ended_at__isnull=True)
            | Q(enrollment__ended_at__date__gte=on_date)
        )
    if class_subject.requirement == CurriculumRequirement.ELECTIVE:
        contexts = contexts.filter(subject_selections__class_subject=class_subject)

    students = [context.enrollment.student for context in contexts.distinct()]
    students.sort(key=lambda item: (item.full_name.casefold(), item.student_code.casefold()))
    return students


def eligible_student_payload(class_subject, *, on_date=None):
    return [
        {
            "studentId": student.student_code,
            "studentName": student.full_name,
            "admissionNumber": student.admission_number,
        }
        for student in eligible_students_for_class_subject(
            class_subject,
            on_date=on_date,
        )
    ]


def effective_teacher_for_occurrence(entry, lesson_date):
    override = (
        TimetableOverride.objects.filter(
            timetable_entry=entry,
            lesson_date=lesson_date,
        )
        .select_related("substitute_teacher_membership__user")
        .first()
    )
    if override is not None:
        if override.is_cancelled:
            return None, override
        if override.substitute_teacher_membership_id:
            return override.substitute_teacher_membership, override

    assignment = (
        TeachingAssignment.objects.filter(
            class_subject=entry.class_subject,
            started_at__date__lte=lesson_date,
        )
        .filter(Q(ended_at__isnull=True) | Q(ended_at__date__gte=lesson_date))
        .select_related("teacher_membership__user")
        .order_by("-started_at", "-created_at")
        .first()
    )
    return (assignment.teacher_membership if assignment else None), override


def _entry_for_school(school, external_id):
    item = (
        TimetableEntry.objects.filter(school=school, external_id=external_id)
        .select_related(
            "school",
            "term__session",
            "class_subject__session",
            "class_subject__academic_class",
            "class_subject__subject",
        )
        .first()
    )
    if item is None:
        raise Rejected("Timetable lesson does not exist in this school.")
    return item


def _topic(entry, value):
    if not value:
        return None
    try:
        item = CurriculumTopic.objects.get(id=value)
    except (CurriculumTopic.DoesNotExist, ValueError, TypeError):
        raise Rejected("Curriculum topic does not exist.")
    if item.class_subject_id != entry.class_subject_id or item.term_id != entry.term_id:
        raise Rejected("Curriculum topic does not belong to this lesson's class-subject and term.")
    return item


def _lesson_date(entry, raw):
    value = parse_date(str(raw or ""))
    if value is None:
        raise Rejected("lessonDate must be a valid date.")
    if value > timezone.localdate():
        raise Rejected("Attendance cannot be recorded for a future lesson occurrence.")
    if value < entry.term.starts_on or value > entry.term.ends_on:
        raise Rejected("Lesson date falls outside the timetable term.")
    if value.isoweekday() != entry.day_of_week:
        raise Rejected("Lesson date does not match the recurring timetable weekday.")
    return value


def _canonical_entries_payload(register):
    return [
        {
            "studentId": item.student_code,
            "studentName": item.student_name,
            "status": item.status,
            "note": item.note,
        }
        for item in register.entries.select_related("student").all()
    ]


def serialize_register(register):
    lesson = serialize_entry(register.timetable_entry)
    topic = register.curriculum_topic
    return {
        "id": register.external_id,
        "canonicalRegisterId": str(register.id),
        "timetableEntryId": register.timetable_entry.external_id,
        "lessonDate": register.lesson_date.isoformat(),
        "sessionId": lesson["sessionId"],
        "termId": lesson["termId"],
        "term": lesson["term"],
        "classSubjectId": lesson["classSubjectId"],
        "classId": lesson["classId"],
        "className": lesson["className"],
        "section": lesson["section"],
        "subjectId": lesson["subjectId"],
        "subject": lesson["subject"],
        "periodNumber": lesson["periodNumber"],
        "startTime": lesson["startTime"],
        "endTime": lesson["endTime"],
        "time": lesson["time"],
        "room": lesson["room"],
        "teacherId": str(register.teacher_membership_id),
        "teacher": _membership_name(register.teacher_membership),
        "topicId": str(topic.id) if topic else "",
        "topic": topic.title if topic else "",
        "state": register.state,
        "submittedAt": register.submitted_at.isoformat() if register.submitted_at else None,
        "submittedByMembershipId": str(register.submitted_by_id) if register.submitted_by_id else None,
        "entries": _canonical_entries_payload(register),
    }


def _sync_record_payload(register, *, actor=None):
    payload = serialize_register(register)
    SyncRecord.objects.filter(
        school=register.school,
        entity_type=LESSON_ATTENDANCE_ENTITY,
        entity_id=register.external_id,
    ).update(payload=payload, updated_by=actor)
    return payload


@transaction.atomic
def upsert_register(*, membership: Membership, payload: dict):
    school = membership.school
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can record subject attendance.")

    entry = _entry_for_school(school, payload["timetableEntryId"])
    lesson_date = _lesson_date(entry, payload["lessonDate"])
    existing = (
        LessonAttendanceRegister.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .select_related("timetable_entry", "teacher_membership", "curriculum_topic")
        .first()
    )
    occurrence_existing = (
        LessonAttendanceRegister.objects.select_for_update()
        .filter(timetable_entry=entry, lesson_date=lesson_date)
        .exclude(external_id=payload["id"])
        .first()
    )
    if occurrence_existing is not None:
        raise Rejected("This timetable occurrence already has an attendance register.")
    if existing is not None:
        if existing.timetable_entry_id != entry.id or existing.lesson_date != lesson_date:
            raise Rejected("An attendance register cannot be moved to another lesson occurrence.")
        if existing.state == LessonAttendanceState.SUBMITTED:
            raise Rejected("Submitted subject attendance is historical and cannot be rewritten.")

    if entry.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed term's subject attendance is historical and cannot be changed.")
    if not entry.is_active and existing is None:
        raise Rejected("Attendance cannot be opened for an inactive timetable lesson.")

    teacher, override = effective_teacher_for_occurrence(entry, lesson_date)
    if override is not None and override.is_cancelled:
        raise Rejected("Attendance cannot be recorded for a cancelled lesson occurrence.")
    if teacher is None:
        raise Rejected("This lesson occurrence has no authorized Teacher assignment.")
    if teacher.id != membership.id:
        raise Rejected("This Teacher membership is not authorized for this lesson occurrence.")

    topic = _topic(entry, payload.get("topicId"))
    requested_state = payload["state"]
    roster = eligible_students_for_class_subject(entry.class_subject, on_date=lesson_date)
    roster_by_code = {student.student_code: student for student in roster}

    supplied = {}
    for raw in payload.get("entries", []):
        student_code = str(raw.get("studentId") or "").strip()
        if not student_code or student_code in supplied:
            raise Rejected("Attendance entries must contain unique studentId values.")
        if student_code not in roster_by_code:
            raise Rejected("Attendance contains a student who is not eligible for this subject occurrence.")
        status = str(raw.get("status") or AttendanceMark.UNMARKED)
        if status not in AttendanceMark.values:
            raise Rejected("Attendance status is invalid.")
        note = str(raw.get("note") or "").strip()
        if len(note) > 500:
            raise Rejected("Attendance note is too long.")
        supplied[student_code] = (status, note)

    if requested_state == LessonAttendanceState.SUBMITTED:
        missing = [
            student.student_code
            for student in roster
            if student.student_code not in supplied
            or supplied[student.student_code][0] == AttendanceMark.UNMARKED
        ]
        if missing:
            raise Rejected("Every eligible student must have an explicit attendance status before submission.")

    now = timezone.now()
    if existing is None:
        register = LessonAttendanceRegister.objects.create(
            school=school,
            external_id=payload["id"],
            timetable_entry=entry,
            lesson_date=lesson_date,
            teacher_membership=membership,
            curriculum_topic=topic,
            state=requested_state,
            submitted_at=now if requested_state == LessonAttendanceState.SUBMITTED else None,
            submitted_by=membership if requested_state == LessonAttendanceState.SUBMITTED else None,
        )
    else:
        register = existing
        register.teacher_membership = membership
        register.curriculum_topic = topic
        register.state = requested_state
        if requested_state == LessonAttendanceState.SUBMITTED:
            register.submitted_at = now
            register.submitted_by = membership
        register.save(
            update_fields=[
                "teacher_membership",
                "curriculum_topic",
                "state",
                "submitted_at",
                "submitted_by",
                "updated_at",
            ]
        )

    valid_student_ids = []
    existing_marks = {
        item.student_code: item
        for item in register.entries.select_related("student").all()
    }
    for student in roster:
        valid_student_ids.append(student.id)
        status, note = supplied.get(
            student.student_code,
            (
                existing_marks.get(student.student_code).status
                if student.student_code in existing_marks
                else AttendanceMark.UNMARKED,
                existing_marks.get(student.student_code).note
                if student.student_code in existing_marks
                else "",
            ),
        )
        LessonAttendanceEntry.objects.update_or_create(
            register=register,
            student=student,
            defaults={
                "student_code": student.student_code,
                "student_name": student.full_name,
                "status": status,
                "note": note,
            },
        )
    register.entries.exclude(student_id__in=valid_student_ids).delete()

    register = LessonAttendanceRegister.objects.select_related(
        "school",
        "timetable_entry__school",
        "timetable_entry__term__session",
        "timetable_entry__class_subject__session",
        "timetable_entry__class_subject__academic_class",
        "timetable_entry__class_subject__subject",
        "teacher_membership__user",
        "curriculum_topic",
        "submitted_by",
    ).get(pk=register.pk)
    _sync_record_payload(register, actor=membership)
    return register
