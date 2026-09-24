from apps.schools.models import Role
from apps.students.models import GuardianLink, Student

from .models import ReportCard, ReportCardState


def _report_card_from_payload(membership, payload):
    external_id = str(payload.get("id") or "")
    if not external_id:
        return None
    return (
        ReportCard.objects.select_related("academic_class", "student")
        .filter(school=membership.school, external_id=external_id)
        .first()
    )


def report_card_visible_payload(membership, payload):
    item = _report_card_from_payload(membership, payload)
    if item is None:
        return None

    if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
        return payload
    if (
        membership.role == Role.PRINCIPAL
        and item.academic_class.section.strip().casefold() == "secondary"
    ):
        return payload
    if membership.role == Role.TEACHER:
        from apps.class_teachers.services import current_class_teacher

        class_teacher = current_class_teacher(item.academic_class, item.term.session, required=False)
        if class_teacher is not None and class_teacher.id == membership.id:
            return payload
        return None

    # Student and Parent never see a report card before the school has
    # released it, and never anyone else's.
    if item.state != ReportCardState.RELEASED:
        return None

    if membership.role == Role.STUDENT:
        is_mine = Student.objects.filter(
            id=item.student_id, school=membership.school, account_user=membership.user
        ).exists()
        return payload if is_mine else None

    if membership.role == Role.PARENT:
        is_linked = GuardianLink.objects.filter(
            account_user=membership.user, student_id=item.student_id
        ).exists()
        return payload if is_linked else None

    return None
