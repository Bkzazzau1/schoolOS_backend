from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.academics.models import AcademicLifecycleStatus, CurriculumTopic, TeachingAssignment
from apps.core.errors import Rejected
from apps.lesson_attendance.models import LessonAttendanceRegister, LessonAttendanceState
from apps.lesson_attendance.services import effective_teacher_for_occurrence
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord
from apps.timetable.models import TimetableEntry
from apps.timetable.services import serialize_entry

from .models import (
    LessonDeliveryRecord,
    LessonDeliveryState,
    LessonPlan,
    LessonPlanReview,
    LessonPlanReviewDecision,
    LessonPlanState,
)


LESSON_PLAN_ENTITY = "teacher_lesson_plan"
LESSON_PLAN_REVIEW_ENTITY = "lesson_plan_review"
LESSON_DELIVERY_ENTITY = "lesson_delivery_record"
SYLLABUS_PROGRESS_ENTITY = "teacher_syllabus_progress"


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


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


def _lesson_date(entry, raw):
    value = parse_date(str(raw or ""))
    if value is None:
        raise Rejected("lessonDate must be a valid date.")
    if value < entry.term.starts_on or value > entry.term.ends_on:
        raise Rejected("Lesson date falls outside the timetable term.")
    if value.isoweekday() != entry.day_of_week:
        raise Rejected("Lesson date does not match the recurring timetable weekday.")
    return value


def _topic(entry, value):
    try:
        item = CurriculumTopic.objects.get(id=value)
    except (CurriculumTopic.DoesNotExist, ValueError, TypeError):
        raise Rejected("Curriculum topic does not exist.")
    if item.class_subject_id != entry.class_subject_id or item.term_id != entry.term_id:
        raise Rejected("Curriculum topic does not belong to this lesson occurrence.")
    return item


def _plan_for_school(school, external_id):
    item = (
        LessonPlan.objects.filter(school=school, external_id=external_id)
        .select_related(
            "school",
            "timetable_entry__term__session",
            "timetable_entry__class_subject__academic_class",
            "timetable_entry__class_subject__subject",
            "class_subject__academic_class",
            "class_subject__subject",
            "curriculum_topic",
            "author_membership__user",
            "last_edited_by__user",
            "submitted_by__user",
            "reviewed_by__user",
        )
        .first()
    )
    if item is None:
        raise Rejected("Lesson plan does not exist in this school.")
    return item


def _effective_teacher(entry, lesson_date):
    teacher, override = effective_teacher_for_occurrence(entry, lesson_date)
    if override is not None and override.is_cancelled:
        raise Rejected("This lesson occurrence is cancelled.")
    if teacher is None or not teacher.is_active:
        raise Rejected("This lesson occurrence has no active Teacher authority.")
    return teacher, override


def _state_for_client(value):
    if value == LessonPlanState.NEEDS_CHANGES:
        return "needsChanges"
    return value


def _occurrence_payload(entry, lesson_date):
    """Serialize an occurrence without requiring it to remain actionable."""

    payload = serialize_entry(entry)
    teacher, override = effective_teacher_for_occurrence(entry, lesson_date)
    payload["lessonDate"] = lesson_date.isoformat()
    if override is not None and override.is_cancelled:
        teacher = None
        status = "cancelled"
    elif override is not None and override.substitute_teacher_membership_id:
        status = "substitution"
    elif teacher is None:
        status = "uncovered"
    else:
        status = "scheduled"
    payload["effectiveTeacherId"] = str(teacher.id) if teacher else ""
    payload["effectiveTeacher"] = _membership_name(teacher)
    if override is not None:
        payload["room"] = override.room or entry.room
        payload["occurrenceNote"] = override.note
    else:
        payload["occurrenceNote"] = ""
    payload["occurrenceStatus"] = status
    return payload, teacher


