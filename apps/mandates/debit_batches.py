"""A direct-debit batch: prepared by a maker, approved by a DIFFERENT checker for exactly the snapshot they saw, and only then run.

    DRAFT -> PENDING_APPROVAL -> APPROVED -> PROCESSING -> COMPLETED | PARTIALLY_SUCCESSFUL | FAILED
                  |                                         (failed debits can be retried)
                  +-> REJECTED (with a reason, back to the maker)        anything not yet run -> CANCELLED

What keeps this honest:
* the maker and the checker are different people - the service says so, and so does the database (`debit_checker_is_not_the_preparer`);
* an approval is for ONE fingerprint of the batch (`snapshot_hash`): anything that changes what would be debited - a family's balance, its mandate,
  the provider connection, an amount, the selection - changes the fingerprint, so a stale preview or a stale approval is refused, and an approval that
  no longer matches is withdrawn and the batch goes back to its maker. One debit is never approved and another executed;
* the preview reads the receivables ledger and the mandates and changes nothing: no provider is called and no ledger entry is made;
* a person can only LOWER a proposed amount, never raise it: what a family can be debited is the ledger's word, not a screen's;
* nothing reaches a provider until the batch is approved and started, and then only through the job queue (see jobs.py), never inside a transaction.

Refusals raised AFTER a state change was committed (a withdrawn approval) are raised outside the transaction, so the change is kept.
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.core.scrub import scrub
from apps.receivables import periods
from apps.receivables.models import Family, FamilyStatus, ReceivableStatus, StudentReceivable

from . import audit, evaluation, notifications, permissions
from .constants import EDITABLE_BATCH, FROZEN_INSTRUCTION, BatchStatus, Eligibility, InstructionStatus
from .errors import MandateRefused
from .models import MandateBatchEvent, MandateDebitBatch, MandateDebitInstruction

MIN_REJECTION_WORDS = 3
#: Events that mean a person worked on the batch: whoever did any of these can never approve it.
MAKER_KINDS = ("created", "preview_refreshed", "selection_changed", "amount_changed", "revised", "submitted", "retried")


# -- finding, locking, recording ------------------------------------------------------------------------


def get_batch(membership, batch_id, *, lock: bool = False) -> MandateDebitBatch:
    """Only ever this school's own: another school's batch answers 404, as if it did not exist."""
    query = MandateDebitBatch.objects.select_related("session", "term", "prepared_by__user").filter(school=membership.school, id=batch_id)
    batch = (query.select_for_update(of=("self",)) if lock else query).first()
    if batch is None:
        raise NotFound("That direct-debit batch was not found.")
    return batch


def record(batch, kind: str, actor=None, **detail) -> MandateBatchEvent:
    return MandateBatchEvent.objects.create(batch=batch, kind=kind, actor=actor, version=batch.version, detail=scrub(detail))


def _need_prepare(membership) -> None:
    if not permissions.can_prepare(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can prepare a direct-debit batch.", "not_preparer")


def _need_approve(membership) -> None:
    if not permissions.can_approve(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can approve a direct-debit batch.", "not_approver")


def makers_of(batch) -> set:
    """Everyone who prepared, changed or submitted this batch."""
    ids = {batch.prepared_by_id, batch.submitted_by_id}
    ids.update(MandateBatchEvent.objects.filter(batch=batch, kind__in=MAKER_KINDS).values_list("actor_id", flat=True))
    ids.discard(None)
    return ids


def _editable(membership, batch_id) -> MandateDebitBatch:
    """The batch, locked, for a maker to change. A rejected batch goes back to being a draft the moment they start revising it."""
    _need_prepare(membership)
    batch = get_batch(membership, batch_id, lock=True)
    if batch.status not in EDITABLE_BATCH:
        raise MandateRefused("This batch can no longer be changed. Only a draft, or a batch that was rejected, can be edited.", "not_editable")
    if batch.status == BatchStatus.REJECTED:
        batch.status = BatchStatus.DRAFT
        batch.save(update_fields=["status", "updated_at"])
        record(batch, "revised", membership, after="rejection")
    return batch


def _check_version(batch, expected_version) -> None:
    if expected_version is not None and int(expected_version) != batch.version:
        raise MandateRefused(
            "This batch was changed since you last looked at it. Refresh it and try again.", "stale_preview", version=batch.version, snapshotHash=batch.snapshot_hash
        )


# -- the preview --------------------------------------------------------------------------------------


def _candidate_families(school, session_id, term_id) -> list[Family]:
    """Families that still have something unpaid in the batch's scope. (What is actually owed is worked out per family from the ledger.)"""
    rows = StudentReceivable.objects.filter(school=school, session_id=session_id).exclude(status__in=[ReceivableStatus.VOID, ReceivableStatus.SETTLED])
    if term_id:
        rows = rows.filter(term_id=term_id)
    ids = set(rows.values_list("family_id", flat=True))
    return list(Family.objects.filter(school=school, id__in=ids, status=FamilyStatus.ACTIVE, merged_into__isnull=True).order_by("display_name", "code"))


