from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.academics.models import CurriculumTopic, TeachingAssignment
from apps.lesson_delivery.models import LessonDeliveryRecord

from .models import WeeklyLearningState, WeeklyLearningUpdate
from .services import publish_update


@receiver(post_save, sender=LessonDeliveryRecord)
def refresh_weekly_learning_from_delivery(sender, instance, **kwargs):
    drafts = WeeklyLearningUpdate.objects.filter(
        class_subject=instance.timetable_entry.class_subject,
        term=instance.timetable_entry.term,
        week_start__lte=instance.lesson_date,
        week_end__gte=instance.lesson_date,
        state=WeeklyLearningState.DRAFT,
    )
    for item in drafts:
        publish_update(item, actor=instance.teacher_membership)


@receiver(post_save, sender=TeachingAssignment)
def refresh_weekly_learning_teacher_authority(sender, instance, **kwargs):
    drafts = WeeklyLearningUpdate.objects.filter(
        class_subject=instance.class_subject,
        state=WeeklyLearningState.DRAFT,
    )
    for item in drafts:
        publish_update(item, actor=instance.assigned_by)


@receiver(post_save, sender=CurriculumTopic)
def refresh_weekly_learning_topic_labels(sender, instance, **kwargs):
    drafts = WeeklyLearningUpdate.objects.filter(
        class_subject=instance.class_subject,
        term=instance.term,
        state=WeeklyLearningState.DRAFT,
    )
    for item in drafts:
        publish_update(item)
