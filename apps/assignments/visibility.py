from apps.schools.models import Role
from apps.students.models import GuardianLink, Student

from .models import (
    Assignment,
    AssignmentRecipient,
    AssignmentState,
    AssignmentSubmission,
    SubmissionState,
)
from .services import current_teacher


def _assignment_from_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    return (
        Assignment.objects.select_related(
            "class_subject__academic_class",
            "author_membership",
        )
        .filter(school=membership.school, external_id=external_id)
        .first()
    )


def assignment_visible_payload(membership, payload):
    item = _assignment_from_payload(membership, payload)
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
    if item.state not in {AssignmentState.PUBLISHED, AssignmentState.CLOSED}:
        return None
    if membership.role == Role.STUDENT:
        student_ids = Student.objects.filter(
            school=membership.school,
            account_user=membership.user,
        ).values_list("id", flat=True)
        if AssignmentRecipient.objects.filter(
            assignment=item, student_id__in=student_ids
        ).exists():
            return payload
        return None
    if membership.role == Role.PARENT:
        linked_ids = list(
            GuardianLink.objects.filter(
                account_user=membership.user,
                student__school=membership.school,
            ).values_list("student_id", flat=True)
        )
        recipients = list(
            AssignmentRecipient.objects.filter(
                assignment=item,
                student_id__in=linked_ids,
            ).order_by("student_name", "student_code")
        )
        if recipients:
            # Families need to know which of their own linked children received
            # this assignment, but must never receive the frozen class roster.
            visible = dict(payload)
            visible["familyRecipients"] = [
                {
                    "studentId": recipient.student_code,
                    "studentName": recipient.student_name,
                    "admissionNumber": recipient.admission_number,
                }
                for recipient in recipients
            ]
            return visible
    return None


def submission_visible_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    item = (
        AssignmentSubmission.objects.select_related(
            "student",
            "assignment__class_subject__academic_class",
            "assignment__author_membership",
        )
        .filter(school=membership.school, external_id=external_id)
        .first()
    )
    if item is None:
        return None

    # A Student's in-progress draft is private to that Student. The canonical
    # history remains on the server, but every oversight role begins only after
    # actual submission.
    if membership.role == Role.STUDENT:
        return payload if item.student.account_user_id == membership.user_id else None
    if item.state == SubmissionState.DRAFT:
        return None

    if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
        return payload
    if (
        membership.role == Role.PRINCIPAL
        and item.assignment.class_subject.academic_class.section.strip().casefold()
        == "secondary"
    ):
        return payload
    if membership.role == Role.TEACHER:
        teacher = current_teacher(item.assignment.class_subject, required=False)
        if item.assignment.author_membership_id == membership.id or (
            teacher is not None and teacher.id == membership.id
        ):
            return payload
        return None
    if membership.role == Role.PARENT:
        if GuardianLink.objects.filter(
            account_user=membership.user,
            student=item.student,
        ).exists():
            return payload
    return None
