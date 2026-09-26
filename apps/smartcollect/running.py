"""Running an approved batch: queueing the provider work, retrying what failed, and counting how it went.

Split from `batches` (which prepares, submits and decides): everything here begins only once a batch is APPROVED, and the calls it queues are
made later by the job worker, never here and never inside a transaction.
"""

import hashlib

from django.db import transaction
from django.utils import timezone

from apps.bankconnect import permissions as authority

from . import audit
from .batches import FROZEN, _event, _need_prepare, _still_the_provider, _withdraw, get_batch, live_hash
from .constants import BatchStatus, GenerationStatus
from .errors import CollectionRefused
from .models import CollectionGenerationBatch, CollectionGenerationBatchItem


def generation_key(batch, family_id) -> str:
    """The same for every attempt of one family in one batch: what makes generation safe to repeat."""
    return "gen-" + hashlib.sha256(f"{batch.school_id}:{batch.id}:{family_id}".encode()).hexdigest()[:40]


def provider_reference(item) -> str:
    """The reference SchoolOS gives the provider for this family's account. Deterministic, so a repeat finds the same account."""
    return "SOS-" + item.idempotency_key.removeprefix("gen-")[:24].upper()


def _queue(batch, items) -> int:
    """Queue the provider work for these items. An item that has been through a round of work before (one that failed, and is being made
    again after a retry or a fresh approval) starts a new round, so its earlier finished job is never mistaken for the new one; the
    idempotency key, and so the provider reference, stay the same."""
    from . import jobs
    from .models import ProviderJob

    queued = 0
    for item in items:
        item.idempotency_key = item.idempotency_key or generation_key(batch, item.family_id)
        item.provider_request_reference = item.provider_request_reference or provider_reference(item)
        if ProviderJob.objects.filter(idempotency_key=f"provision:{item.id}:{item.retry_round}").exists():
            item.retry_round += 1
        item.generation_status = GenerationStatus.PENDING
        item.error_code = item.safe_error_message = ""
        item.save()
        jobs.enqueue_provision(item)
        queued += 1
    return queued


def start_processing(membership, batch_id) -> CollectionGenerationBatch:
    """Begin generating. Only an APPROVED batch, and only for the exact snapshot that was approved: this is where the fingerprint the
    checker saw is compared with what would be run. The provider calls themselves are queued, never made here."""
    withdrawn = None
    with transaction.atomic():
        if not (authority.can_prepare(membership) or authority.can_approve(membership)):
            raise CollectionRefused("Only someone who prepares or approves collection batches can start generating.", "not_preparer")
        batch = get_batch(membership, batch_id, lock=True)
        if batch.status != BatchStatus.APPROVED:
            raise CollectionRefused("Only an approved batch can be generated.", "not_approved")
        if not batch.approved_snapshot_hash or batch.approved_snapshot_hash != batch.snapshot_hash:
            raise CollectionRefused("This batch no longer matches what was approved.", "approval_mismatch")
        _still_the_provider(batch)
        if live_hash(batch) != batch.approved_snapshot_hash:
            withdrawn = "What families owe, the policy or their accounts changed after this batch was approved."
            _withdraw(batch, membership, withdrawn)
        else:
            todo = list(
                CollectionGenerationBatchItem.objects.select_related("family").select_for_update(of=("self",)).filter(batch=batch, selected=True)
                .exclude(generation_status__in=FROZEN)
            )
            queued = _queue(batch, todo)
            batch.status = BatchStatus.PROCESSING
            batch.started_by, batch.started_at = membership, timezone.now()
            batch.completed_at = None
            batch.save()
            _event(batch, "started", membership, queued=queued)
            audit.record(batch.school, "batch_started", actor=membership, obj=batch, families=queued)
    if withdrawn:
        raise CollectionRefused(withdrawn + " Its approval was withdrawn and it is back with the maker.", "batch_changed", version=batch.version)
    return batch


