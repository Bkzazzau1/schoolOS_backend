from apps.schools.models import Role
from apps.students.models import GuardianLink, Student

from .models import Assignment, AssignmentRecipient, AssignmentState, AssignmentSubmission
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
        linked_ids = GuardianLink.objects.filter(
            account_user=membership.user,
            student__school=membership.school,
        ).values_list("student_id", flat=True)
        if AssignmentRecipient.objects.filter(
            assignment=item, student_id__in=linked_ids
        ).exists():
            return payload
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
    if membership.role == Role.STUDENT:
        if item.student.account_user_id == membership.user_id:
            return payload
        return None
    if membership.role == Role.PARENT:
        if GuardianLink.objects.filter(
            account_user=membership.user,
            student=item.student,
        ).exists():
            return payload
    return None
