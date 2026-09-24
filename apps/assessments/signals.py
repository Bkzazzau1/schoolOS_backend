from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.academics.models import TeachingAssignment
from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .models import AssessmentRecipient
from .services import ASSESSMENT_ENTITY


def _touch_records(school_id, entity_ids):
    ids = [value for value in set(entity_ids) if value]
    if not ids:
        return
    with transaction.atomic():
        records = SyncRecord.objects.select_for_update().filter(
            school_id=school_id,
            entity_type=ASSESSMENT_ENTITY,
            entity_id__in=ids,
            deleted=False,
        )
        for record in records:
            # Visibility (currentTeacher) changed even if content did not. Saving
            # assigns a fresh school sync sequence without pretending content
            # got a new version.
            record.save(update_fields=["updated_by"])


def _touch_student_access(student_id, school_id):
    assessment_ids = list(
        AssessmentRecipient.objects.filter(student_id=student_id).values_list(
            "assessment__external_id", flat=True
        )
    )
    _touch_records(school_id, assessment_ids)


@receiver(post_save, sender=TeachingAssignment)
def assessment_teacher_authority_changed(sender, instance, **kwargs):
    def refresh():
        assessment_ids = list(
            instance.class_subject.assessments.values_list("external_id", flat=True)
        )
        _touch_records(instance.school_id, assessment_ids)

    transaction.on_commit(refresh)


@receiver(post_save, sender=GuardianLink)
def assessment_guardian_link_saved(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_access(instance.student_id, instance.student.school_id)
    )


@receiver(post_delete, sender=GuardianLink)
def assessment_guardian_link_deleted(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_access(instance.student_id, instance.student.school_id)
    )
