"""Reading and writing synced records from the server's own code.

The app changes records through `sync/push/`. When the server itself must create
or change records (approving a staff member creates several at once), it uses
these, so the records get proper versions and devices pick the change up on their
next pull exactly as if another device had made it.
"""

from django.db import transaction

from .models import SyncRecord


def read(school, entity_type: str, entity_id: str) -> dict | None:
    """The stored payload, or None if there is no such record or it was deleted."""
    record = SyncRecord.objects.filter(
        school=school, entity_type=entity_type, entity_id=entity_id, deleted=False
    ).first()
    return record.payload if record else None


def write(school, entity_type: str, entity_id: str, payload: dict, *, by=None) -> SyncRecord:
    """Create the record, or replace its payload and bump its version."""
    with transaction.atomic():
        record = (
            SyncRecord.objects.select_for_update()
            .filter(school=school, entity_type=entity_type, entity_id=entity_id)
            .first()
        )
        if record is None:
            return SyncRecord.objects.create(
                school=school, entity_type=entity_type, entity_id=entity_id, payload=payload, updated_by=by
            )
        record.payload = payload
        record.deleted = False
        record.version += 1
        record.updated_by = by
        record.save()
        return record
