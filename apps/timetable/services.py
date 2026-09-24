from django.db import transaction
from django.utils.dateparse import parse_date, parse_time

from apps.academics.models import AcademicLifecycleStatus, AcademicTerm, ClassSubject, TeachingAssignment
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .models import TimetableEntry, TimetableOverride


TIMETABLE_ENTRY_ENTITY = "academic_timetable_entry"
TIMETABLE_OVERRIDE_ENTITY = "academic_timetable_override"


def _term(school, value):
    try:
        item = AcademicTerm.objects.select_related("session").get(id=value)
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not exist.")
    if item.session.school_id != school.id:
        raise Rejected("Academic term does not belong to this school.")
    return item


def _class_subject(school, value):
    try:
        item = ClassSubject.objects.select_related(
            "session", "academic_class", "subject"
        ).get(id=value)
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class-subject requirement does not exist.")
    if item.session.school_id != school.id:
        raise Rejected("Class-subject requirement does not belong to this school.")
    return item


def _entry(school, external_id):
    item = (
        TimetableEntry.objects.select_related(
            "school",
            "term__session",
            "class_subject__session",
            "class_subject__academic_class",
            "class_subject__subject",
        )
        .filter(school=school, external_id=external_id)
        .first()
    )
    if item is None:
        raise Rejected("Timetable entry does not exist in this school.")
    return item


def _teacher_membership(school, value):
    if not value:
        return None
    try:
        item = Membership.objects.select_related("user", "school").get(id=value)
    except (Membership.DoesNotExist, ValueError, TypeError):
        raise Rejected("Substitute Teacher membership does not exist.")
    if item.school_id != school.id or item.role != Role.TEACHER or not item.is_active:
        raise Rejected("Choose an active Teacher membership in this school.")
    return item


def _active_assignment(class_subject):
    return (
        TeachingAssignment.objects.filter(
            class_subject=class_subject,
            ended_at__isnull=True,
            teacher_membership__is_active=True,
        )
        .select_related("teacher_membership__user")
        .first()
    )


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _times(payload):
    starts_at = parse_time(str(payload.get("startTime") or ""))
    ends_at = parse_time(str(payload.get("endTime") or ""))
    if starts_at is None or ends_at is None:
        raise Rejected("startTime and endTime must be valid 24-hour times.")
    if starts_at >= ends_at:
        raise Rejected("A timetable lesson must end after it starts.")
    return starts_at, ends_at


def _overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def _entry_status(item):
    assignment = _active_assignment(item.class_subject)
    if assignment is None:
        return "uncovered"

    teacher_id = assignment.teacher_membership_id
    class_id = item.class_subject.academic_class_id
    room = item.room.strip().casefold()
    peers = (
        TimetableEntry.objects.filter(
            term=item.term,
            day_of_week=item.day_of_week,
            is_active=True,
        )
        .exclude(pk=item.pk)
        .select_related("class_subject__academic_class")
    )
    for peer in peers:
        if not _overlaps(item.starts_at, item.ends_at, peer.starts_at, peer.ends_at):
            continue
        same_class = peer.class_subject.academic_class_id == class_id
        same_room = bool(room and peer.room.strip().casefold() == room)
        peer_assignment = _active_assignment(peer.class_subject)
        same_teacher = bool(
            peer_assignment is not None
            and peer_assignment.teacher_membership_id == teacher_id
        )
        if same_class or same_room or same_teacher:
            return "clash"
    return "scheduled"


def serialize_entry(item):
    assignment = _active_assignment(item.class_subject)
    teacher = assignment.teacher_membership if assignment is not None else None
    return {
        "id": item.external_id,
        "canonicalTimetableEntryId": str(item.id),
        "sessionId": str(item.term.session_id),
        "termId": str(item.term_id),
        "term": item.term.name,
        "classSubjectId": str(item.class_subject_id),
        "classId": str(item.class_subject.academic_class_id),
        "className": item.class_subject.academic_class.name,
        "section": item.class_subject.academic_class.section,
        "subjectId": str(item.class_subject.subject_id),
        "subject": item.class_subject.subject.name,
        "teacherId": str(teacher.id) if teacher else "",
        "teacher": _membership_name(teacher),
        "dayOfWeek": item.day_of_week,
        "day": item.get_day_of_week_display(),
        "periodNumber": item.period_number,
        "startTime": item.starts_at.strftime("%H:%M"),
        "endTime": item.ends_at.strftime("%H:%M"),
        "time": f"{item.starts_at:%H:%M}–{item.ends_at:%H:%M}",
        "room": item.room,
        "status": _entry_status(item),
        "isActive": item.is_active,
    }


def serialize_override(item):
    base = serialize_entry(item.timetable_entry)
    original_teacher_id = base["teacherId"]
    substitute = item.substitute_teacher_membership
    effective_teacher_id = str(substitute.id) if substitute else original_teacher_id
    effective_teacher = _membership_name(substitute) if substitute else base["teacher"]
    if item.is_cancelled:
        status = "cancelled"
    elif substitute is not None:
        status = "substitution"
    else:
        status = base["status"]
    return {
        "id": item.external_id,
        "canonicalTimetableOverrideId": str(item.id),
        "timetableEntryId": item.timetable_entry.external_id,
        "lessonDate": item.lesson_date.isoformat(),
        "originalTeacherId": original_teacher_id,
        "teacherId": effective_teacher_id,
        "teacher": effective_teacher,
        "room": item.room or item.timetable_entry.room,
        "note": item.note,
        "status": status,
        "isCancelled": item.is_cancelled,
        "lesson": base,
    }


