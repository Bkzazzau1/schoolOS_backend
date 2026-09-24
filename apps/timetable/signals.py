from django.db.models import Count
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.academics.models import AcademicTerm, ClassSubject, TeachingAssignment
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role

from .models import TimetableEntry
from .services import refresh_class_subject_sync, refresh_term_sync
from .teacher_sync import (
    publish_school_teacher_timetable_links,
    publish_teacher_timetable_link,
)


@receiver(pre_save, sender=ClassSubject)
def protect_curriculum_used_by_timetable(sender, instance, **kwargs):
    if not instance.pk:
        return
    existing = ClassSubject.objects.filter(pk=instance.pk).first()
    if existing is None:
        return
    scheduled = TimetableEntry.objects.filter(
        class_subject=existing,
        is_active=True,
    )
    if existing.is_active and not instance.is_active and scheduled.exists():
        raise Rejected(
            "Deactivate or finish this class-subject timetable before removing it from the active curriculum."
        )
    if instance.periods_per_week < existing.periods_per_week:
        overallocated = (
            scheduled.values("term_id")
            .annotate(slot_count=Count("id"))
            .filter(slot_count__gt=instance.periods_per_week)
            .exists()
        )
        if overallocated:
            raise Rejected(
                "Reduce the scheduled timetable periods before lowering curriculum periods per week."
            )


@receiver(post_save, sender=TeachingAssignment)
def refresh_timetable_when_teacher_assignment_changes(sender, instance, **kwargs):
    refresh_class_subject_sync(instance.class_subject, actor=instance.assigned_by)
    publish_school_teacher_timetable_links(
        instance.school,
        actor=instance.assigned_by,
    )


@receiver(post_save, sender=AcademicTerm)
def refresh_timetable_when_term_state_changes(sender, instance, **kwargs):
    refresh_term_sync(instance)
    publish_school_teacher_timetable_links(instance.session.school)


@receiver(post_save, sender=Membership)
def refresh_timetable_when_teacher_membership_changes(sender, instance, **kwargs):
    if instance.role != Role.TEACHER:
        return
    class_subject_ids = TeachingAssignment.objects.filter(
        teacher_membership=instance,
        ended_at__isnull=True,
    ).values_list("class_subject_id", flat=True)
    for class_subject in ClassSubject.objects.filter(id__in=class_subject_ids):
        refresh_class_subject_sync(class_subject)
    # Publish even after deactivation so a previously authorized device receives
    # an empty private schedule rather than retaining cached lessons indefinitely.
    publish_teacher_timetable_link(instance)
