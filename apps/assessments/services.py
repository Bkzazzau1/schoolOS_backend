from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from apps.academics.models import AcademicLifecycleStatus, AcademicTerm, ClassSubject, TeachingAssignment
from apps.core.errors import Rejected
from apps.lesson_attendance.services import eligible_students_for_class_subject
from apps.schools.models import Membership, Role
from apps.students.models import Student
from apps.sync.models import SyncRecord

from .models import (
    AssessmentDefinition,
    AssessmentEvent,
    AssessmentRecipient,
    AssessmentScore,
    AssessmentState,
    AssessmentType,
)

ASSESSMENT_ENTITY = "academic_assessment"

# A single, school-wide default grading band. Every school on this deployment
# uses the same scale today; a per-school configurable scale is a reasonable
# later addition but is not built now (there is exactly one canonical scale
# to keep this the smallest correct model).
_GRADE_BANDS = (
    (Decimal("70"), "A"),
    (Decimal("60"), "B"),
    (Decimal("50"), "C"),
    (Decimal("45"), "D"),
    (Decimal("40"), "E"),
)


def grade_for_percent(percent: Decimal | None) -> str:
    if percent is None:
        return ""
    for threshold, letter in _GRADE_BANDS:
        if percent >= threshold:
            return letter
    return "F"


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _class_subject(school, value):
    try:
        item = ClassSubject.objects.select_related(
            "session", "academic_class", "subject"
        ).get(id=value, session__school=school)
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class subject does not exist in this school.")
    if not item.is_active or not item.academic_class.is_active or not item.subject.is_active:
        raise Rejected("Assessments require an active class curriculum subject.")
    return item


def _term(class_subject, value):
    try:
        item = AcademicTerm.objects.select_related("session").get(
            id=value, session=class_subject.session
        )
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not belong to this class-subject session.")
    if item.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Assessments cannot be changed in a closed academic term.")
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
    if member is None or not member.is_active or member.role != Role.TEACHER:
        if required:
            raise Rejected("This class subject has no active Teacher authority.")
        return None
    return member


def _assert_teacher_authority(membership, class_subject):
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can manage assessments.")
    teacher = current_teacher(class_subject)
    if teacher.id != membership.id:
        raise Rejected("This Teacher is not the current authority for that class subject.")
    return teacher


def _assert_release_authority(membership):
    if membership.role not in {Role.ADMINISTRATOR, Role.PROPRIETOR} or not membership.is_active:
        raise Rejected("Only the Proprietor or Administrator can lock or release assessment results.")


def _assert_principal_secondary_authority(membership, class_subject):
    if membership.role != Role.PRINCIPAL or not membership.is_active:
        raise Rejected("Only an active Principal membership can record an academic review.")
    if class_subject.academic_class.section.strip().casefold() != "secondary":
        raise Rejected("Principal academic review is limited to Secondary classes.")


def _score(raw, maximum_score):
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise Rejected("score must be a valid number.")
    if value < 0 or value > Decimal(maximum_score):
        raise Rejected("score must be between zero and the assessment maximum score.")
    return value


def _loaded_assessment(school, external_id, *, lock=False):
    query = AssessmentDefinition.objects.select_related(
        "school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "term",
        "author_membership__user",
    )
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("Assessment does not exist in this school.")
    return item


def _next_revision(item):
    latest = item.events.order_by("-revision").first()
    return (latest.revision if latest is not None else 0) + 1


def _append_event(item, *, actor, action, comment="", student=None, previous_score=None, new_score=None):
    AssessmentEvent.objects.create(
        assessment=item,
        revision=_next_revision(item),
        action=action,
        actor_membership=actor,
        comment=comment,
        student=student,
        previous_score=previous_score,
        new_score=new_score,
    )


