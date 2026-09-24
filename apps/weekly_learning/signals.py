from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.academics.models import (
    CurriculumTopic,
    EnrollmentAcademicContext,
    StudentSubjectSelection,
    TeachingAssignment,
)
from apps.lesson_delivery.models import LessonDeliveryRecord, LessonPlan
from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .models import WeeklyLearningState, WeeklyLearningUpdate
from .services import WEEKLY_LEARNING_ENTITY, publish_update


def _touch_visibility(items, *, actor=None):
    """Allocate a fresh sync sequence when who may read a record changes.

    Published payloads remain byte-for-byte frozen. The new sequence only makes
    the pull layer re-run handler.visible for memberships whose relationship to
    the record may have changed.
    """

    for item in items.select_related("school"):
        record = SyncRecord.objects.filter(
            school=item.school,
            entity_type=WEEKLY_LEARNING_ENTITY,
            entity_id=item.external_id,
            deleted=False,
        ).first()
        if record is None:
            continue
        record.version += 1
        record.updated_by = actor
        record.save(update_fields=["version", "updated_by"])


@receiver(post_save, sender=LessonPlan)
def refresh_weekly_learning_from_plan(sender, instance, **kwargs):
    drafts = WeeklyLearningUpdate.objects.filter(
        class_subject=instance.class_subject,
        term=instance.timetable_entry.term,
        week_start__lte=instance.lesson_date,
        week_end__gte=instance.lesson_date,
        state=WeeklyLearningState.DRAFT,
    )
    for item in drafts:
        publish_update(item, actor=instance.last_edited_by or instance.submitted_by)


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
    _touch_visibility(
        WeeklyLearningUpdate.objects.filter(
            class_subject=instance.class_subject,
            state=WeeklyLearningState.PUBLISHED,
        ),
        actor=instance.assigned_by,
    )


@receiver(post_save, sender=CurriculumTopic)
def refresh_weekly_learning_topic_labels(sender, instance, **kwargs):
    drafts = WeeklyLearningUpdate.objects.filter(
        class_subject=instance.class_subject,
        term=instance.term,
        state=WeeklyLearningState.DRAFT,
    )
    for item in drafts:
        publish_update(item)


@receiver(post_save, sender=StudentSubjectSelection)
def refresh_weekly_learning_elective_visibility(sender, instance, **kwargs):
    _touch_visibility(
        WeeklyLearningUpdate.objects.filter(
            class_subject=instance.class_subject,
            state=WeeklyLearningState.PUBLISHED,
        )
    )


@receiver(post_save, sender=EnrollmentAcademicContext)
def refresh_weekly_learning_enrollment_visibility(sender, instance, **kwargs):
    _touch_visibility(
        WeeklyLearningUpdate.objects.filter(
            class_subject__session=instance.session,
            class_subject__academic_class=instance.academic_class,
            state=WeeklyLearningState.PUBLISHED,
        )
    )


def _refresh_guardian_school(instance):
    school_id = instance.student.school_id
    if school_id is None:
        return
    _touch_visibility(
        WeeklyLearningUpdate.objects.filter(
            school_id=school_id,
            state=WeeklyLearningState.PUBLISHED,
        )
    )


@receiver(post_save, sender=GuardianLink)
def refresh_weekly_learning_guardian_link(sender, instance, **kwargs):
    _refresh_guardian_school(instance)


@receiver(post_delete, sender=GuardianLink)
def refresh_weekly_learning_guardian_unlink(sender, instance, **kwargs):
    _refresh_guardian_school(instance)
