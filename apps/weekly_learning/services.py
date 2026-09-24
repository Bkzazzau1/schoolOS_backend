from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.academics.models import (
    AcademicLifecycleStatus,
    AcademicTerm,
    ClassSubject,
    EnrollmentAcademicContext,
    TeachingAssignment,
)
from apps.core.errors import Rejected
from apps.lesson_attendance.models import LessonAttendanceState
from apps.lesson_delivery.models import (
    LessonDeliveryRecord,
    LessonDeliveryState,
    LessonPlan,
    LessonPlanState,
)
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink, Student
from apps.sync.models import SyncRecord

from .models import WeeklyLearningPublication, WeeklyLearningState, WeeklyLearningUpdate


WEEKLY_LEARNING_ENTITY = "teacher_weekly_learning_update"


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _class_subject(school, value):
    try:
        item = (
            ClassSubject.objects.select_related(
                "session",
                "academic_class",
                "subject",
            )
            .get(id=value, session__school=school)
        )
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class subject does not exist in this school.")
    if not item.is_active or not item.subject.is_active or not item.academic_class.is_active:
        raise Rejected("Weekly learning requires an active class curriculum subject.")
    return item


def _term(class_subject, value):
    try:
        item = AcademicTerm.objects.select_related("session").get(
            id=value,
            session=class_subject.session,
        )
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not belong to this class subject session.")
    if item.status != AcademicLifecycleStatus.ACTIVE:
        raise Rejected("Weekly learning can be changed only in the active academic term.")
    return item


def _week_bounds(term, raw):
    start = parse_date(str(raw or ""))
    if start is None:
        raise Rejected("weekStart must be a valid date.")
    if start.weekday() != 0:
        raise Rejected("weekStart must be a Monday.")
    end = start + timedelta(days=6)
    if end < term.starts_on or start > term.ends_on:
        raise Rejected("Weekly learning week falls outside the academic term.")
    if start > timezone.localdate():
        raise Rejected("A future academic week cannot be reported yet.")
    return start, end


def _current_teacher(class_subject):
    assignment = (
        TeachingAssignment.objects.filter(
            class_subject=class_subject,
            ended_at__isnull=True,
        )
        .select_related("teacher_membership__user")
        .first()
    )
    if (
        assignment is None
        or not assignment.teacher_membership.is_active
        or assignment.teacher_membership.role != Role.TEACHER
    ):
        raise Rejected("This class subject has no active Teacher authority.")
    return assignment.teacher_membership


def _live_evidence(class_subject, term, week_start, week_end):
    start = max(week_start, term.starts_on)
    end = min(week_end, term.ends_on)

    plans = list(
        LessonPlan.objects.filter(
            class_subject=class_subject,
            lesson_date__gte=start,
            lesson_date__lte=end,
            state=LessonPlanState.APPROVED,
        )
        .select_related("curriculum_topic")
        .order_by("lesson_date", "timetable_entry__starts_at")
    )
    deliveries = list(
        LessonDeliveryRecord.objects.filter(
            timetable_entry__class_subject=class_subject,
            lesson_date__gte=start,
            lesson_date__lte=end,
            state=LessonDeliveryState.DELIVERED,
        )
        .select_related(
            "curriculum_topic",
            "teacher_membership__user",
            "attendance_register",
        )
        .prefetch_related("attendance_register__entries")
        .order_by("lesson_date", "timetable_entry__starts_at")
    )

    planned_topics = []
    planned_seen = set()
    for plan in plans:
        topic_id = str(plan.curriculum_topic_id)
        if topic_id in planned_seen:
            continue
        planned_seen.add(topic_id)
        planned_topics.append(
            {
                "id": topic_id,
                "title": plan.curriculum_topic.title,
                "sequence": plan.curriculum_topic.sequence,
            }
        )

    topic_map = {}
    lesson_dates = []
    attendance_counts = {
        "total": 0,
        "present": 0,
        "absent": 0,
        "late": 0,
        "excused": 0,
        "unmarked": 0,
    }
    attendance_register_ids = []

    for delivery in deliveries:
        lesson_date = delivery.lesson_date.isoformat()
        if lesson_date not in lesson_dates:
            lesson_dates.append(lesson_date)
        topic_id = str(delivery.curriculum_topic_id)
        topic = topic_map.setdefault(
            topic_id,
            {
                "id": topic_id,
                "title": delivery.curriculum_topic.title,
                "sequence": delivery.curriculum_topic.sequence,
                "deliveredLessons": 0,
                "completed": False,
                "deliveryIds": [],
            },
        )
        topic["deliveredLessons"] += 1
        topic["completed"] = topic["completed"] or delivery.topic_completed
        topic["deliveryIds"].append(delivery.external_id)

        attendance = delivery.attendance_register
        if attendance is None or attendance.state != LessonAttendanceState.SUBMITTED:
            continue
        attendance_register_ids.append(attendance.external_id)
        statuses = [entry.status for entry in attendance.entries.all()]
        attendance_counts["total"] += len(statuses)
        for status in ("present", "absent", "late", "excused", "unmarked"):
            attendance_counts[status] += statuses.count(status)

    delivered_topics = sorted(
        topic_map.values(),
        key=lambda item: (item["sequence"], item["title"]),
    )
    completed_topics = [item for item in delivered_topics if item["completed"]]
    covered_summary = ", ".join(item["title"] for item in delivered_topics)
    if not covered_summary:
        covered_summary = "No server-accepted delivered lesson evidence yet."

    evidence_parts = [f"{len(deliveries)} delivered lesson(s)"]
    if attendance_counts["total"]:
        evidence_parts.append(
            f"attendance: {attendance_counts['present']} present, "
            f"{attendance_counts['late']} late, "
            f"{attendance_counts['absent']} absent, "
            f"{attendance_counts['excused']} excused"
        )

    return {
        "approvedPlans": len(plans),
        "plannedTopics": planned_topics,
        "deliveredLessons": len(deliveries),
        "deliveredTopics": delivered_topics,
        "completedTopics": completed_topics,
        "lessonDates": lesson_dates,
        "deliveryIds": [item.external_id for item in deliveries],
        "attendanceRegisterIds": attendance_register_ids,
        "attendance": attendance_counts,
        "coveredSummary": covered_summary,
        "evidenceSummary": " · ".join(evidence_parts),
    }