def retry_failed(membership, batch_id, *, item_ids=None):
    """Try the failed families again. Under an UNCHANGED snapshot the earlier approval still stands and they are queued again; if the
    snapshot has changed since it was approved, the approval no longer covers them and the batch goes back to be approved afresh.
    Returns `(batch, retried)`: `retried` is how many were queued (0 when approval is needed again)."""
    withdrawn = None
    retried = 0
    with transaction.atomic():
        _need_prepare(membership)
        batch = get_batch(membership, batch_id, lock=True)
        if batch.status not in (BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED):
            raise CollectionRefused("Only a batch that finished with failures can be retried.", "nothing_to_retry")
        failed = list(
            CollectionGenerationBatchItem.objects.select_related("family").select_for_update(of=("self",)).filter(
                batch=batch, selected=True, generation_status=GenerationStatus.FAILED
            )
        )
        if item_ids is not None:
            wanted = {str(x) for x in item_ids}
            unknown = wanted - {str(i.id) for i in failed}
            if unknown:
                raise CollectionRefused("Some of those families did not fail, or are not in this batch.", "item_not_failed")
            failed = [i for i in failed if str(i.id) in wanted]
        if not failed:
            raise CollectionRefused("There are no failed families to retry.", "nothing_to_retry")
        _still_the_provider(batch)
        if live_hash(batch) != batch.approved_snapshot_hash:
            withdrawn = "What families owe, the policy or their accounts changed since this batch was approved, so the retry needs a fresh approval."
            _withdraw(batch, membership, withdrawn)
        else:
            retried = _queue(batch, failed)
            batch.status = BatchStatus.PROCESSING
            batch.completed_at = None
            batch.save()
            _event(batch, "retried", membership, retried=retried)
            audit.record(batch.school, "batch_retry_started", actor=membership, obj=batch, families=retried)
    return batch, retried if not withdrawn else 0


def refresh_progress(batch_id) -> CollectionGenerationBatch | None:
    """Count what has happened and, when every selected family is done, say how the batch ended. Partial failure keeps every success."""
    with transaction.atomic():
        batch = CollectionGenerationBatch.objects.select_for_update().filter(pk=batch_id).first()
        if batch is None:
            return None
        counts = {s: 0 for s in GenerationStatus.values}
        for status in CollectionGenerationBatchItem.objects.filter(batch=batch, selected=True).values_list("generation_status", flat=True):
            counts[status] += 1
        batch.success_count, batch.failed_count = counts[GenerationStatus.SUCCESS], counts[GenerationStatus.FAILED]
        if batch.status == BatchStatus.PROCESSING and not (counts[GenerationStatus.PENDING] or counts[GenerationStatus.GENERATING] or counts[GenerationStatus.RETRYING]):
            if counts[GenerationStatus.FAILED] == 0:
                batch.status = BatchStatus.COMPLETED
            elif counts[GenerationStatus.SUCCESS] == 0:
                batch.status = BatchStatus.FAILED
            else:
                batch.status = BatchStatus.PARTIALLY_SUCCESSFUL
            batch.completed_at = timezone.now()
            _event(batch, "finished", None, status=batch.status, success=batch.success_count, failed=batch.failed_count)
        batch.save()
        return batch


def progress(batch) -> dict:
    counts = {s: 0 for s in GenerationStatus.values}
    for status in CollectionGenerationBatchItem.objects.filter(batch=batch, selected=True).values_list("generation_status", flat=True):
        counts[status] += 1
    total = sum(counts.values())
    done = counts[GenerationStatus.SUCCESS] + counts[GenerationStatus.FAILED]
    return {
        "status": batch.status, "total": total, "successful": counts[GenerationStatus.SUCCESS], "failed": counts[GenerationStatus.FAILED],
        "waiting": counts[GenerationStatus.PENDING] + counts[GenerationStatus.RETRYING], "generating": counts[GenerationStatus.GENERATING],
        "finished": batch.status not in (BatchStatus.PROCESSING, BatchStatus.APPROVED) and done == total,
    }