def serialize_plan(plan):
    occurrence, effective_teacher = _occurrence_payload(
        plan.timetable_entry,
        plan.lesson_date,
    )
    topic = plan.curriculum_topic
    return {
        "id": plan.external_id,
        "canonicalPlanId": str(plan.id),
        "timetableEntryId": plan.timetable_entry.external_id,
        "lessonDate": plan.lesson_date.isoformat(),
        "sessionId": occurrence["sessionId"],
        "termId": occurrence["termId"],
        "term": occurrence["term"],
        "classSubjectId": str(plan.class_subject_id),
        "classId": occurrence["classId"],
        "className": occurrence["className"],
        "section": occurrence["section"],
        "subjectId": occurrence["subjectId"],
        "subject": occurrence["subject"],
        "periodNumber": occurrence["periodNumber"],
        "startTime": occurrence["startTime"],
        "endTime": occurrence["endTime"],
        "time": occurrence["time"],
        "room": occurrence["room"],
        "occurrenceStatus": occurrence["occurrenceStatus"],
        "effectiveTeacherId": str(effective_teacher.id) if effective_teacher else "",
        "effectiveTeacher": _membership_name(effective_teacher),
        "authorMembershipId": str(plan.author_membership_id),
        "author": _membership_name(plan.author_membership),
        "lastEditedByMembershipId": str(plan.last_edited_by_id) if plan.last_edited_by_id else None,
        "submittedByMembershipId": str(plan.submitted_by_id) if plan.submitted_by_id else None,
        "topicId": str(topic.id),
        "topic": topic.title,
        "topicSequence": topic.sequence,
        "state": _state_for_client(plan.state),
        "objectives": plan.objectives,
        "starter": plan.starter,
        "activities": plan.activities,
        "assessment": plan.assessment,
        "resources": plan.resources,
        "version": plan.version,
        "submittedAt": plan.submitted_at.isoformat() if plan.submitted_at else None,
        "reviewedAt": plan.reviewed_at.isoformat() if plan.reviewed_at else None,
        "reviewedByMembershipId": str(plan.reviewed_by_id) if plan.reviewed_by_id else None,
        "reviewComment": plan.review_comment,
        "updatedAt": plan.updated_at.isoformat(),
    }


def serialize_review(review):
    approved = review.decision == LessonPlanReviewDecision.APPROVED
    return {
        "id": review.external_id,
        "approvalId": f"{LESSON_PLAN_ENTITY}:{review.plan.external_id}:v{review.plan_version}",
        "planId": review.plan.external_id,
        "planVersion": review.plan_version,
        "previousStatus": "Pending",
        "newStatus": "Approved" if approved else "Returned",
        "reviewerMembershipId": str(review.reviewer_membership_id),
        "reviewedAt": review.reviewed_at.isoformat(),
        "comment": review.comment,
    }


def _attendance_for_occurrence(entry, lesson_date):
    return (
        LessonAttendanceRegister.objects.filter(
            timetable_entry=entry,
            lesson_date=lesson_date,
        )
        .select_related("submitted_by")
        .first()
    )


def serialize_delivery(item):
    occurrence, effective_teacher = _occurrence_payload(
        item.timetable_entry,
        item.lesson_date,
    )
    attendance = item.attendance_register
    attendance_entries = []
    if attendance is not None:
        attendance_entries = list(attendance.entries.values_list("status", flat=True))
    return {
        "id": item.external_id,
        "canonicalDeliveryId": str(item.id),
        "timetableEntryId": item.timetable_entry.external_id,
        "lessonDate": item.lesson_date.isoformat(),
        "planId": item.plan.external_id,
        "topicId": str(item.curriculum_topic_id),
        "topic": item.curriculum_topic.title,
        "classSubjectId": str(item.timetable_entry.class_subject_id),
        "className": occurrence["className"],
        "section": occurrence["section"],
        "subject": occurrence["subject"],
        "time": occurrence["time"],
        "room": occurrence["room"],
        "occurrenceStatus": occurrence["occurrenceStatus"],
        "teacherId": str(item.teacher_membership_id),
        "teacher": _membership_name(item.teacher_membership),
        "currentEffectiveTeacherId": str(effective_teacher.id) if effective_teacher else "",
        "state": item.state,
        "reflection": item.reflection,
        "homework": item.homework,
        "topicCompleted": item.topic_completed,
        "deliveredAt": item.delivered_at.isoformat() if item.delivered_at else None,
        "attendanceRegisterId": attendance.external_id if attendance else None,
        "attendanceState": attendance.state if attendance else None,
        "attendanceTotal": len(attendance_entries),
        "attendancePresent": attendance_entries.count("present"),
        "attendanceAbsent": attendance_entries.count("absent"),
        "attendanceLate": attendance_entries.count("late"),
        "updatedAt": item.updated_at.isoformat(),
    }