def _serialize_live(item):
    class_subject = item.class_subject
    current_teacher = None
    assignment = (
        TeachingAssignment.objects.filter(
            class_subject=class_subject,
            ended_at__isnull=True,
        )
        .select_related("teacher_membership__user")
        .first()
    )
    if assignment is not None and assignment.teacher_membership.is_active:
        current_teacher = assignment.teacher_membership

    evidence = _live_evidence(
        class_subject,
        item.term,
        item.week_start,
        item.week_end,
    )
    return {
        "id": item.external_id,
        "canonicalUpdateId": str(item.id),
        "sessionId": str(class_subject.session_id),
        "termId": str(item.term_id),
        "term": item.term.name,
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "section": class_subject.academic_class.section,
        "subjectId": str(class_subject.subject_id),
        "subject": class_subject.subject.name,
        "weekStart": item.week_start.isoformat(),
        "weekEnd": item.week_end.isoformat(),
        "weekLabel": f"{item.week_start.isoformat()} – {item.week_end.isoformat()}",
        "state": item.state,
        "authorMembershipId": str(item.author_membership_id),
        "author": _membership_name(item.author_membership),
        "currentTeacherId": str(current_teacher.id) if current_teacher else "",
        "currentTeacher": _membership_name(current_teacher),
        "nextFocus": item.next_focus,
        "supportNote": item.support_note,
        "parentNote": item.parent_note,
        "version": item.version,
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
        "publishedByMembershipId": str(item.published_by_id) if item.published_by_id else None,
        "updatedAt": item.updated_at.isoformat(),
        "planned": ", ".join(topic["title"] for topic in evidence["plannedTopics"]),
        "covered": evidence["coveredSummary"],
        "evidence": evidence["evidenceSummary"],
        "evidenceDetails": evidence,
    }


def serialize_update(item):
    if item.state == WeeklyLearningState.PUBLISHED:
        publication = item.publications.order_by("-revision").first()
        if publication is not None:
            return publication.snapshot
    return _serialize_live(item)


def _sync_record(*, school, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=school,
            entity_type=WEEKLY_LEARNING_ENTITY,
            entity_id=entity_id,
        )
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school,
            entity_type=WEEKLY_LEARNING_ENTITY,
            entity_id=entity_id,
            payload=payload,
            version=1,
            deleted=False,
            updated_by=actor,
        )
    if record.payload == payload and not record.deleted:
        return record
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])
    return record


def replace_current_sync_payload(*, school, entity_id, payload, actor=None):
    SyncRecord.objects.filter(
        school=school,
        entity_type=WEEKLY_LEARNING_ENTITY,
        entity_id=entity_id,
    ).update(payload=payload, deleted=False, updated_by=actor)


@transaction.atomic
def publish_update(item, *, actor=None):
    item = WeeklyLearningUpdate.objects.select_related(
        "school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "term",
        "author_membership__user",
        "published_by__user",
    ).get(pk=item.pk)
    return _sync_record(
        school=item.school,
        entity_id=item.external_id,
        payload=serialize_update(item),
        actor=actor,
    )


