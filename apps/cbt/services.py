from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.academics.models import AcademicLifecycleStatus, AcademicTerm, ClassSubject, TeachingAssignment
from apps.core.errors import Rejected
from apps.lesson_attendance.services import eligible_students_for_class_subject
from apps.schools.models import Membership, Role
from apps.students.models import Student
from apps.sync.models import SyncRecord

from .models import (
    CbtAttempt,
    CbtEvent,
    CbtQuestion,
    CbtRecipient,
    CbtResultMode,
    CbtTest,
    CbtTestState,
)

CBT_TEST_ENTITY = "academic_cbt_test"
CBT_ATTEMPT_ENTITY = "academic_cbt_attempt"


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
        raise Rejected("CBT tests require an active class curriculum subject.")
    return item


def _term(class_subject, value):
    try:
        item = AcademicTerm.objects.select_related("session").get(
            id=value, session=class_subject.session
        )
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not belong to this class-subject session.")
    if item.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("CBT tests cannot be changed in a closed academic term.")
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
    """The one rule that makes 'each subject teacher can create a CBT test
    only for the subject they choose' true: class_subject always names one
    specific subject in one specific class, and this always requires the
    acting membership to be that exact class-subject's current teacher -
    never merely 'a teacher of this class'."""
    if membership.role != Role.TEACHER or not membership.is_active:
        raise Rejected("Only an active Teacher membership can manage CBT tests.")
    teacher = current_teacher(class_subject)
    if teacher.id != membership.id:
        raise Rejected("This Teacher is not the current authority for that class subject.")
    return teacher


def _student_for_membership(membership):
    if membership.role != Role.STUDENT or not membership.is_active:
        raise Rejected("Only an active Student membership can attempt a CBT test.")
    item = Student.objects.filter(school=membership.school, account_user=membership.user).first()
    if item is None:
        raise Rejected("This Student membership is not linked to a canonical student record.")
    return item


def _clean_questions(raw_questions):
    if not raw_questions:
        return []
    cleaned = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raise Rejected("Each question must be an object.")
        prompt = str(raw.get("prompt", "")).strip()
        options = raw.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise Rejected(f"Question {index + 1} needs at least two options.")
        options = [str(item).strip() for item in options]
        if any(not item for item in options):
            raise Rejected(f"Question {index + 1} has a blank option.")
        correct_index = raw.get("correctIndex")
        if not isinstance(correct_index, int) or isinstance(correct_index, bool):
            raise Rejected(f"Question {index + 1} needs a correct answer.")
        if not (0 <= correct_index < len(options)):
            raise Rejected(f"Question {index + 1}'s correct answer is out of range.")
        if not prompt:
            raise Rejected(f"Question {index + 1} needs a prompt.")
        cleaned.append(
            {
                "prompt": prompt,
                "options": options,
                "correct_index": correct_index,
                "explanation": str(raw.get("explanation", "")).strip(),
            }
        )
    return cleaned


def _loaded_test(school, external_id, *, lock=False):
    query = CbtTest.objects.select_related(
        "school",
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
        "term",
        "author_membership__user",
    ).prefetch_related("questions")
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("CBT test does not exist in this school.")
    return item


def _loaded_attempt(school, external_id, *, lock=False):
    query = CbtAttempt.objects.select_related(
        "school",
        "test__class_subject__academic_class",
        "test__class_subject__subject",
        "student",
        "recipient",
    )
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("CBT attempt does not exist in this school.")
    return item


def _next_revision(test):
    latest = test.events.order_by("-revision").first()
    return (latest.revision if latest is not None else 0) + 1


def _append_event(test, *, actor, action, comment=""):
    CbtEvent.objects.create(
        test=test, revision=_next_revision(test), action=action, actor_membership=actor, comment=comment
    )


def serialize_cbt_test(item: CbtTest) -> dict:
    class_subject = item.class_subject
    teacher = current_teacher(class_subject, required=False)
    questions = list(item.questions.all())
    attempts = list(CbtAttempt.objects.filter(test=item))
    submitted = [a for a in attempts if a.submitted]
    average_score_percent = None
    if submitted and questions:
        total_percent = sum(Decimal(a.score or 0) / Decimal(len(questions)) * 100 for a in submitted)
        average_score_percent = float((total_percent / len(submitted)).quantize(Decimal("0.1")))

    return {
        "id": item.external_id,
        "canonicalTestId": str(item.id),
        "sessionId": str(class_subject.session_id),
        "termId": str(item.term_id),
        "term": item.term.name,
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "section": class_subject.academic_class.section,
        "subjectId": str(class_subject.subject_id),
        "subject": class_subject.subject.name,
        "state": item.state,
        "title": item.title,
        "durationMinutes": item.duration_minutes,
        "instructions": item.instructions,
        "resultMode": item.result_mode,
        "version": item.version,
        "authorMembershipId": str(item.author_membership_id),
        "author": _membership_name(item.author_membership),
        "currentTeacherId": str(teacher.id) if teacher else "",
        "currentTeacher": _membership_name(teacher),
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
        "closedAt": item.closed_at.isoformat() if item.closed_at else None,
        "totalRecipients": item.recipients.count(),
        "submittedCount": len(submitted),
        "averageScorePercent": average_score_percent,
        "questions": [
            {
                "id": str(q.id),
                "sequence": q.sequence,
                "prompt": q.prompt,
                "options": q.options,
                "correctIndex": q.correct_index,
                "explanation": q.explanation,
            }
            for q in questions
        ],
        "updatedAt": item.updated_at.isoformat(),
    }