def serialize_assessment(item):
    class_subject = item.class_subject
    teacher = current_teacher(class_subject, required=False)
    scores = {
        row.student_id: row
        for row in AssessmentScore.objects.filter(assessment=item).select_related("student")
    }
    entries = []
    entered = 0
    total_percent = Decimal("0")
    scored_count = 0
    for recipient in item.recipients.select_related("student").order_by("student_name", "student_code"):
        row = scores.get(recipient.student_id)
        score = row.score if row is not None else None
        percent = None
        if score is not None and item.maximum_score > 0:
            percent = (score / Decimal(item.maximum_score) * 100).quantize(Decimal("0.1"))
            total_percent += percent
            scored_count += 1
        if score is not None:
            entered += 1
        entries.append(
            {
                "studentId": recipient.student_code,
                "studentName": recipient.student_name,
                "admissionNumber": recipient.admission_number,
                "score": float(score) if score is not None else None,
                "percent": float(percent) if percent is not None else None,
                "grade": grade_for_percent(percent),
            }
        )
    average_percent = float(total_percent / scored_count) if scored_count else None

    return {
        "id": item.external_id,
        "canonicalAssessmentId": str(item.id),
        "sessionId": str(class_subject.session_id),
        "termId": str(item.term_id),
        "term": item.term.name,
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "section": class_subject.academic_class.section,
        "subjectId": str(class_subject.subject_id),
        "subject": class_subject.subject.name,
        "type": item.assessment_type,
        "state": item.state,
        "title": item.title,
        "maximumScore": item.maximum_score,
        "weight": float(item.weight),
        "version": item.version,
        "authorMembershipId": str(item.author_membership_id),
        "author": _membership_name(item.author_membership),
        "currentTeacherId": str(teacher.id) if teacher else "",
        "currentTeacher": _membership_name(teacher),
        "submittedAt": item.submitted_at.isoformat() if item.submitted_at else None,
        "submittedByMembershipId": str(item.submitted_by_id) if item.submitted_by_id else None,
        "lockedAt": item.locked_at.isoformat() if item.locked_at else None,
        "lockedByMembershipId": str(item.locked_by_id) if item.locked_by_id else None,
        "releasedAt": item.released_at.isoformat() if item.released_at else None,
        "releasedByMembershipId": str(item.released_by_id) if item.released_by_id else None,
        "totalStudents": len(entries),
        "entered": entered,
        "averagePercent": average_percent,
        "entries": entries,
        "updatedAt": item.updated_at.isoformat(),
    }


def _sync_record(*, school, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=ASSESSMENT_ENTITY, entity_id=entity_id)
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school,
            entity_type=ASSESSMENT_ENTITY,
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
        school=school, entity_type=ASSESSMENT_ENTITY, entity_id=entity_id
    ).update(payload=payload, deleted=False, updated_by=actor)


def publish_assessment_sync(item, *, actor=None):
    item = _loaded_assessment(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_id=item.external_id,
        payload=serialize_assessment(item),
        actor=actor,
    )