def serialize_topic_progress(topic):
    deliveries = list(
        LessonDeliveryRecord.objects.filter(
            curriculum_topic=topic,
            state=LessonDeliveryState.DELIVERED,
        )
        .select_related(
            "teacher_membership__user",
            "timetable_entry__class_subject__academic_class",
            "timetable_entry__class_subject__subject",
        )
        .order_by("lesson_date", "delivered_at")
    )
    if not deliveries:
        return None
    latest = deliveries[-1]
    completed = any(item.topic_completed for item in deliveries)
    return {
        "id": str(topic.id),
        "canonicalTopicId": str(topic.id),
        "classSubjectId": str(topic.class_subject_id),
        "className": topic.class_subject.academic_class.name,
        "subject": topic.class_subject.subject.name,
        "termId": str(topic.term_id),
        "week": topic.sequence,
        "topic": topic.title,
        "reportedStatus": "completed" if completed else "inProgress",
        "actorMembershipId": str(latest.teacher_membership_id),
        "version": len(deliveries),
        "updatedAt": (latest.delivered_at or latest.updated_at).isoformat(),
        "deliveredLessons": len(deliveries),
        "latestDeliveryDate": latest.lesson_date.isoformat(),
        "evidenceDeliveryIds": [item.external_id for item in deliveries],
    }