def _apply(item: MandateDebitInstruction, ev: evaluation.Evaluation, selected: bool) -> None:
    mandate = ev.mandate
    item.mandate, item.payer = mandate, (mandate.payer if mandate else None)
    item.provider_connection, item.provider = (mandate.provider_connection if mandate else None), (mandate.provider if mandate else "")
    item.mandate_status = mandate.status if mandate else ""
    item.outstanding_minor, item.eligible_minor, item.maximum_minor = ev.outstanding_minor, ev.eligible_minor, ev.maximum_minor
    item.proposed_debit_minor, item.amount_adjusted, item.receivable_allocation = ev.proposed_minor, ev.adjusted, ev.allocation
    item.eligibility_status, item.eligibility_note = ev.status, ev.note[:300]
    item.selected = bool(selected and ev.status in evaluation.SELECTABLE)
    if item.status not in FROZEN_INSTRUCTION:
        item.status = InstructionStatus.PENDING if item.selected else InstructionStatus.SKIPPED
    item.fingerprint = evaluation.fingerprint_of_evaluation(ev, selected=item.selected)


def _totals(batch, items) -> None:
    chosen = [i for i in items if i.selected]
    batch.family_count = len(items)
    batch.total_items = len(chosen)
    batch.total_outstanding_minor = sum(i.outstanding_minor for i in chosen)
    batch.total_amount_minor = sum(i.proposed_debit_minor for i in chosen)


def _settle_hash(batch, items, actor, kind: str, **detail) -> bool:
    """Bring the batch's totals and fingerprint into line with its items. If what would be debited changed, the version rises and the change is
    recorded. Returns whether it changed."""
    _totals(batch, items)
    new = evaluation.batch_hash(batch, [(i.family_id, i.fingerprint) for i in items if i.selected])
    changed = new != batch.snapshot_hash
    if changed:
        if batch.snapshot_hash:  # the first fingerprint is version 1; every later change raises it
            batch.version += 1
        batch.snapshot_hash = new
    batch.save()
    record(batch, kind, actor, changed=changed, selected=batch.total_items, **detail)
    return changed


def _rebuild(batch, actor, kind: str = "preview_refreshed") -> None:
    """Work every family out again from the ledger and the mandates as they are now, keeping each person's own choices where they still make
    sense. Debits already sent are frozen. Nothing is called at any provider and nothing in the ledger changes."""
    today = periods.school_today()
    existing = {i.family_id: i for i in MandateDebitInstruction.objects.select_related("family", "mandate__provider_connection", "mandate__payer__guardian").filter(batch=batch)}
    seen = set()
    for family in _candidate_families(batch.school, batch.session_id, batch.term_id):
        seen.add(family.id)
        prior = existing.get(family.id)
        if prior is not None and prior.status in FROZEN_INSTRUCTION:
            continue
        adjusted = prior.proposed_debit_minor if prior is not None and prior.amount_adjusted else None
        ev = evaluation.evaluate(batch.session_id, batch.term_id, family, today=today, adjusted_to=adjusted)
        if prior is None:
            prior = MandateDebitInstruction(batch=batch, school=batch.school, family=family)
            existing[family.id] = prior
        _apply(prior, ev, selected=bool(prior.selected))
        prior.save()
    for family_id, item in existing.items():
        if family_id not in seen and item.status not in FROZEN_INSTRUCTION and item.selected:
            item.selected, item.status = False, InstructionStatus.SKIPPED
            item.eligibility_status, item.eligibility_note = Eligibility.NOTHING_DUE, "The family no longer owes anything in this period."
            item.fingerprint = ""
            item.save()
    items = list(MandateDebitInstruction.objects.filter(batch=batch))
    batch.preview_built_at = timezone.now()
    _settle_hash(batch, items, actor, kind)