@transaction.atomic
def upsert_assessment(*, membership: Membership, payload: dict, publish_sync=True):
    """Teacher-authored draft creation, editing and publication.

    Publication freezes the eligible-student roster and the marking scale
    (type/maximum score/weight), exactly like Assignment publication.
    """
    school = membership.school
    action = payload["action"]
    class_subject = _class_subject(school, payload["classSubjectId"])
    term = _term(class_subject, payload["termId"])
    teacher = _assert_teacher_authority(membership, class_subject)

    existing = (
        AssessmentDefinition.objects.select_for_update()
        .filter(school=school, external_id=payload["id"])
        .first()
    )
    if existing is not None:
        if existing.class_subject_id != class_subject.id or existing.term_id != term.id:
            raise Rejected("An assessment cannot be moved to another class subject or term.")
        if action == "saveDraft" and existing.state != AssessmentState.DRAFT:
            raise Rejected("Published assessments require an explicit audited revision.")
        if action == "publish" and existing.state != AssessmentState.DRAFT:
            raise Rejected("Only a draft assessment can be published.")

    assessment_type = payload["type"]
    if assessment_type not in AssessmentType.values:
        raise Rejected("Unsupported assessment type.")
    title = payload.get("title", "").strip()
    maximum_score = int(payload.get("maximumScore") or 0)
    weight = _weight(payload.get("weight"))

    if action == "publish":
        if not title:
            raise Rejected("Add a title before publication.")
        if maximum_score <= 0:
            raise Rejected("maximumScore must be greater than zero before publication.")
        if weight <= 0:
            raise Rejected("weight must be greater than zero before publication.")

    now = timezone.now()
    if existing is None:
        if action != "saveDraft":
            raise Rejected("Create and synchronize the assessment draft before publication.")
        item = AssessmentDefinition.objects.create(
            school=school,
            external_id=payload["id"],
            class_subject=class_subject,
            term=term,
            author_membership=membership,
            last_edited_by=membership,
            assessment_type=assessment_type,
            state=AssessmentState.DRAFT,
            title=title,
            maximum_score=maximum_score,
            weight=weight,
            version=1,
        )
        _append_event(item, actor=membership, action="created")
    else:
        item = existing
        if action == "saveDraft":
            item.assessment_type = assessment_type
            item.title = title
            item.maximum_score = maximum_score
            item.weight = weight
            item.last_edited_by = membership
            item.version += 1
            item.save()
        elif action == "publish":
            item.assessment_type = assessment_type
            item.title = title
            item.maximum_score = maximum_score
            item.weight = weight
            item.last_edited_by = membership
            item.state = AssessmentState.PUBLISHED
            item.version += 1
            item.save()

            students = eligible_students_for_class_subject(
                class_subject, on_date=timezone.localdate()
            )
            if not students:
                raise Rejected("This class subject has no eligible students to assess.")
            recipients = AssessmentRecipient.objects.bulk_create(
                [
                    AssessmentRecipient(
                        assessment=item,
                        student=student,
                        student_code=student.student_code,
                        student_name=student.full_name,
                        admission_number=student.admission_number,
                    )
                    for student in students
                ]
            )
            AssessmentScore.objects.bulk_create(
                [
                    AssessmentScore(assessment=item, recipient=recipient, student=recipient.student)
                    for recipient in recipients
                ]
            )
            _append_event(item, actor=membership, action="published")

    if publish_sync:
        publish_assessment_sync(item, actor=teacher)
    return item


def _weight(raw):
    if raw is None:
        return Decimal("1.00")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise Rejected("weight must be a valid number.")
    if value <= 0 or value > Decimal("99.99"):
        raise Rejected("weight must be a positive number.")
    return value


