from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.students.models import StudentEnrollment
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link

from .services import attach_current_enrollment_context


@receiver(post_save, sender=StudentEnrollment)
def attach_academic_context_to_active_enrollment(sender, instance, created, **kwargs):
    """Link new active enrollments to the current session/class when configured.

    Registration remains backward compatible when a school has not configured
    the academic calendar yet. Once configured, canonical context is attached
    automatically and the private Student/Parent payloads are republished.
    """

    if not created:
        return
    context = attach_current_enrollment_context(instance)
    if context is None:
        return
    publish_student_class_link(instance.student)
    publish_parent_family_links_for_student(instance.student)
