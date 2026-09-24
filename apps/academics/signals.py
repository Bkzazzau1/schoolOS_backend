from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.schools.models import Membership
from apps.students.models import EnrollmentStatus, Student, StudentEnrollment
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link

from .models import AcademicTerm, EnrollmentAcademicContext, TeachingAssignment
from .services import attach_current_enrollment_context


@receiver(post_save, sender=StudentEnrollment)
def attach_academic_context_to_active_enrollment(sender, instance, created, **kwargs):
    """Link new active enrollments to the current session/class when configured."""

    if not created:
        return
    context = attach_current_enrollment_context(instance)
    if context is None:
        return
    publish_student_class_link(instance.student)
    publish_parent_family_links_for_student(instance.student)


@receiver(post_save, sender=AcademicTerm)
def refresh_private_academic_context_when_term_changes(sender, instance, **kwargs):
    """Republish Student/Parent/Teacher context whenever term state changes.

    ``currentTerm`` is derived from the active term. Without republishing these
    private sync records, devices could remain on First Term after the school
    activates Second Term even though the canonical database is correct.
    """

    student_ids = (
        EnrollmentAcademicContext.objects.filter(
            session=instance.session,
            enrollment__status=EnrollmentStatus.ACTIVE,
        )
        .values_list("enrollment__student_id", flat=True)
        .distinct()
    )
    for student in Student.objects.filter(id__in=student_ids).iterator():
        publish_student_class_link(student)
        publish_parent_family_links_for_student(student)

    from .curriculum_services import publish_teacher_assignment_link

    teacher_ids = (
        TeachingAssignment.objects.filter(
            ended_at__isnull=True,
            class_subject__session=instance.session,
        )
        .values_list("teacher_membership_id", flat=True)
        .distinct()
    )
    for teacher in Membership.objects.filter(
        id__in=teacher_ids,
        is_active=True,
    ).select_related("school", "user"):
        publish_teacher_assignment_link(teacher)