def create_batch(membership, *, session, term=None, title: str = "") -> MandateDebitBatch:
    """Start a draft for a session and term, and build its preview. Nothing is sent to any provider."""
    _need_prepare(membership)
    school = membership.school
    if session is None or session.school_id != school.id:
        raise MandateRefused("Choose one of the school's sessions.", "session_required")
    if term is not None and term.session_id != session.id:
        raise MandateRefused("That term is not in that session.", "term_not_in_session")
    with transaction.atomic():
        batch = MandateDebitBatch.objects.create(
            school=school, session=session, term=term, title=" ".join(str(title or "").split())[:120], prepared_by=membership,
        )
        record(batch, "created", membership, session=str(session.id), term=str(term.id) if term else "")
        _rebuild(batch, membership, "preview_refreshed")
        audit.record(school, "debit_batch_created", actor=membership, obj=batch)
    return batch


@transaction.atomic
def refresh(membership, batch_id, *, expected_version=None) -> MandateDebitBatch:
    batch = _editable(membership, batch_id)
    _check_version(batch, expected_version)
    _rebuild(batch, membership)
    return batch


@transaction.atomic
def set_selection(membership, batch_id, *, select=(), deselect=(), select_all_eligible: bool = False, deselect_all: bool = False, expected_version=None):
    """Choose which families are debited. Only a family that is ready can be chosen: the server says why not for the rest."""
    batch = _editable(membership, batch_id)
    _check_version(batch, expected_version)
    items = list(MandateDebitInstruction.objects.select_related("family").filter(batch=batch))
    by_id = {str(i.id): i for i in items}
    wanted, unwanted = {str(x) for x in select}, {str(x) for x in deselect}
    unknown = (wanted | unwanted) - set(by_id)
    if unknown:
        raise MandateRefused("Some of those families are not in this batch.", "item_not_found")
    refused = []
    for item in items:
        key = str(item.id)
        if item.status in FROZEN_INSTRUCTION:
            if key in wanted | unwanted:
                refused.append(f"{item.family.display_name} has already been debited from this batch")
            continue
        if deselect_all or key in unwanted:
            item.selected = False
        if select_all_eligible and item.eligibility_status in evaluation.SELECTABLE:
            item.selected = True
        if key in wanted:
            if item.eligibility_status not in evaluation.SELECTABLE:
                refused.append(f"{item.family.display_name}: {item.eligibility_note or 'not ready to debit'}")
            else:
                item.selected = True
    if refused:
        raise MandateRefused(refused[0], "cannot_select", problems=refused[:20])
    for item in items:
        if item.status not in FROZEN_INSTRUCTION:
            item.status = InstructionStatus.PENDING if item.selected else InstructionStatus.SKIPPED
            item.fingerprint = evaluation.fingerprint(
                family_id=item.family_id, mandate_id=item.mandate_id, connection_id=item.provider_connection_id, provider=item.provider,
                outstanding_minor=item.outstanding_minor, eligible_minor=item.eligible_minor, maximum_minor=item.maximum_minor,
                proposed_minor=item.proposed_debit_minor, allocation=item.receivable_allocation, ready=item.eligibility_status == Eligibility.ELIGIBLE,
                selected=item.selected,
            )
            item.save()
    _settle_hash(batch, items, membership, "selection_changed", select=len(wanted), deselect=len(unwanted), all_eligible=bool(select_all_eligible), none=bool(deselect_all))
    return batch


@transaction.atomic
def set_amount(membership, batch_id, item_id, *, amount_minor, reason: str = "", expected_version=None) -> MandateDebitBatch:
    """Lower one family's debit. It can never be more than the ledger allows (the server works that out again): a larger figure is refused."""
    batch = _editable(membership, batch_id)
    _check_version(batch, expected_version)
    item = MandateDebitInstruction.objects.select_related("family").filter(batch=batch, id=item_id).first()
    if item is None:
        raise MandateRefused("That family is not in this batch.", "item_not_found")
    if item.status in FROZEN_INSTRUCTION:
        raise MandateRefused("That family has already been debited from this batch.", "already_debited")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise MandateRefused("An amount is a whole number of kobo greater than zero.", "invalid_amount")
    fresh = evaluation.evaluate(batch.session_id, batch.term_id, item.family, mandate=item.mandate)
    if fresh.status != Eligibility.ELIGIBLE:
        raise MandateRefused(fresh.note or "This family cannot be debited now.", "cannot_adjust")
    if amount_minor > fresh.proposed_minor:
        raise MandateRefused("A debit cannot be more than what the family owes and the mandate allows.", "amount_too_high", allowedMinor=fresh.proposed_minor)
    fresh = evaluation.evaluate(batch.session_id, batch.term_id, item.family, mandate=item.mandate, adjusted_to=amount_minor)
    _apply(item, fresh, selected=item.selected)
    item.save()
    audit.record(batch.school, "debit_amount_lowered", actor=membership, obj=item, batch=str(batch.id), amount_minor=amount_minor, reason=" ".join(str(reason or "").split())[:300])
    items = list(MandateDebitInstruction.objects.filter(batch=batch))
    _settle_hash(batch, items, membership, "amount_changed", family=str(item.family_id), amount_minor=amount_minor)
    return batch


