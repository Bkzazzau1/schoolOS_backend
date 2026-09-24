from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.academics.models import (
    AcademicLifecycleStatus,
    AcademicTerm,
    ClassSubject,
    CurriculumTopic,
    TeachingAssignment,
)
from apps.core.errors import Rejected
from apps.lesson_attendance.services import eligible_students_for_class_subject
from apps.schools.models import Membership, Role
from apps.students.models import Student
from apps.sync.models import SyncRecord

from .models import (
    Assignment,
    AssignmentGradeEvent,
    AssignmentPublication,
    AssignmentRecipient,
    AssignmentState,
    AssignmentSubmission,
    AssignmentSubmissionVersion,
    AssignmentType,
    SubmissionState,
)


ASSIGNMENT_ENTITY = "academic_assignment"
SUBMISSION_ENTITY = "student_assignment_submission"


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
                "session", "academic_class", "subject"
            )
            .get(id=value, session__school=school)
        )
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class subject does not exist in this school.")
    if not item.is_active or not item.academic_class.is_active or not item.subject.is_active:
        raise Rejected("Assignments require an active class curriculum subject.")
    return item


def _term(class_subject, value, *, publishing=False):
    try:
        item = AcademicTerm.objects.select_related("session").get(
            id=value, session=class_subject.session
        )
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not belong to this class-subject session.")
    if item.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Assignments cannot be changed in a closed academic term.")
    if publishing and item.status != AcademicLifecycleStatus.ACTIVE:
        raise Rejected("Assignments can be published only in the active academic term.")
    return item


def _topic(class_subject, term, value):
    if not value:
        return None
    try:
        item = CurriculumTopic.objects.get(
            id=value, class_subject=class_subject, term=term
        )
    except (CurriculumTopic.DoesNotExist, ValueError, TypeError):
        raise Rejected("Curriculum topic does not belong to this class subject and term.")
    return item


def current_teacher(class_subject, *, required=True):
    assignment = (
        TeachingAssignment.objects.filter(
            class_subject=class_subject, ended_at__isnull=True
        )
        .select_related("teacher_membership__user")
        .first()
    )
    member = assignment.teacher_membership if assignment is not None else None
    if (
        member is None
        or not member.is_active
        or member.role != Role.TEACHER
    ):
        if required:
            raise Rejected("This class subject has no active Teacher authority.")
        return None
    return member


def _assert_teacher_authority(membership, class_subject):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can manage assignments.")
    teacher = current_teacher(class_subject)
    if teacher.id != membership.id:
        raise Rejected("This Teacher is not the current authority for that class subject.")
    return teacher


def _student_for_membership(membership):
    if membership.role != Role.STUDENT or not membership.is_active:
        raise Rejected("Only an active Student membership can change assignment submissions.")
    item = Student.objects.filter(
        school=membership.school,
        account_user=membership.user,
    ).first()
    if item is None:
        raise Rejected("This Student membership is not linked to a canonical student record.")
    return item


def _parse_due(raw, term, *, required=False):
    if raw in (None, ""):
        if required:
            raise Rejected("A due date and time are required before publication.")
        return None
    value = parse_datetime(str(raw))
    if value is None:
        raise Rejected("dueAt must be an ISO date-time.")
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    if value.date() < term.starts_on or value.date() > term.ends_on:
        raise Rejected("Assignment due time must fall inside the academic term.")
    return value


def _score(raw, maximum_score):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise Rejected("score must be a valid number.")
    if value < 0 or value > Decimal(maximum_score):
        raise Rejected("score must be between zero and the assignment maximum score.")
    return value


def _publication_revision(item):
    publication = item.publications.order_by("-revision").first()
    return publication.revision if publication is not None else 0


