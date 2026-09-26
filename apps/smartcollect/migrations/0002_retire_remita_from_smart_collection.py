"""Remita is not a Smart Money Collection provider: it is reserved for a separate Mandates / Direct Debit feature.

While it was being built, Smart Money Collection briefly treated a Remita payment reference (an RRR) as though it were a family's
collection account. This migration takes anything that was made under that idea out of Smart Money Collection, and deletes nothing:

* (a Remita provider connection is disconnected first, by bankconnect 0005: its credential is dropped and it stops being the school's
  active provider. No other provider is switched on in its place; the school chooses one;)
* a family's Remita account is closed (or, if the provider never gave it a number, marked failed), so the family is free to be given a
  Paystack or Monnify account. Nothing is asked of Remita: an RRR it already issued is not cancelled by SchoolOS, and a payment made
  to one is no longer read by SchoolOS;
* a collection batch that had not made accounts yet is cancelled; one that was generating stops, keeping the families that succeeded;
* a planned provider switch that involved Remita is cancelled, and provider calls still queued for Remita are failed;
* payments already recorded, finished batches, applied switches, and every audit row stay exactly as they were.

Each change is written to the collection audit trail. On a school that never used Remita it does nothing.
"""

from django.db import migrations
from django.db.models import Q
from django.utils import timezone

REMITA = "remita"
WHY = "Remita is not a Smart Money Collection provider. It is reserved for Mandates / Direct Debit."
LIVE_ACCOUNT = ("provisioning", "active", "settled", "grace", "dormant", "suspended", "closing")
NOT_STARTED = ("draft", "pending_approval", "rejected", "approved")


def retire_remita(apps, schema_editor):
    Account = apps.get_model("receivables", "FamilyCollectionAccount")
    Batch = apps.get_model("smartcollect", "CollectionGenerationBatch")
    Item = apps.get_model("smartcollect", "CollectionGenerationBatchItem")
    BatchEvent = apps.get_model("smartcollect", "CollectionBatchEvent")
    Job = apps.get_model("smartcollect", "ProviderJob")
    Switch = apps.get_model("smartcollect", "ProviderSwitch")
    Audit = apps.get_model("smartcollect", "CollectionAuditEvent")
    now = timezone.now()

    def audit(school_id, kind, obj_type, obj_id, **detail):
        Audit.objects.create(school_id=school_id, kind=kind, object_type=obj_type, object_id=str(obj_id), detail=detail)

    for switch in Switch.objects.filter(
        Q(from_connection__provider=REMITA) | Q(to_connection__provider=REMITA), status__in=("scheduled", "ready_to_switch")
    ):
        switch.status, switch.cancelled_at, switch.cancel_reason = "cancelled", now, WHY
        switch.save(update_fields=["status", "cancelled_at", "cancel_reason"])
        audit(switch.school_id, "provider_switch_cancelled", "ProviderSwitch", switch.id, why=WHY)

    for batch in Batch.objects.filter(provider=REMITA, status__in=NOT_STARTED + ("processing",)):
        untouched = Item.objects.filter(batch=batch).exclude(generation_status="success")
        if batch.status in NOT_STARTED:
            untouched.update(selected=False, generation_status="skipped")
            batch.status, batch.cancelled_at = "cancelled", now
            batch.save(update_fields=["status", "cancelled_at"])
            kind = "cancelled"
        else:
            untouched.filter(generation_status__in=("pending", "generating", "retrying")).update(
                generation_status="failed", error_code="provider_removed", safe_error_message=WHY,
            )
            batch.success_count = Item.objects.filter(batch=batch, generation_status="success").count()
            batch.failed_count = Item.objects.filter(batch=batch, generation_status="failed").count()
            batch.status = "partially_successful" if batch.success_count else "failed"
            batch.completed_at = now
            batch.save(update_fields=["status", "completed_at", "success_count", "failed_count"])
            kind = "stopped"
        BatchEvent.objects.create(batch=batch, kind=kind, version=batch.version, detail={"why": WHY})
        audit(batch.school_id, f"batch_{kind}_provider_removed", "CollectionGenerationBatch", batch.id, why=WHY)

    Job.objects.filter(status__in=("queued", "running", "retry")).filter(
        Q(item__batch__provider=REMITA) | Q(account__provider=REMITA)
    ).update(status="failed", last_error_code="provider_removed", finished_at=now, lease_until=None)

    for account in Account.objects.filter(provider=REMITA, status__in=LIVE_ACCOUNT):
        made = bool(account.account_number or account.external_account_ref)
        account.status = "closed" if made else "failed"
        account.status_changed_at = account.closed_at = now
        account.close_reason = WHY[:200]
        account.grace_until, account.after_grace = None, ""
        account.save(update_fields=["status", "status_changed_at", "closed_at", "close_reason", "grace_until", "after_grace"])
        audit(account.school_id, "collection_account_retired_provider_removed", "FamilyCollectionAccount", account.id, family=str(account.family_id), why=WHY)


class Migration(migrations.Migration):
    dependencies = [
        ("smartcollect", "0001_initial"),
        ("bankconnect", "0005_retire_remita_connections"),
        ("receivables", "0007_collection_account_reuse_and_grace"),
    ]

    operations = [migrations.RunPython(retire_remita, migrations.RunPython.noop)]