def _sync_record(*, school, entity_type, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=school,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school,
            entity_type=entity_type,
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


def replace_current_sync_payload(*, school, entity_type, entity_id, payload, actor=None):
    """Replace payload after handler validation without creating a second version.

    The generic sync engine has already accepted and versioned the mutation.
    This function swaps its cleaned client payload for the richer server-owned
    canonical representation inside the same transaction/version.
    """

    SyncRecord.objects.filter(
        school=school,
        entity_type=entity_type,
        entity_id=entity_id,
    ).update(payload=payload, deleted=False, updated_by=actor)


@transaction.atomic
def publish_plan(plan, *, actor=None):
    plan = _plan_for_school(plan.school, plan.external_id)
    return _sync_record(
        school=plan.school,
        entity_type=LESSON_PLAN_ENTITY,
        entity_id=plan.external_id,
        payload=serialize_plan(plan),
        actor=actor,
    )


@transaction.atomic
def publish_review(review, *, actor=None):
    return _sync_record(
        school=review.school,
        entity_type=LESSON_PLAN_REVIEW_ENTITY,
        entity_id=review.external_id,
        payload=serialize_review(review),
        actor=actor,
    )


@transaction.atomic
def publish_delivery(item, *, actor=None):
    item = LessonDeliveryRecord.objects.select_related(
        "school",
        "timetable_entry__term__session",
        "timetable_entry__class_subject__academic_class",
        "timetable_entry__class_subject__subject",
        "plan",
        "curriculum_topic__class_subject__academic_class",
        "curriculum_topic__class_subject__subject",
        "teacher_membership__user",
        "attendance_register",
    ).get(pk=item.pk)
    return _sync_record(
        school=item.school,
        entity_type=LESSON_DELIVERY_ENTITY,
        entity_id=item.external_id,
        payload=serialize_delivery(item),
        actor=actor,
    )


@transaction.atomic
def publish_topic_progress(topic, *, actor=None):
    topic = CurriculumTopic.objects.select_related(
        "term",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
    ).get(pk=topic.pk)
    payload = serialize_topic_progress(topic)
    if payload is None:
        return None
    return _sync_record(
        school=topic.class_subject.session.school,
        entity_type=SYLLABUS_PROGRESS_ENTITY,
        entity_id=str(topic.id),
        payload=payload,
        actor=actor,
    )


@transaction.atomic
def upsert_plan(*, membership: Membership, payload: dict, publish_sync=True):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can author lesson plans.")
    school = membership.school
    entry = _entry_for_school(school, payload["timetableEntryId"])
    lesson_date = _lesson_date(entry, payload["lessonDate"])
    if entry.term.status != AcademicLifecycleStatus.ACTIVE:
        raise Rejected("Lesson plans can be changed only in the active academic term.")
    if not entry.is_active:
        raise Rejected("Lesson plans require an active timetable lesson.")
    effective_teacher, _ = _effective_teacher(entry, lesson_date)
    if effective_teacher.id != membership.id:
        raise Rejected("This Teacher is not authorized for that lesson occurrence.")
    topic = _topic(entry, payload["topicId"])

    existing = (
        LessonPlan.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    occurrence_existing = (
        LessonPlan.objects.select_for_update()
        .filter(timetable_entry=entry, lesson_date=lesson_date)
        .exclude(external_id=payload["id"])
        .first()
    )
    if occurrence_existing is not None:
        raise Rejected("This timetable occurrence already has a lesson plan.")
    if existing is not None:
        if existing.timetable_entry_id != entry.id or existing.lesson_date != lesson_date:
            raise Rejected("A lesson plan cannot be moved to another occurrence.")
        if existing.curriculum_topic_id != topic.id:
            raise Rejected("A lesson plan topic cannot be replaced after the plan is created.")
        if existing.state in {LessonPlanState.SUBMITTED, LessonPlanState.APPROVED}:
            raise Rejected("Submitted or approved lesson plans are locked for Teacher editing.")

    action = payload["action"]
    if action == "submit":
        if not payload["objectives"].strip() or not payload["activities"].strip() or not payload["assessment"].strip():
            raise Rejected("Add objectives, teaching activities and assessment evidence before submission.")
        target_state = LessonPlanState.SUBMITTED
    else:
        target_state = existing.state if existing is not None and existing.state == LessonPlanState.NEEDS_CHANGES else LessonPlanState.DRAFT

    now = timezone.now()
    if existing is None:
        item = LessonPlan.objects.create(
            school=school,
            external_id=payload["id"],
            timetable_entry=entry,
            lesson_date=lesson_date,
            class_subject=entry.class_subject,
            curriculum_topic=topic,
            author_membership=membership,
            last_edited_by=membership,
            submitted_by=membership if action == "submit" else None,
            state=target_state,
            objectives=payload["objectives"],
            starter=payload["starter"],
            activities=payload["activities"],
            assessment=payload["assessment"],
            resources=payload["resources"],
            version=1,
            submitted_at=now if action == "submit" else None,
        )
    else:
        item = existing
        item.last_edited_by = membership
        item.state = target_state
        item.objectives = payload["objectives"]
        item.starter = payload["starter"]
        item.activities = payload["activities"]
        item.assessment = payload["assessment"]
        item.resources = payload["resources"]
        item.version += 1
        if action == "submit":
            item.submitted_by = membership
            item.submitted_at = now
            item.reviewed_at = None
            item.reviewed_by = None
            item.review_comment = ""
        item.save(
            update_fields=[
                "last_edited_by",
                "state",
                "objectives",
                "starter",
                "activities",
                "assessment",
                "resources",
                "version",
                "submitted_by",
                "submitted_at",
                "reviewed_at",
                "reviewed_by",
                "review_comment",
                "updated_at",
            ]
        )
    if publish_sync:
        publish_plan(item, actor=membership)
    return item


@transaction.atomic
def review_plan(*, membership: Membership, payload: dict, publish_review_sync=True):
    if membership.role != Role.PRINCIPAL or not membership.is_active:
        raise Rejected("Only an active Principal can review Secondary lesson plans.")
    plan = _plan_for_school(membership.school, payload["planId"])
    if plan.class_subject.academic_class.section.strip().casefold() != "secondary":
        raise Rejected("Principal lesson-plan review is limited to Secondary.")
    if plan.state != LessonPlanState.SUBMITTED:
        raise Rejected("Only the current submitted lesson-plan version can be reviewed.")
    if payload["planVersion"] != plan.version:
        raise Rejected("The lesson plan changed. Reload before reviewing it.")
    if payload["decision"] == LessonPlanReviewDecision.NEEDS_CHANGES and not payload["comment"].strip():
        raise Rejected("Explain the changes needed before returning the lesson plan.")

    review = LessonPlanReview.objects.create(
        school=membership.school,
        external_id=payload["id"],
        plan=plan,
        reviewer_membership=membership,
        plan_version=plan.version,
        decision=payload["decision"],
        comment=payload["comment"].strip(),
    )
    plan.state = (
        LessonPlanState.APPROVED
        if review.decision == LessonPlanReviewDecision.APPROVED
        else LessonPlanState.NEEDS_CHANGES
    )
    plan.reviewed_at = review.reviewed_at
    plan.reviewed_by = membership
    plan.review_comment = review.comment
    plan.save(
        update_fields=[
            "state",
            "reviewed_at",
            "reviewed_by",
            "review_comment",
            "updated_at",
        ]
    )
    if publish_review_sync:
        publish_review(review, actor=membership)
    # Review changes the plan independently of a Teacher plan mutation, so the
    # plan SyncRecord receives its own new canonical version here.
    publish_plan(plan, actor=membership)
    return review


@transaction.atomic
def upsert_delivery(*, membership: Membership, payload: dict, publish_sync=True):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher can record lesson delivery.")
    school = membership.school
    entry = _entry_for_school(school, payload["timetableEntryId"])
    lesson_date = _lesson_date(entry, payload["lessonDate"])
    if lesson_date > timezone.localdate():
        raise Rejected("A future lesson occurrence cannot be recorded as delivered.")
    if entry.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Closed-term lesson delivery is historical and cannot be changed.")
    teacher, _ = _effective_teacher(entry, lesson_date)
    if teacher.id != membership.id:
        raise Rejected("This Teacher is not authorized for that lesson occurrence.")
    plan = _plan_for_school(school, payload["planId"])
    if plan.timetable_entry_id != entry.id or plan.lesson_date != lesson_date:
        raise Rejected("Lesson delivery must use the plan for the same timetable occurrence.")
    if plan.state != LessonPlanState.APPROVED:
        raise Rejected("Lesson delivery requires an approved lesson plan.")

    existing = (
        LessonDeliveryRecord.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    occurrence_existing = (
        LessonDeliveryRecord.objects.select_for_update()
        .filter(timetable_entry=entry, lesson_date=lesson_date)
        .exclude(external_id=payload["id"])
        .first()
    )
    if occurrence_existing is not None:
        raise Rejected("This timetable occurrence already has a lesson-delivery record.")
    if existing is not None:
        if existing.timetable_entry_id != entry.id or existing.lesson_date != lesson_date:
            raise Rejected("A lesson-delivery record cannot be moved to another occurrence.")
        if existing.state == LessonDeliveryState.DELIVERED:
            raise Rejected("Delivered lesson evidence is historical and cannot be rewritten.")

    action = payload["action"]
    target_state = LessonDeliveryState.DELIVERED if action == "deliver" else LessonDeliveryState.DRAFT
    attendance = _attendance_for_occurrence(entry, lesson_date)
    if attendance is not None and attendance.state != LessonAttendanceState.SUBMITTED:
        attendance = None
    now = timezone.now()
    if existing is None:
        item = LessonDeliveryRecord.objects.create(
            school=school,
            external_id=payload["id"],
            timetable_entry=entry,
            lesson_date=lesson_date,
            plan=plan,
            curriculum_topic=plan.curriculum_topic,
            teacher_membership=membership,
            attendance_register=attendance,
            state=target_state,
            reflection=payload["reflection"],
            homework=payload["homework"],
            topic_completed=payload["topicCompleted"],
            delivered_at=now if target_state == LessonDeliveryState.DELIVERED else None,
        )
    else:
        item = existing
        item.teacher_membership = membership
        item.attendance_register = attendance
        item.state = target_state
        item.reflection = payload["reflection"]
        item.homework = payload["homework"]
        item.topic_completed = payload["topicCompleted"]
        if target_state == LessonDeliveryState.DELIVERED:
            item.delivered_at = now
        item.save(
            update_fields=[
                "teacher_membership",
                "attendance_register",
                "state",
                "reflection",
                "homework",
                "topic_completed",
                "delivered_at",
                "updated_at",
            ]
        )
    if publish_sync:
        publish_delivery(item, actor=membership)
    if item.state == LessonDeliveryState.DELIVERED:
        publish_topic_progress(item.curriculum_topic, actor=membership)
    return item


def teacher_can_view_progress(membership, class_subject_id):
    return TeachingAssignment.objects.filter(
        class_subject_id=class_subject_id,
        teacher_membership=membership,
        ended_at__isnull=True,
    ).exists()


@transaction.atomic
def refresh_occurrence_records(entry, lesson_date=None, *, actor=None):
    plans = LessonPlan.objects.filter(timetable_entry=entry)
    deliveries = LessonDeliveryRecord.objects.filter(timetable_entry=entry)
    if lesson_date is not None:
        plans = plans.filter(lesson_date=lesson_date)
        deliveries = deliveries.filter(lesson_date=lesson_date)
    for plan in plans:
        publish_plan(plan, actor=actor)
    for item in deliveries:
        publish_delivery(item, actor=actor)