@transaction.atomic
def save_scores(*, membership: Membership, payload: dict, publish_sync=True):
    """Teacher entry/editing of scores while the assessment is open for entry."""
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_teacher_authority(membership, item.class_subject)
    if item.state != AssessmentState.PUBLISHED:
        raise Rejected("Scores can only be entered while the assessment is published and open.")

    recipients = {row.student.student_code: row for row in item.recipients.select_related("student")}
    now = timezone.now()
    for entry in payload.get("entries") or []:
        student_code = entry.get("studentId")
        recipient = recipients.get(student_code)
        if recipient is None:
            raise Rejected(f"{student_code} is not in this assessment's published roster.")
        score = _score(entry.get("score"), item.maximum_score)
        AssessmentScore.objects.filter(assessment=item, student=recipient.student).update(
            score=score,
            entered_by=membership,
            entered_at=now if score is not None else None,
        )

    item.last_edited_by = membership
    item.version += 1
    item.save(update_fields=["last_edited_by", "version", "updated_at"])

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def submit_assessment(*, membership: Membership, payload: dict, publish_sync=True):
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_teacher_authority(membership, item.class_subject)
    if item.state != AssessmentState.PUBLISHED:
        raise Rejected("Only a published, open assessment can be submitted for review.")

    item.state = AssessmentState.SUBMITTED
    item.submitted_by = membership
    item.submitted_at = timezone.now()
    item.last_edited_by = membership
    item.version += 1
    item.save()
    _append_event(item, actor=membership, action="submitted")

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def return_for_correction(*, membership: Membership, payload: dict, publish_sync=True):
    """Send a submitted or locked assessment back to the Teacher for open editing."""
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_release_authority(membership)
    if item.state not in {AssessmentState.SUBMITTED, AssessmentState.LOCKED}:
        raise Rejected("Only a submitted or locked assessment can be returned for correction.")
    if item.state == AssessmentState.LOCKED and item.locked_by_id is None:
        raise Rejected("This assessment's lock is missing its authorizing membership.")

    item.state = AssessmentState.PUBLISHED
    item.submitted_at = None
    item.submitted_by = None
    item.locked_at = None
    item.locked_by = None
    item.version += 1
    item.save()
    _append_event(
        item,
        actor=membership,
        action="returned",
        comment=payload.get("comment", ""),
    )

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def lock_assessment(*, membership: Membership, payload: dict, publish_sync=True):
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_release_authority(membership)
    if item.state != AssessmentState.SUBMITTED:
        raise Rejected("Only a submitted assessment can be locked.")

    item.state = AssessmentState.LOCKED
    item.locked_by = membership
    item.locked_at = timezone.now()
    item.version += 1
    item.save()
    _append_event(item, actor=membership, action="locked")

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def release_assessment(*, membership: Membership, payload: dict, publish_sync=True):
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_release_authority(membership)
    if item.state != AssessmentState.LOCKED:
        raise Rejected("Only a locked assessment can be released to Students and Parents.")

    item.state = AssessmentState.RELEASED
    item.released_by = membership
    item.released_at = timezone.now()
    item.version += 1
    item.save()
    _append_event(item, actor=membership, action="released")

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def correct_score(*, membership: Membership, payload: dict, publish_sync=True):
    """An explicit, audited score change after normal entry has closed off.

    Available to the current class-subject Teacher and to Administrator or
    Proprietor - never a silent rewrite: every correction appends an
    AssessmentEvent with the previous and new value. The closed-term check in
    `_loaded_assessment`/`_term` already blocks this once the term is closed;
    this function itself refuses correction on a DRAFT/PUBLISHED assessment,
    where the ordinary saveScores path already covers open editing.
    """
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    if membership.role == Role.TEACHER:
        _assert_teacher_authority(membership, item.class_subject)
    elif membership.role in {Role.ADMINISTRATOR, Role.PROPRIETOR}:
        pass
    else:
        raise Rejected("This membership cannot correct assessment scores.")
    if item.state not in {AssessmentState.SUBMITTED, AssessmentState.LOCKED, AssessmentState.RELEASED}:
        raise Rejected("Use ordinary score entry while the assessment is still open, not a correction.")
    # A closed term already blocks writes at _term(); re-validate defensively
    # here too since correction can happen long after publication.
    if item.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Assessments cannot be changed in a closed academic term.")

    student_code = payload.get("studentId")
    recipient = item.recipients.filter(student__student_code=student_code).select_related("student").first()
    if recipient is None:
        raise Rejected(f"{student_code} is not in this assessment's published roster.")
    score_row = AssessmentScore.objects.select_for_update().get(assessment=item, student=recipient.student)
    previous = score_row.score
    new_value = _score(payload.get("score"), item.maximum_score)
    score_row.score = new_value
    score_row.entered_by = membership
    score_row.entered_at = timezone.now() if new_value is not None else None
    score_row.save()

    item.last_edited_by = membership
    item.version += 1
    item.save(update_fields=["last_edited_by", "version", "updated_at"])
    _append_event(
        item,
        actor=membership,
        action="corrected",
        comment=payload.get("comment", ""),
        student=recipient.student,
        previous_score=previous,
        new_score=new_value,
    )

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item


@transaction.atomic
def principal_review(*, membership: Membership, payload: dict, publish_sync=True):
    """A Secondary Principal's review note. Advisory only: it never changes
    assessment state or gates release, matching the product's existing
    "Principal approval is a review decision, not publication" boundary."""
    item = _loaded_assessment(membership.school, payload["id"], lock=True)
    _assert_principal_secondary_authority(membership, item.class_subject)
    if item.state not in {AssessmentState.SUBMITTED, AssessmentState.LOCKED}:
        raise Rejected("Only a submitted or locked assessment can be reviewed.")

    action = payload["action"]
    if action == "principalReview":
        event_action = "principal_reviewed"
    elif action == "principalReturn":
        event_action = "principal_returned"
    else:
        raise Rejected("Unsupported Principal review action.")
    comment = payload.get("comment", "").strip()
    if event_action == "principal_returned" and not comment:
        raise Rejected("Add a comment before returning an assessment with concerns.")

    _append_event(item, actor=membership, action=event_action, comment=comment)

    if publish_sync:
        publish_assessment_sync(item, actor=membership)
    return item
