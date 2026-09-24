from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.students.models import StudentEnrollment
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link

from .models import AcademicTerm, TeachingAssignment
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
def refresh_teacher_curriculum_when_term_changes(sender, instance, **kwargs):
    """Republish private Teacher class links when current-term context changes."""

    from .curriculum_services import publish_teacher_assignment_link

    teacher_ids = (
        TeachingAssignment.objects.filter(
            ended_at__isnull=True,
            class_subject__session=instance.session,
        )
        .values_list("teacher_membership_id", flat=True)
        .distinct()
    )
    for assignment in (
        TeachingAssignment.objects.filter(
            teacher_membership_id__in=teacher_ids,
            ended_at__isnull=True,
        )
        .select_related("teacher_membership__school", "teacher_membership__user")
        .order_by("teacher_membership_id")
    ):
        publish_teacher_assignment_link(assignment.teacher_membership)
