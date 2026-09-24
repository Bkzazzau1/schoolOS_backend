from django.db.models import Q
from django.utils.dateparse import parse_date

from apps.academics.models import (
    ClassSubject,
    CurriculumRequirement,
    EnrollmentAcademicContext,
    StudentSubjectSelection,
    TeachingAssignment,
)
from apps.schools.models import Role
from apps.students.models import GuardianLink, Student


def _subject_and_week(membership, payload):
    week_start = parse_date(str(payload.get("weekStart") or ""))
    week_end = parse_date(str(payload.get("weekEnd") or ""))
    if week_start is None or week_end is None:
        return None, None, None
    try:
        class_subject = ClassSubject.objects.select_related(
            "session",
            "academic_class",
        ).get(
            id=payload.get("classSubjectId"),
            session__school=membership.school,
        )
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        return None, None, None
    return class_subject, week_start, week_end


def _eligible_codes_for_students(membership, payload, student_ids):
    if payload.get("state") != "published":
        return []
    class_subject, week_start, week_end = _subject_and_week(membership, payload)
    if class_subject is None:
        return []

    contexts = list(
        EnrollmentAcademicContext.objects.filter(
            enrollment__student_id__in=student_ids,
            session=class_subject.session,
            academic_class=class_subject.academic_class,
            enrollment__started_at__date__lte=week_end,
        )
        .filter(
            Q(enrollment__ended_at__isnull=True)
            | Q(enrollment__ended_at__date__gte=week_start)
        )
        .select_related("enrollment__student")
    )
    if not contexts:
        return []

    if class_subject.requirement == CurriculumRequirement.COMPULSORY:
        return sorted({ctx.enrollment.student.student_code for ctx in contexts})

    context_ids = [ctx.id for ctx in contexts]
    selected_context_ids = set(
        StudentSubjectSelection.objects.filter(
            enrollment_context_id__in=context_ids,
            class_subject=class_subject,
            selected_at__date__lte=week_end,
        )
        .filter(
            Q(deselected_at__isnull=True)
            | Q(deselected_at__date__gte=week_start)
        )
        .values_list("enrollment_context_id", flat=True)
    )
    return sorted(
        {
            ctx.enrollment.student.student_code
            for ctx in contexts
            if ctx.id in selected_context_ids
        }
    )


def parent_visible_payload(membership, payload):
    if membership.role != Role.PARENT:
        return None
    student_ids = GuardianLink.objects.filter(
        account_user=membership.user,
        student__school=membership.school,
    ).values_list("student_id", flat=True)
    codes = _eligible_codes_for_students(membership, payload, student_ids)
    if not codes:
        return None
    return {**payload, "visibleStudentIds": codes}


def student_visible_payload(membership, payload):
    if membership.role != Role.STUDENT:
        return None
    student_ids = Student.objects.filter(
        school=membership.school,
        account_user=membership.user,
    ).values_list("id", flat=True)
    codes = _eligible_codes_for_students(membership, payload, student_ids)
    if not codes:
        return None
    return {**payload, "visibleStudentIds": codes}


def teacher_can_view_payload(membership, payload):
    if membership.role != Role.TEACHER:
        return False
    if str(payload.get("authorMembershipId") or "") == str(membership.id):
        return True
    try:
        return TeachingAssignment.objects.filter(
            class_subject_id=payload.get("classSubjectId"),
            class_subject__session__school=membership.school,
            teacher_membership=membership,
            ended_at__isnull=True,
        ).exists()
    except (ValueError, TypeError):
        return False