def _sync_record(*, school, entity_type, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=entity_type, entity_id=entity_id)
        .first()
    )
    if record is None:
        SyncRecord.objects.create(
            school=school,
            entity_type=entity_type,
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
def refresh_entry_sync(item, *, actor=None):
    item = TimetableEntry.objects.select_related(
        "school",
        "term__session",
        "class_subject__academic_class",
        "class_subject__subject",
    ).get(pk=item.pk)
    _sync_record(
        school=item.school,
        entity_type=TIMETABLE_ENTRY_ENTITY,
        entity_id=item.external_id,
        payload=serialize_entry(item),
        actor=actor,
    )


@transaction.atomic
def refresh_term_sync(term, *, actor=None, exclude_external_id=None):
    entries = TimetableEntry.objects.filter(term=term).select_related(
        "school",
        "term__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )
    for item in entries:
        if exclude_external_id and item.external_id == exclude_external_id:
            continue
        _sync_record(
            school=item.school,
            entity_type=TIMETABLE_ENTRY_ENTITY,
            entity_id=item.external_id,
            payload=serialize_entry(item),
            actor=actor,
        )


@transaction.atomic
def refresh_class_subject_sync(class_subject, *, actor=None):
    term_ids = set()
    for item in TimetableEntry.objects.filter(class_subject=class_subject).select_related("term"):
        term_ids.add(item.term_id)
    for term in AcademicTerm.objects.filter(id__in=term_ids):
        refresh_term_sync(term, actor=actor)


@transaction.atomic
def upsert_entry(*, membership, payload):
    school = membership.school
    term = _term(school, payload["termId"])
    class_subject = _class_subject(school, payload["classSubjectId"])
    if term.session_id != class_subject.session_id:
        raise Rejected("Timetable term must belong to the class-subject academic session.")
    if term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed term timetable is historical and cannot be changed.")
    if not class_subject.is_active or not class_subject.subject.is_active:
        raise Rejected("Timetable can use only an active class-subject requirement.")
    if not class_subject.academic_class.is_active:
        raise Rejected("Timetable can use only an active academic class.")

    starts_at, ends_at = _times(payload)
    existing = TimetableEntry.objects.select_for_update().filter(
        school=school, external_id=payload["id"]
    ).first()
    if existing is not None and (
        existing.term_id != term.id or existing.class_subject_id != class_subject.id
    ):
        raise Rejected("A timetable entry cannot move to another term or class-subject. Create a new entry.")

    active_for_subject = TimetableEntry.objects.select_for_update().filter(
        term=term,
        class_subject=class_subject,
        is_active=True,
    )
    if existing is not None:
        active_for_subject = active_for_subject.exclude(pk=existing.pk)
    if payload["isActive"] and active_for_subject.count() >= class_subject.periods_per_week:
        raise Rejected(
            "This class-subject already has its curriculum periods per week scheduled for this term."
        )

    class_peers = TimetableEntry.objects.select_for_update().filter(
        term=term,
        day_of_week=payload["dayOfWeek"],
        is_active=True,
        class_subject__academic_class=class_subject.academic_class,
    )
    if existing is not None:
        class_peers = class_peers.exclude(pk=existing.pk)
    if payload["isActive"]:
        for peer in class_peers:
            if _overlaps(starts_at, ends_at, peer.starts_at, peer.ends_at):
                raise Rejected("This class already has a timetable lesson during that time.")

    item, _ = TimetableEntry.objects.update_or_create(
        school=school,
        external_id=payload["id"],
        defaults={
            "term": term,
            "class_subject": class_subject,
            "day_of_week": payload["dayOfWeek"],
            "period_number": payload["periodNumber"],
            "starts_at": starts_at,
            "ends_at": ends_at,
            "room": payload["room"],
            "is_active": payload["isActive"],
            "created_by": existing.created_by if existing else membership,
        },
    )
    return item


@transaction.atomic
def upsert_override(*, membership, payload):
    school = membership.school
    entry = _entry(school, payload["timetableEntryId"])
    term = entry.term
    if term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed term timetable is historical and cannot receive overrides.")
    lesson_date = parse_date(str(payload.get("lessonDate") or ""))
    if lesson_date is None:
        raise Rejected("lessonDate must be a valid date.")
    if lesson_date < term.starts_on or lesson_date > term.ends_on:
        raise Rejected("Timetable override date must fall inside the academic term.")
    if lesson_date.isoweekday() != entry.day_of_week:
        raise Rejected("Timetable override date must match the recurring lesson weekday.")
    substitute = _teacher_membership(school, payload.get("substituteTeacherId"))

    existing = TimetableOverride.objects.select_for_update().filter(
        school=school, external_id=payload["id"]
    ).first()
    if existing is not None and (
        existing.timetable_entry_id != entry.id or existing.lesson_date != lesson_date
    ):
        raise Rejected("A timetable override cannot be moved to another lesson or date. Create a new override.")

    item, _ = TimetableOverride.objects.update_or_create(
        school=school,
        external_id=payload["id"],
        defaults={
            "timetable_entry": entry,
            "lesson_date": lesson_date,
            "substitute_teacher_membership": substitute,
            "room": payload["room"],
            "note": payload["note"],
            "is_cancelled": payload["isCancelled"],
            "created_by": existing.created_by if existing else membership,
        },
    )
    return item