@transaction.atomic
def rename(membership, batch_id, title: str) -> MandateDebitBatch:
    batch = _editable(membership, batch_id)
    batch.title = " ".join(str(title or "").split())[:120]
    batch.save(update_fields=["title", "updated_at"])
    return batch


# -- freshness ----------------------------------------------------------------------------------------


def live_fingerprint(batch, item) -> str:
    """The fingerprint this instruction WOULD have if the batch were rebuilt right now. It uses the mandate the instruction was prepared with, so
    a mandate that has since been cancelled, suspended or replaced shows up as a change. Nothing is written."""
    ev = evaluation.evaluate(
        batch.session_id, batch.term_id, item.family, mandate=item.mandate, adjusted_to=item.proposed_debit_minor if item.amount_adjusted else None,
    )
    return evaluation.fingerprint_of_evaluation(ev, selected=item.selected)


def live_hash(batch) -> str:
    """The fingerprint the batch WOULD have if it were rebuilt right now. Debits already sent count as they were. Nothing is written."""
    entries = []
    for item in MandateDebitInstruction.objects.filter(batch=batch, selected=True).select_related("family", "mandate__provider_connection", "mandate__payer__guardian"):
        entries.append((item.family_id, item.fingerprint if item.status in FROZEN_INSTRUCTION else live_fingerprint(batch, item)))
    return evaluation.batch_hash(batch, entries)


def withdraw(batch, actor, why: str) -> None:
    """The batch no longer matches what was submitted or approved: the approval is withdrawn and the batch goes back to its maker."""
    was = batch.status
    batch.status = BatchStatus.DRAFT
    for name in ("approved_by", "approved_at", "submitted_by", "submitted_at"):
        setattr(batch, name, None)
    batch.approved_snapshot_hash, batch.approved_version = "", None
    batch.version += 1
    batch.save()
    record(batch, "approval_invalidated", actor, was=was, why=why)
    audit.record(batch.school, "approval_invalidated", actor=actor, obj=batch, was=was, why=why)


# -- submit, approve, reject ------------------------------------------------------------------------


def _problems(batch, items) -> list[str]:
    chosen = [i for i in items if i.selected]
    problems = [] if chosen else ["Select at least one family."]
    for item in chosen:
        if item.eligibility_status not in evaluation.SELECTABLE:
            problems.append(f"{item.family.display_name}: {item.eligibility_note or 'not ready to debit'}")
    return problems


def submit(membership, batch_id, *, expected_hash: str = "", expected_version=None) -> MandateDebitBatch:
    """Send the batch to be approved. What is submitted is exactly what the maker was looking at: a stale preview is refused."""
    stale = None
    with transaction.atomic():
        _need_prepare(membership)
        batch = get_batch(membership, batch_id, lock=True)
        if batch.status not in EDITABLE_BATCH:
            raise MandateRefused("Only a draft, or a rejected batch, can be submitted.", "not_editable")
        _check_version(batch, expected_version)
        if expected_hash and expected_hash != batch.snapshot_hash:
            raise MandateRefused("This batch was changed since you last looked at it. Refresh it and try again.", "stale_preview", version=batch.version, snapshotHash=batch.snapshot_hash)
        items = list(MandateDebitInstruction.objects.select_related("family").filter(batch=batch))
        problems = _problems(batch, items)
        if problems:
            raise MandateRefused(problems[0], "batch_not_ready", problems=problems[:20])
        if live_hash(batch) != batch.snapshot_hash:
            stale = "What families owe, or their mandates, changed since the preview was built. Refresh the preview and look at it again."
        else:
            if batch.status == BatchStatus.REJECTED:
                record(batch, "revised", membership, after="rejection")
            batch.status = BatchStatus.PENDING_APPROVAL
            batch.submitted_by, batch.submitted_at = membership, timezone.now()
            batch.rejected_by = batch.rejected_at = None
            batch.rejection_reason = ""
            batch.save()
            record(batch, "submitted", membership, hash=batch.snapshot_hash, selected=batch.total_items, total=batch.total_amount_minor)
            audit.record(batch.school, "debit_batch_submitted", actor=membership, obj=batch, selected=batch.total_items, total_minor=batch.total_amount_minor)
    if stale:
        raise MandateRefused(stale, "stale_preview", version=batch.version, snapshotHash=batch.snapshot_hash)
    notifications.batch_waiting(batch)
    return batch


