from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .models import (
    EnrollmentStatus,
    LifecycleStatus,
    Student,
    StudentEnrollment,
    StudentLifecycleEvent,
    StudentStatus,
)
from .services import publish_school_roster_meter


@transaction.atomic
def graduate_canonical_student_from_alumni_transition(profile, *, actor=None) -> bool:
    """Mirror an Alumni Management transition into the canonical roster.

    Alumni existed before the canonical student domain, so legacy transitions
    without a canonical match must keep working. When the supplied admission
    number/former-student reference identifies an active canonical student, the
    enrollment is closed as Graduated in the same database transaction and the
    billing meter is updated exactly once.
    """

    school = profile.school
    student = None
    admission_number = (profile.admission_number or "").strip()
    reference = (profile.original_student_reference or "").strip()

    if admission_number:
        student = Student.objects.select_for_update().filter(
            school=school,
            admission_number__iexact=admission_number,
        ).first()

    if student is None and reference:
        student = Student.objects.select_for_update().filter(
            school=school,
            student_code__iexact=reference,
        ).first()
        if student is None:
            try:
                canonical_id = UUID(reference)
            except ValueError:
                canonical_id = None
            if canonical_id is not None:
                student = Student.objects.select_for_update().filter(
                    school=school,
                    id=canonical_id,
                ).first()

    if student is None:
        return False

    external_id = f"alumni-transition:{profile.membership_id}"
    if student.status == StudentStatus.GRADUATED:
        StudentLifecycleEvent.objects.get_or_create(
            school=school,
            external_id=external_id,
            defaults={
                "student": student,
                "workflow": "Alumni",
                "change": "Graduated to Alumni",
                "status": LifecycleStatus.COMPLETED,
                "requested_at": profile.submitted_at or timezone.now(),
                "completed_at": profile.submitted_at or timezone.now(),
                "approved_by": "Alumni Management",
                "created_by": actor,
            },
        )
        return True

    enrollment = StudentEnrollment.objects.select_for_update().filter(
        student=student,
        status=EnrollmentStatus.ACTIVE,
    ).first()
    if enrollment is None:
        return False

    now = timezone.now()
    enrollment.status = EnrollmentStatus.GRADUATED
    enrollment.is_billable = False
    enrollment.ended_at = now
    enrollment.save(update_fields=["status", "is_billable", "ended_at"])

    student.status = StudentStatus.GRADUATED
    student.save(update_fields=["status", "updated_at"])

    StudentLifecycleEvent.objects.get_or_create(
        school=school,
        external_id=external_id,
        defaults={
            "student": student,
            "workflow": "Alumni",
            "change": "Graduated to Alumni",
            "from_class": enrollment.class_name,
            "status": LifecycleStatus.COMPLETED,
            "requested_at": profile.submitted_at or now,
            "completed_at": now,
            "approved_by": "Alumni Management",
            "created_by": actor,
        },
    )
    publish_school_roster_meter(
        school,
        bump_revision=True,
        reason="student_graduated_to_alumni",
    )
    return True