def serialize_cbt_attempt(item: CbtAttempt) -> dict:
    test = item.test
    class_subject = test.class_subject
    return {
        "id": item.external_id,
        "canonicalAttemptId": str(item.id),
        "testId": test.external_id,
        "testTitle": test.title,
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "subject": class_subject.subject.name,
        "durationMinutes": test.duration_minutes,
        "questionCount": test.questions.count(),
        "studentId": item.student.student_code,
        "studentName": item.student.full_name,
        "startedAt": item.started_at.isoformat() if item.started_at else None,
        "deadlineAt": item.deadline_at.isoformat() if item.deadline_at else None,
        "answers": item.answers,
        "submitted": item.submitted,
        "submittedAt": item.submitted_at.isoformat() if item.submitted_at else None,
        "score": item.score,
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
    SyncRecord.objects.filter(school=school, entity_type=entity_type, entity_id=entity_id).update(
        payload=payload, deleted=False, updated_by=actor
    )


def publish_test_sync(item: CbtTest, *, actor=None):
    item = _loaded_test(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_type=CBT_TEST_ENTITY,
        entity_id=item.external_id,
        payload=serialize_cbt_test(item),
        actor=actor,
    )


def publish_attempt_sync(item: CbtAttempt, *, actor=None):
    item = _loaded_attempt(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_type=CBT_ATTEMPT_ENTITY,
        entity_id=item.external_id,
        payload=serialize_cbt_attempt(item),
        actor=actor,
    )


@transaction.atomic
def upsert_cbt_test(*, membership: Membership, payload: dict, publish_sync=True) -> CbtTest:
    school = membership.school
    action = payload["action"]
    class_subject = _class_subject(school, payload["classSubjectId"])
    term = _term(class_subject, payload["termId"])
    _assert_teacher_authority(membership, class_subject)

    existing = (
        CbtTest.objects.select_for_update().filter(school=school, external_id=payload["id"]).first()
    )
    if existing is not None:
        if existing.class_subject_id != class_subject.id or existing.term_id != term.id:
            raise Rejected("A CBT test cannot be moved to another class subject or term.")
        if action == "saveDraft" and existing.state != CbtTestState.DRAFT:
            raise Rejected("A published or closed CBT test cannot be silently rewritten.")
        if action == "publish" and existing.state != CbtTestState.DRAFT:
            raise Rejected("Only a draft CBT test can be published.")
        if action == "close" and existing.state != CbtTestState.PUBLISHED:
            raise Rejected("Only a published CBT test can be closed.")

    title = payload.get("title", "").strip()
    duration_minutes = int(payload.get("durationMinutes") or 0)
    instructions = payload.get("instructions", "").strip()
    result_mode = payload.get("resultMode") or CbtResultMode.SCORE_ONLY
    if result_mode not in CbtResultMode.values:
        raise Rejected("Unsupported result mode.")
    questions = _clean_questions(payload.get("questions"))

    if action == "publish":
        if not title:
            raise Rejected("Add a title before publishing.")
        if duration_minutes <= 0:
            raise Rejected("Duration must be greater than zero before publishing.")
        if not questions:
            raise Rejected("Add at least one question before publishing.")

    now = timezone.now()
    if action == "close":
        if existing is None:
            raise Rejected("This CBT test does not exist yet.")
        item = existing
        item.state = CbtTestState.CLOSED
        item.closed_by = membership
        item.closed_at = now
        item.last_edited_by = membership
        item.version += 1
        item.save()
        _append_event(item, actor=membership, action="closed")
        if publish_sync:
            publish_test_sync(item, actor=membership)
        return item

    if existing is None:
        if action != "saveDraft":
            raise Rejected("Create and synchronize the CBT draft before publication.")
        item = CbtTest.objects.create(
            school=school,
            external_id=payload["id"],
            class_subject=class_subject,
            term=term,
            author_membership=membership,
            last_edited_by=membership,
            state=CbtTestState.DRAFT,
            title=title,
            duration_minutes=duration_minutes,
            instructions=instructions,
            result_mode=result_mode,
            version=1,
        )
        _append_event(item, actor=membership, action="created")
    else:
        item = existing
        item.title = title
        item.duration_minutes = duration_minutes
        item.instructions = instructions
        item.result_mode = result_mode
        item.last_edited_by = membership
        item.version += 1
        if action == "publish":
            item.state = CbtTestState.PUBLISHED
            item.published_by = membership
            item.published_at = now
        item.save()

    if action in {"saveDraft", "publish"}:
        item.questions.all().delete()
        CbtQuestion.objects.bulk_create(
            [
                CbtQuestion(
                    test=item,
                    sequence=index + 1,
                    prompt=q["prompt"],
                    options=q["options"],
                    correct_index=q["correct_index"],
                    explanation=q["explanation"],
                )
                for index, q in enumerate(questions)
            ]
        )

    if action == "publish":
        students = eligible_students_for_class_subject(class_subject, on_date=timezone.localdate())
        if not students:
            raise Rejected("This class subject has no eligible students to receive the CBT test.")
        recipients = CbtRecipient.objects.bulk_create(
            [
                CbtRecipient(
                    test=item,
                    student=student,
                    student_code=student.student_code,
                    student_name=student.full_name,
                    admission_number=student.admission_number,
                )
                for student in students
            ]
        )
        attempts = CbtAttempt.objects.bulk_create(
            [
                CbtAttempt(
                    school=school,
                    external_id=f"cbt-attempt-{item.external_id}-{recipient.student_id}",
                    test=item,
                    recipient=recipient,
                    student=recipient.student,
                    answers=[],
                )
                for recipient in recipients
            ]
        )
        # Each attempt's sync record is born here, server-side - a Student
        # only ever updates an existing record (start/answer/submit), never
        # creates their own attempt.
        for attempt in attempts:
            publish_attempt_sync(attempt, actor=membership)
        _append_event(item, actor=membership, action="published")

    if publish_sync:
        publish_test_sync(item, actor=membership)
    return item


@transaction.atomic
def start_attempt(*, membership: Membership, external_id: str, publish_sync=True) -> CbtAttempt:
    """external_id here is the deterministic attempt id
    (`cbt-attempt-{testExternalId}-{studentId}`), which the client computes
    the same way the server does, so an attempt can be looked up before the
    Student has ever written to it themselves."""
    student = _student_for_membership(membership)
    item = CbtAttempt.objects.select_for_update().filter(school=membership.school, external_id=external_id).first()
    if item is None:
        raise Rejected("This CBT test is not available to you.")
    if item.student_id != student.id:
        raise Rejected("This CBT attempt does not belong to this Student.")
    if item.test.state != CbtTestState.PUBLISHED:
        raise Rejected("This CBT test is not currently open.")
    if item.test.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("CBT tests cannot be changed in a closed academic term.")
    if item.started_at is None:
        item.started_at = timezone.now()
        item.deadline_at = item.started_at + timedelta(minutes=item.test.duration_minutes)
        item.answers = [None] * item.test.questions.count()
        item.version += 1
        item.save()
    if publish_sync:
        publish_attempt_sync(item, actor=membership)
    return item


@transaction.atomic
def answer_question(
    *, membership: Membership, external_id: str, question_index: int, option_index: int, publish_sync=True
) -> CbtAttempt:
    student = _student_for_membership(membership)
    item = CbtAttempt.objects.select_for_update().filter(school=membership.school, external_id=external_id).first()
    if item is None or item.student_id != student.id:
        raise Rejected("This CBT attempt does not belong to this Student.")
    if item.started_at is None:
        raise Rejected("Start the attempt before answering.")
    if item.submitted:
        raise Rejected("This attempt is already submitted.")
    if timezone.now() >= item.deadline_at:
        raise Rejected("This CBT attempt's time is up.")
    questions = list(item.test.questions.all())
    if not (0 <= question_index < len(questions)):
        raise Rejected("questionIndex is out of range.")
    if not (0 <= option_index < len(questions[question_index].options)):
        raise Rejected("optionIndex is out of range.")
    answers = list(item.answers) if item.answers else [None] * len(questions)
    while len(answers) < len(questions):
        answers.append(None)
    answers[question_index] = option_index
    item.answers = answers
    item.version += 1
    item.save()
    if publish_sync:
        publish_attempt_sync(item, actor=membership)
    return item


@transaction.atomic
def submit_attempt(*, membership: Membership, external_id: str, publish_sync=True) -> CbtAttempt:
    student = _student_for_membership(membership)
    item = CbtAttempt.objects.select_for_update().filter(school=membership.school, external_id=external_id).first()
    if item is None or item.student_id != student.id:
        raise Rejected("This CBT attempt does not belong to this Student.")
    if item.started_at is None:
        raise Rejected("Start the attempt before submitting.")
    if item.submitted:
        return item
    questions = list(item.test.questions.all())
    answers = list(item.answers) if item.answers else []
    score = sum(
        1
        for index, question in enumerate(questions)
        if index < len(answers) and answers[index] == question.correct_index
    )
    item.score = score
    item.submitted_at = timezone.now()
    item.version += 1
    item.save()
    if publish_sync:
        publish_attempt_sync(item, actor=membership)
        publish_test_sync(item.test, actor=membership)
    return item