def serialize_assignment(item):
    class_subject = item.class_subject
    teacher = current_teacher(class_subject, required=False)
    total = item.recipients.count()
    submissions = item.submissions.exclude(state=SubmissionState.DRAFT)
    submitted = submissions.count()
    graded = submissions.filter(state=SubmissionState.GRADED).count()
    late = submissions.filter(is_late=True).count()
    topic = item.curriculum_topic
    return {
        "id": item.external_id,
        "canonicalAssignmentId": str(item.id),
        "sessionId": str(class_subject.session_id),
        "termId": str(item.term_id),
        "term": item.term.name,
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "section": class_subject.academic_class.section,
        "subjectId": str(class_subject.subject_id),
        "subject": class_subject.subject.name,
        "topicId": str(topic.id) if topic else "",
        "topic": topic.title if topic else "",
        "type": item.assignment_type,
        "state": item.state,
        "title": item.title,
        "instructions": item.instructions,
        "dueAt": item.due_at.isoformat() if item.due_at else None,
        "maximumScore": item.maximum_score,
        "version": item.version,
        "publicationRevision": _publication_revision(item),
        "authorMembershipId": str(item.author_membership_id),
        "author": _membership_name(item.author_membership),
        "currentTeacherId": str(teacher.id) if teacher else "",
        "currentTeacher": _membership_name(teacher),
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
        "publishedByMembershipId": str(item.published_by_id) if item.published_by_id else None,
        "closedAt": item.closed_at.isoformat() if item.closed_at else None,
        "totalStudents": total,
        "submissions": submitted,
        "marked": graded,
        "unmarked": max(0, submitted - graded),
        "lateSubmissions": late,
        "updatedAt": item.updated_at.isoformat(),
    }


def serialize_submission(item):
    assignment = item.assignment
    class_subject = assignment.class_subject
    return {
        "id": item.external_id,
        "canonicalSubmissionId": str(item.id),
        "assignmentId": assignment.external_id,
        "assignmentCanonicalId": str(assignment.id),
        "assignmentRevision": _publication_revision(assignment),
        "title": assignment.title,
        "type": assignment.assignment_type,
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "section": class_subject.academic_class.section,
        "subjectId": str(class_subject.subject_id),
        "subject": class_subject.subject.name,
        "dueAt": assignment.due_at.isoformat() if assignment.due_at else None,
        "maximumScore": assignment.maximum_score,
        "studentId": item.student.student_code,
        "studentName": item.student.full_name,
        "admissionNumber": item.student.admission_number,
        "state": item.state,
        "responseText": item.response_text,
        "attemptNumber": item.attempt_number,
        "submittedAt": item.submitted_at.isoformat() if item.submitted_at else None,
        "isLate": item.is_late,
        "score": float(item.score) if item.score is not None else None,
        "feedback": item.feedback,
        "gradedByMembershipId": str(item.graded_by_id) if item.graded_by_id else None,
        "gradedBy": _membership_name(item.graded_by),
        "gradedAt": item.graded_at.isoformat() if item.graded_at else None,
        "version": item.version,
        "updatedAt": item.updated_at.isoformat(),
    }


def _sync_record(*, school, entity_type, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=entity_type, entity_id=entity_id)
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
    SyncRecord.objects.filter(
        school=school,
        entity_type=entity_type,
        entity_id=entity_id,
    ).update(payload=payload, deleted=False, updated_by=actor)


def _loaded_assignment(school, external_id, *, lock=False):
    query = Assignment.objects.select_related(
        "school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "term",
        "curriculum_topic",
        "author_membership__user",
        "published_by__user",
    )
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("Assignment does not exist in this school.")
    return item


def _loaded_submission(school, external_id, *, lock=False):
    query = AssignmentSubmission.objects.select_related(
        "school",
        "assignment__class_subject__session",
        "assignment__class_subject__academic_class",
        "assignment__class_subject__subject",
        "assignment__term",
        "assignment__author_membership__user",
        "recipient",
        "student",
        "graded_by__user",
    )
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("Assignment submission does not exist in this school.")
    return item


def publish_assignment_sync(item, *, actor=None):
    item = _loaded_assignment(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_type=ASSIGNMENT_ENTITY,
        entity_id=item.external_id,
        payload=serialize_assignment(item),
        actor=actor,
    )


def publish_submission_sync(item, *, actor=None):
    item = _loaded_submission(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_type=SUBMISSION_ENTITY,
        entity_id=item.external_id,
        payload=serialize_submission(item),
        actor=actor,
    )


def _publication_snapshot(item):
    payload = serialize_assignment(item)
    # Operational counters are live and are not part of the content revision.
    for key in ("submissions", "marked", "unmarked", "lateSubmissions", "updatedAt"):
        payload.pop(key, None)
    return payload


