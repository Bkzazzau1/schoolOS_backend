from apps.schools.models import Role
from apps.students.models import Student

from .models import CbtAttempt, CbtRecipient, CbtResultMode, CbtTest, CbtTestState
from .services import current_teacher


def _test_from_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    return (
        CbtTest.objects.select_related("class_subject__academic_class", "author_membership")
        .filter(school=membership.school, external_id=external_id)
        .first()
    )


def _redact_questions(payload):
    visible = dict(payload)
    visible["questions"] = [
        {k: v for k, v in question.items() if k not in {"correctIndex", "explanation"}}
        for question in payload.get("questions", [])
    ]
    return visible


def cbt_test_visible_payload(membership, payload):
    item = _test_from_payload(membership, payload)
    if item is None:
        return None

    if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
        return payload
    if (
        membership.role == Role.PRINCIPAL
        and item.class_subject.academic_class.section.strip().casefold() == "secondary"
    ):
        return payload
    if membership.role == Role.TEACHER:
        teacher = current_teacher(item.class_subject, required=False)
        if item.author_membership_id == membership.id or (
            teacher is not None and teacher.id == membership.id
        ):
            return payload
        return None

    if membership.role != Role.STUDENT:
        return None
    if item.state not in {CbtTestState.PUBLISHED, CbtTestState.CLOSED}:
        return None

    student_ids = Student.objects.filter(
        school=membership.school, account_user=membership.user
    ).values_list("id", flat=True)
    recipient = CbtRecipient.objects.filter(test=item, student_id__in=student_ids).first()
    if recipient is None:
        return None

    # A Student never receives a correct answer before their own attempt is
    # submitted, and even then only when the Teacher's result_mode allows it -
    # the same redaction whether this is the first look at the test or a
    # re-sync after answering.
    attempt = CbtAttempt.objects.filter(test=item, recipient=recipient).first()
    reveal = (
        attempt is not None
        and attempt.submitted
        and item.result_mode == CbtResultMode.SCORE_AND_ANSWERS
    )
    return payload if reveal else _redact_questions(payload)


def _attempt_from_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    return (
        CbtAttempt.objects.select_related(
            "student", "test__class_subject__academic_class", "test__author_membership"
        )
        .filter(school=membership.school, external_id=external_id)
        .first()
    )


def cbt_attempt_visible_payload(membership, payload):
    item = _attempt_from_payload(membership, payload)
    if item is None:
        return None

    if membership.role == Role.STUDENT:
        return payload if item.student.account_user_id == membership.user_id else None

    if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
        return payload
    if (
        membership.role == Role.PRINCIPAL
        and item.test.class_subject.academic_class.section.strip().casefold() == "secondary"
    ):
        return payload
    if membership.role == Role.TEACHER:
        teacher = current_teacher(item.test.class_subject, required=False)
        if item.test.author_membership_id == membership.id or (
            teacher is not None and teacher.id == membership.id
        ):
            return payload
        return None

    return None
