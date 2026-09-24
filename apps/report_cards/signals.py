from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.students.models import GuardianLink
from apps.sync.models import SyncRecord

from .models import ReportCard
from .services import REPORT_CARD_ENTITY


def _touch_student_report_cards(student_id, school_id):
    external_ids = list(
        ReportCard.objects.filter(student_id=student_id).values_list("external_id", flat=True)
    )
    ids = [value for value in set(external_ids) if value]
    if not ids:
        return
    with transaction.atomic():
        records = SyncRecord.objects.select_for_update().filter(
            school_id=school_id,
            entity_type=REPORT_CARD_ENTITY,
            entity_id__in=ids,
            deleted=False,
        )
        for record in records:
            # A newly (or no-longer) linked Parent's access changed even
            # though the report card's own content did not.
            record.save(update_fields=["updated_by"])


@receiver(post_save, sender=GuardianLink)
def report_card_guardian_link_saved(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_report_cards(instance.student_id, instance.student.school_id)
    )


@receiver(post_delete, sender=GuardianLink)
def report_card_guardian_link_deleted(sender, instance, **kwargs):
    transaction.on_commit(
        lambda: _touch_student_report_cards(instance.student_id, instance.student.school_id)
    )
