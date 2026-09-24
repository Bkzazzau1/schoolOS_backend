from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.academics.models import TeachingAssignment
from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .models import AssignmentRecipient
from .services import ASSIGNMENT_ENTITY, SUBMISSION_ENTITY


def _touch_records(school_id, entity_type, entity_ids):
    ids = [value for value in set(entity_ids) if value]
    if not ids:
        return
    with transaction.atomic():
        records = SyncRecord.objects.select_for_update().filter(
            school_id=school_id,
            entity_type=entity_type,
            entity_id__in=ids,
            deleted=False,
        )
        for record in records:
            # Visibility changed even if content did not. Saving assigns a fresh
            # school sync sequence without pretending content got a new version.
            record.save(update_fields=["updated_by"])


def _touch_student_access(student_id, school_id):
    recipients = AssignmentRecipient.objects.filter(student_id=student_id).select_related(
        "assignment"
    )
    assignment_ids = [row.assignment.external_id for row in recipients]
    submission_ids = [
        row.submission.external_id
        for row in recipients
        if hasattr(row, "submission")
    ]
    _touch_records(school_id, ASSIGNMENT_ENTITY, assignment_ids)
    _touch_records(school_id, SUBMISSION_ENTITY, submission_ids)


@receiver(post_save, sender=TeachingAssignment)
def assignment_teacher_authority_changed(sender, instance, **kwargs):
    def refresh():
        assignments = list(instance.class_subject.assignments.all())
        _touch_records(
            instance.school_id,
            ASSIGNMENT_ENTITY,
            [item.external_id for item in assignments],
        )
        submission_ids = []
        for item in assignments:
            submission_ids.extend(
                item.submissions.values_list("external_id", flat=True)
            )
        _touch_records(instance.school_id, SUBMISSION_ENTITY, submission_ids)

    transaction.on_commit(refresh)


@receiver(post_save, sender=GuardianLink)
def assignment_guardian_link_saved(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_access(instance.student_id, instance.student.school_id)
    )


@receiver(post_delete, sender=GuardianLink)
def assignment_guardian_link_deleted(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_access(instance.student_id, instance.student.school_id)
    )