@transaction.atomic
def upsert_assignment(*, membership: Membership, payload: dict, publish_sync=True):
    school = membership.school
    action = payload["action"]
    class_subject = _class_subject(school, payload["classSubjectId"])
    publishing = action in {"publish", "revise"}
    term = _term(class_subject, payload["termId"], publishing=publishing)
    teacher = _assert_teacher_authority(membership, class_subject)
    topic = _topic(class_subject, term, payload.get("topicId"))

    existing = (
        Assignment.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    if existing is not None:
        if existing.class_subject_id != class_subject.id or existing.term_id != term.id:
            raise Rejected("An assignment cannot be moved to another class subject or term.")
        if existing.state == AssignmentState.CLOSED:
            raise Rejected("A closed assignment is historical and cannot be rewritten.")
        if action == "saveDraft" and existing.state != AssignmentState.DRAFT:
            raise Rejected("Published assignments require an explicit audited revision.")
        if action == "publish" and existing.state != AssignmentState.DRAFT:
            raise Rejected("Only a synchronized draft can be published.")
        if action == "revise" and existing.state != AssignmentState.PUBLISHED:
            raise Rejected("Only a published assignment can be revised.")
        if action == "close" and existing.state != AssignmentState.PUBLISHED:
            raise Rejected("Only a published assignment can be closed.")

    assignment_type = payload["type"]
    if assignment_type not in AssignmentType.values:
        raise Rejected("Unsupported assignment type.")
    title = payload.get("title", "").strip()
    instructions = payload.get("instructions", "").strip()
    maximum_score = int(payload.get("maximumScore") or 0)
    due_at = _parse_due(payload.get("dueAt"), term, required=publishing)

    if action in {"publish", "revise"}:
        if not title or not instructions:
            raise Rejected("Add a title and instructions before publication.")
        if maximum_score <= 0:
            raise Rejected("maximumScore must be greater than zero before publication.")
        if due_at <= timezone.now():
            raise Rejected("Assignment due time must be in the future when published or revised.")

    now = timezone.now()
    if existing is None:
        if action != "saveDraft":
            raise Rejected("Create and synchronize the assignment draft before publication.")
        item = Assignment.objects.create(
            school=school,
            external_id=payload["id"],
            class_subject=class_subject,
            term=term,
            curriculum_topic=topic,
            author_membership=membership,
            last_edited_by=membership,
            assignment_type=assignment_type,
            state=AssignmentState.DRAFT,
            title=title,
            instructions=instructions,
            due_at=due_at,
            maximum_score=maximum_score,
            version=1,
        )
    else:
        item = existing
        if action == "saveDraft":
            item.curriculum_topic = topic
            item.assignment_type = assignment_type
            item.title = title
            item.instructions = instructions
            item.due_at = due_at
            item.maximum_score = maximum_score
            item.last_edited_by = membership
            item.version += 1
            item.save()
        elif action == "publish":
            item.curriculum_topic = topic
            item.assignment_type = assignment_type
            item.title = title
            item.instructions = instructions
            item.due_at = due_at
            item.maximum_score = maximum_score
            item.last_edited_by = membership
            item.published_by = membership
            item.published_at = now
            item.state = AssignmentState.PUBLISHED
            item.version += 1
            item.save()

            students = eligible_students_for_class_subject(
                class_subject, on_date=timezone.localdate()
            )
            if not students:
                raise Rejected("This class subject has no eligible students to receive the assignment.")
            AssignmentRecipient.objects.bulk_create(
                [
                    AssignmentRecipient(
                        assignment=item,
                        student=student,
                        student_code=student.student_code,
                        student_name=student.full_name,
                        admission_number=student.admission_number,
                    )
                    for student in students
                ]
            )
            AssignmentPublication.objects.create(
                assignment=item,
                revision=1,
                snapshot=_publication_snapshot(item),
                published_by=membership,
            )
        elif action == "revise":
            # Audience, subject/type/topic and marking scale are frozen at first publication.
            if assignment_type != item.assignment_type:
                raise Rejected("Published assignment type cannot be changed.")
            if topic != item.curriculum_topic:
                raise Rejected("Published assignment curriculum topic cannot be changed.")
            if maximum_score != item.maximum_score:
                raise Rejected("Published assignment maximum score cannot be changed.")
            item.title = title
            item.instructions = instructions
            item.due_at = due_at
            item.last_edited_by = membership
            item.version += 1
            item.save()
            revision = _publication_revision(item) + 1
            AssignmentPublication.objects.create(
                assignment=item,
                revision=revision,
                snapshot=_publication_snapshot(item),
                published_by=membership,
            )
        elif action == "close":
            item.state = AssignmentState.CLOSED
            item.closed_at = now
            item.closed_by = membership
            item.last_edited_by = membership
            item.version += 1
            item.save()

    if publish_sync:
        publish_assignment_sync(item, actor=teacher)
    return item


@transaction.atomic
def upsert_student_submission(*, membership: Membership, payload: dict, publish_sync=True):
    school = membership.school
    student = _student_for_membership(membership)
    assignment = _loaded_assignment(school, payload["assignmentId"], lock=True)
    if assignment.state != AssignmentState.PUBLISHED:
        raise Rejected("This assignment is not open for Student submission.")
    recipient = AssignmentRecipient.objects.filter(
        assignment=assignment, student=student
    ).first()
    if recipient is None:
        raise Rejected("This Student was not in the assignment's published recipient roster.")

    existing = (
        AssignmentSubmission.objects.select_for_update()
        .filter(assignment=assignment, student=student)
        .first()
    )
    by_external = (
        AssignmentSubmission.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    if by_external is not None and (
        existing is None or by_external.id != existing.id
    ):
        raise Rejected("Submission id is already used by another assignment response.")
    if existing is not None and existing.external_id != payload["id"]:
        raise Rejected("This Student already has a canonical submission for the assignment.")

    action = payload["action"]
    response_text = payload.get("responseText", "").strip()
    if action == "submit" and not response_text:
        raise Rejected("Add a response before submitting the assignment.")
    if existing is not None and existing.state in {
        SubmissionState.SUBMITTED,
        SubmissionState.GRADED,
    }:
        raise Rejected("Submitted work is locked unless the Teacher returns it for revision.")

    if existing is None:
        item = AssignmentSubmission.objects.create(
            school=school,
            external_id=payload["id"],
            assignment=assignment,
            recipient=recipient,
            student=student,
            response_text=response_text,
            state=SubmissionState.DRAFT,
            version=1,
        )
    else:
        item = existing
        item.response_text = response_text
        item.version += 1

    if action == "saveDraft":
        item.state = SubmissionState.DRAFT
        item.save()
    elif action == "submit":
        item.attempt_number += 1
        item.state = SubmissionState.SUBMITTED
        item.submitted_at = timezone.now()
        item.is_late = bool(assignment.due_at and item.submitted_at > assignment.due_at)
        item.score = None
        item.feedback = ""
        item.graded_by = None
        item.graded_at = None
        item.save()
        AssignmentSubmissionVersion.objects.create(
            submission=item,
            attempt_number=item.attempt_number,
            assignment_revision=max(1, _publication_revision(assignment)),
            snapshot={
                "responseText": item.response_text,
                "studentId": student.student_code,
                "studentName": student.full_name,
                "assignmentId": assignment.external_id,
                "isLate": item.is_late,
            },
        )
    else:
        raise Rejected("Unsupported Student submission action.")

    if publish_sync:
        publish_submission_sync(item, actor=membership)
        publish_assignment_sync(assignment, actor=membership)
    return item


@transaction.atomic
def mark_submission(*, membership: Membership, payload: dict, publish_sync=True):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can mark assignment submissions.")
    item = _loaded_submission(membership.school, payload["id"], lock=True)
    assignment = item.assignment
    _assert_teacher_authority(membership, assignment.class_subject)
    action = payload["action"]
    if item.state not in {SubmissionState.SUBMITTED, SubmissionState.GRADED}:
        raise Rejected("Only submitted or graded work can be marked or returned.")

    feedback = payload.get("feedback", "").strip()
    revision = item.grade_events.count() + 1
    if action == "return":
        if not feedback:
            raise Rejected("Add Teacher feedback before returning work for revision.")
        item.state = SubmissionState.RETURNED
        item.score = None
        item.feedback = feedback
        item.graded_by = membership
        item.graded_at = timezone.now()
        event_score = None
    elif action == "grade":
        score = _score(payload.get("score"), assignment.maximum_score)
        item.state = SubmissionState.GRADED
        item.score = score
        item.feedback = feedback
        item.graded_by = membership
        item.graded_at = timezone.now()
        event_score = score
    else:
        raise Rejected("Unsupported Teacher marking action.")

    item.version += 1
    item.save()
    AssignmentGradeEvent.objects.create(
        submission=item,
        revision=revision,
        action=action,
        score=event_score,
        feedback=feedback,
        actor_membership=membership,
    )

    if publish_sync:
        publish_submission_sync(item, actor=membership)
        publish_assignment_sync(assignment, actor=membership)
    return item
