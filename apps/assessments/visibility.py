from apps.schools.models import Role
from apps.students.models import GuardianLink, Student

from .models import AssessmentDefinition, AssessmentState
from .services import current_teacher


def _assessment_from_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    return (
        AssessmentDefinition.objects.select_related(
            "class_subject__academic_class",
            "author_membership",
        )
        .filter(school=membership.school, external_id=external_id)
        .first()
    )


def assessment_visible_payload(membership, payload):
    item = _assessment_from_payload(membership, payload)
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

    # Student and Parent never receive marks that have not been released, and
    # even once released they receive only their own child's entry - never
    # the class roster.
    if item.state != AssessmentState.RELEASED:
        return None

    if membership.role == Role.STUDENT:
        student_codes = set(
            Student.objects.filter(
                school=membership.school, account_user=membership.user
            ).values_list("student_code", flat=True)
        )
        mine = [row for row in payload.get("entries", []) if row.get("studentId") in student_codes]
        if not mine:
            return None
        visible = dict(payload)
        visible["entries"] = mine
        return visible

    if membership.role == Role.PARENT:
        linked_codes = set(
            GuardianLink.objects.filter(
                account_user=membership.user,
                student__school=membership.school,
            ).values_list("student__student_code", flat=True)
        )
        mine = [row for row in payload.get("entries", []) if row.get("studentId") in linked_codes]
        if not mine:
            return None
        visible = dict(payload)
        visible["entries"] = mine
        return visible

    return None