@transaction.atomic
def upsert_update(*, membership: Membership, payload: dict, publish_sync=True):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can write weekly learning updates.")

    school = membership.school
    class_subject = _class_subject(school, payload["classSubjectId"])
    term = _term(class_subject, payload["termId"])
    week_start, week_end = _week_bounds(term, payload["weekStart"])
    current_teacher = _current_teacher(class_subject)
    if current_teacher.id != membership.id:
        raise Rejected("This Teacher is not the current authority for that class subject.")

    existing = (
        WeeklyLearningUpdate.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    identity_existing = (
        WeeklyLearningUpdate.objects.select_for_update()
        .filter(
            class_subject=class_subject,
            term=term,
            week_start=week_start,
        )
        .exclude(external_id=payload["id"])
        .first()
    )
    if identity_existing is not None:
        raise Rejected("This class subject already has a weekly update for that week.")
    if existing is not None:
        if (
            existing.class_subject_id != class_subject.id
            or existing.term_id != term.id
            or existing.week_start != week_start
        ):
            raise Rejected("A weekly learning update cannot be moved to another subject, term or week.")
        if existing.state == WeeklyLearningState.PUBLISHED:
            raise Rejected("Published weekly learning is historical and cannot be silently rewritten.")

    action = payload["action"]
    evidence = _live_evidence(class_subject, term, week_start, week_end)
    if action == "publish":
        if evidence["deliveredLessons"] < 1:
            raise Rejected("Publish only after at least one server-accepted lesson was delivered this week.")
        if not payload["nextFocus"].strip():
            raise Rejected("Add the next learning focus before publishing to families.")

    now = timezone.now()
    if existing is None:
        item = WeeklyLearningUpdate.objects.create(
            school=school,
            external_id=payload["id"],
            class_subject=class_subject,
            term=term,
            week_start=week_start,
            week_end=week_end,
            author_membership=membership,
            last_edited_by=membership,
            published_by=membership if action == "publish" else None,
            state=WeeklyLearningState.PUBLISHED if action == "publish" else WeeklyLearningState.DRAFT,
            next_focus=payload["nextFocus"].strip(),
            support_note=payload["supportNote"].strip(),
            parent_note=payload["parentNote"].strip(),
            version=1,
            published_at=now if action == "publish" else None,
        )
    else:
        item = existing
        item.last_edited_by = membership
        item.next_focus = payload["nextFocus"].strip()
        item.support_note = payload["supportNote"].strip()
        item.parent_note = payload["parentNote"].strip()
        item.version += 1
        if action == "publish":
            item.state = WeeklyLearningState.PUBLISHED
            item.published_by = membership
            item.published_at = now
        item.save(
            update_fields=[
                "last_edited_by",
                "next_focus",
                "support_note",
                "parent_note",
                "version",
                "state",
                "published_by",
                "published_at",
                "updated_at",
            ]
        )

    if action == "publish":
        snapshot = _serialize_live(item)
        WeeklyLearningPublication.objects.create(
            update=item,
            revision=1,
            snapshot=snapshot,
            published_by=membership,
        )

    if publish_sync:
        publish_update(item, actor=membership)
    return item


def _enrollment_overlap_filter(week_start, week_end):
    return Q(enrollment__started_at__date__lte=week_end) & (
        Q(enrollment__ended_at__isnull=True)
        | Q(enrollment__ended_at__date__gte=week_start)
    )


def parent_can_view_payload(membership, payload):
    if membership.role != Role.PARENT or payload.get("state") != WeeklyLearningState.PUBLISHED:
        return False
    week_start = parse_date(str(payload.get("weekStart") or ""))
    week_end = parse_date(str(payload.get("weekEnd") or ""))
    if week_start is None or week_end is None:
        return False
    linked_student_ids = GuardianLink.objects.filter(
        account_user=membership.user,
        student__school=membership.school,
    ).values_list("student_id", flat=True)
    return EnrollmentAcademicContext.objects.filter(
        enrollment__student_id__in=linked_student_ids,
        session_id=payload.get("sessionId"),
        academic_class_id=payload.get("classId"),
    ).filter(_enrollment_overlap_filter(week_start, week_end)).exists()


def student_can_view_payload(membership, payload):
    if membership.role != Role.STUDENT or payload.get("state") != WeeklyLearningState.PUBLISHED:
        return False
    week_start = parse_date(str(payload.get("weekStart") or ""))
    week_end = parse_date(str(payload.get("weekEnd") or ""))
    if week_start is None or week_end is None:
        return False
    student_ids = Student.objects.filter(
        school=membership.school,
        account_user=membership.user,
    ).values_list("id", flat=True)
    return EnrollmentAcademicContext.objects.filter(
        enrollment__student_id__in=student_ids,
        session_id=payload.get("sessionId"),
        academic_class_id=payload.get("classId"),
    ).filter(_enrollment_overlap_filter(week_start, week_end)).exists()
