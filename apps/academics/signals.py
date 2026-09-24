from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.errors import Rejected
from apps.staff.constants import PROFILE as STAFF_PROFILE
from apps.students.models import StudentEnrollment
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link
from apps.sync.models import SyncRecord

from .curriculum_models import TeachingAssignment
from .curriculum_services import (
    publish_teacher_class_assignment,
    refresh_teacher_links_for_school,
    resolve_teacher_membership,
)
from .models import AcademicSession
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


@receiver(post_save, sender=AcademicSession)
def refresh_teacher_links_after_session_state_change(sender, instance, **kwargs):
    """Republish private Teacher assignments when a session activates or closes."""

    refresh_teacher_links_for_school(instance.school)


@receiver(post_save, sender=SyncRecord)
def reconcile_teacher_access_after_staff_profile_change(sender, instance, **kwargs):
    """Grant teaching workspace access only after staff onboarding links a Teacher login.

    A Principal may create the responsibility before onboarding is complete. The
    canonical assignment keeps the stable staff-directory id, but private Teacher
    records are not published until the staff profile contains a valid Teacher
    membership id. This receiver closes that gap automatically when onboarding
    later completes.
    """

    if instance.entity_type != STAFF_PROFILE or instance.deleted:
        return
    assignments = list(
        TeachingAssignment.objects.filter(
            class_subject__session__school=instance.school,
            teacher_staff_id=instance.entity_id,
            is_active=True,
        ).select_related("teacher_membership")
    )
    if not assignments:
        return
    try:
        membership = resolve_teacher_membership(instance.school, instance.entity_id)
    except Rejected:
        return
    if membership is None:
        return
    changed = False
    for assignment in assignments:
        if assignment.teacher_membership_id == membership.id:
            continue
        assignment.teacher_membership = membership
        assignment.save(update_fields=["teacher_membership", "updated_at"])
        changed = True
    if changed:
        publish_teacher_class_assignment(membership)
