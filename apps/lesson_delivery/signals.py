from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.academics.models import CurriculumTopic, TeachingAssignment
from apps.lesson_attendance.models import LessonAttendanceRegister, LessonAttendanceState
from apps.schools.models import Membership, Role
from apps.timetable.models import TimetableEntry, TimetableOverride

from .models import LessonDeliveryRecord
from .services import (
    publish_delivery,
    publish_plan,
    publish_topic_progress,
    refresh_occurrence_records,
)


@receiver(post_save, sender=TimetableOverride)
def refresh_delivery_for_occurrence_override(sender, instance, **kwargs):
    refresh_occurrence_records(
        instance.timetable_entry,
        instance.lesson_date,
    )


@receiver(post_save, sender=TimetableEntry)
def refresh_delivery_for_timetable_change(sender, instance, **kwargs):
    refresh_occurrence_records(instance)


@receiver(post_save, sender=TeachingAssignment)
def refresh_delivery_for_teacher_handover(sender, instance, **kwargs):
    for entry in TimetableEntry.objects.filter(class_subject=instance.class_subject):
        refresh_occurrence_records(entry, actor=instance.assigned_by)


@receiver(post_save, sender=Membership)
def refresh_delivery_for_teacher_membership(sender, instance, **kwargs):
    if instance.role != Role.TEACHER:
        return
    class_subject_ids = TeachingAssignment.objects.filter(
        teacher_membership=instance,
    ).values_list("class_subject_id", flat=True)
    for entry in TimetableEntry.objects.filter(class_subject_id__in=class_subject_ids):
        refresh_occurrence_records(entry)


@receiver(post_save, sender=CurriculumTopic)
def refresh_delivery_for_topic_change(sender, instance, **kwargs):
    for plan in instance.lesson_plans.all():
        publish_plan(plan)
    for item in instance.delivery_records.all():
        publish_delivery(item)
    publish_topic_progress(instance)


@receiver(post_save, sender=LessonAttendanceRegister)
def attach_submitted_attendance_to_delivery(sender, instance, **kwargs):
    if instance.state != LessonAttendanceState.SUBMITTED:
        return
    item = LessonDeliveryRecord.objects.filter(
        timetable_entry=instance.timetable_entry,
        lesson_date=instance.lesson_date,
    ).select_related("curriculum_topic").first()
    if item is None or item.attendance_register_id == instance.id:
        return
    item.attendance_register = instance
    item.save(update_fields=["attendance_register", "updated_at"])
    publish_delivery(item, actor=instance.submitted_by)
    publish_topic_progress(item.curriculum_topic, actor=instance.submitted_by)