def approve(membership, batch_id, *, expected_hash: str) -> MandateDebitBatch:
    """Approve exactly what was submitted. The approver is never anyone who prepared, changed or submitted the batch."""
    withdrawn = None
    with transaction.atomic():
        _need_approve(membership)
        batch = get_batch(membership, batch_id, lock=True)
        if batch.status != BatchStatus.PENDING_APPROVAL:
            raise MandateRefused("This batch is not waiting for approval.", "not_pending")
        if membership.id in makers_of(batch):
            raise MandateRefused("You prepared or changed this batch, so someone else must approve it.", "maker_cannot_approve")
        if not expected_hash or expected_hash != batch.snapshot_hash:
            raise MandateRefused("This batch changed after you opened it. Open it again and review what it now contains.", "stale_approval", version=batch.version, snapshotHash=batch.snapshot_hash)
        if live_hash(batch) != batch.snapshot_hash:
            withdrawn = "What families owe, or their mandates, changed since this batch was submitted."
            withdraw(batch, membership, withdrawn)
        else:
            now = timezone.now()
            batch.status = BatchStatus.APPROVED
            batch.approved_by, batch.approved_at = membership, now
            batch.approved_snapshot_hash, batch.approved_version = batch.snapshot_hash, batch.version
            batch.save()
            record(batch, "approved", membership, hash=batch.snapshot_hash, selected=batch.total_items, total=batch.total_amount_minor)
            audit.record(batch.school, "debit_batch_approved", actor=membership, obj=batch, hash=batch.snapshot_hash, selected=batch.total_items, total_minor=batch.total_amount_minor)
    if withdrawn:
        raise MandateRefused(withdrawn + " Its approval was withdrawn and it is back with the maker.", "batch_changed", version=batch.version)
    return batch


@transaction.atomic
def reject(membership, batch_id, *, reason: str) -> MandateDebitBatch:
    """Send it back to the maker, with the reason. Everything stays: the batch, its history and the reason."""
    _need_approve(membership)
    batch = get_batch(membership, batch_id, lock=True)
    if batch.status != BatchStatus.PENDING_APPROVAL:
        raise MandateRefused("This batch is not waiting for approval.", "not_pending")
    if membership.id in makers_of(batch):
        raise MandateRefused("You prepared or changed this batch, so someone else must decide on it.", "maker_cannot_approve")
    reason = " ".join(str(reason or "").split())
    if len(reason.split()) < MIN_REJECTION_WORDS:
        raise MandateRefused("Say why you are rejecting it, so the person who prepared it knows what to change.", "reason_required")
    if len(reason) > 500:
        raise MandateRefused("A reason can be at most 500 characters.", "reason_too_long")
    batch.status = BatchStatus.REJECTED
    batch.rejected_by, batch.rejected_at, batch.rejection_reason = membership, timezone.now(), reason
    batch.save()
    record(batch, "rejected", membership, reason=reason)
    audit.record(batch.school, "debit_batch_rejected", actor=membership, obj=batch, reason=reason)
    transaction.on_commit(lambda: notifications.batch_rejected(batch))
    return batch


@transaction.atomic
def cancel(membership, batch_id, *, reason: str = "") -> MandateDebitBatch:
    _need_prepare(membership)
    batch = get_batch(membership, batch_id, lock=True)
    if batch.status not in (BatchStatus.DRAFT, BatchStatus.PENDING_APPROVAL, BatchStatus.REJECTED, BatchStatus.APPROVED):
        raise MandateRefused("A batch that is debiting, or that already debited, cannot be cancelled.", "cannot_cancel")
    batch.status = BatchStatus.CANCELLED
    batch.cancelled_by, batch.cancelled_at = membership, timezone.now()
    batch.save()
    MandateDebitInstruction.objects.filter(batch=batch).exclude(status__in=FROZEN_INSTRUCTION).update(selected=False, status=InstructionStatus.CANCELLED)
    record(batch, "cancelled", membership, reason=" ".join(str(reason or "").split())[:300])
    audit.record(batch.school, "debit_batch_cancelled", actor=membership, obj=batch)
    return batch
