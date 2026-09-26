"""Sending approved debits to a provider, safely.

A debit is the most sensitive thing in this app: it takes money from a person's account. So:

* nothing is sent until the batch is approved for an exact fingerprint and someone starts it;
* every debit goes through the job queue (see jobs.py) and the provider is asked with NO transaction open and no lock held. The intent is written
  first (a `MandateTransaction` row, in its own committed transaction), then the provider is asked, then the answer is written in another;
* JUST BEFORE the provider is asked, the ledger, the mandate and the provider connection are read again. If anything that was approved has changed -
  the family paid meanwhile, the mandate was cancelled, the amount the ledger allows moved - NOTHING is sent: the debit is stopped and needs a fresh
  review. It is never quietly shrunk and executed as a different debit;
* every debit has one deterministic reference (`request_ref`), and the provider is asked about that reference. A provider that does not answer
  leaves the outcome UNKNOWN: the debit may have happened. It is then ASKED ABOUT until the provider says - it is never simply sent again. Only a
  provider's own "I have no record of that request" makes it safe to send again, with the same reference;
* only a debit the provider confirms is settled into the ledger (see settlement.py), and only once;
* a debit that failed for a reason outside SchoolOS (insufficient funds) can be retried under the same approval, but only if nothing that was approved
  has changed; if it has, it needs a fresh maker and checker.
"""

import hashlib
import logging

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from . import audit, debit_batches, evaluation, identifiers, notifications, permissions, settlement
from .connections import open_for_provider, provider_call_args, record_failure, record_success
from .constants import MAX_ATTEMPTS, BatchStatus, DebitOutcome, InstructionStatus, JobStatus
from .errors import MandateRefused
from .models import MandateDebitBatch, MandateDebitInstruction, MandateProviderJob, MandateTransaction
from .outcomes import DONE, FAILED, RETRY, Outcome
from .providers.base import BadCredentials, ConnectorError, DebitRequest, DebitResult, NotSupported, ProviderUnavailable
from .vault import VaultError, account_context_for, get_vault

logger = logging.getLogger(__name__)

#: Settled for good: a debit in one of these is finished with.
_DONE = (InstructionStatus.SUCCESS, InstructionStatus.CANCELLED, InstructionStatus.SKIPPED)
#: Still being carried out or asked about.
_IN_FLIGHT = (InstructionStatus.PENDING, InstructionStatus.DEBITING, InstructionStatus.UNKNOWN, InstructionStatus.RETRYING)
#: In words a payer or a person can read.
FAILURE_WORDS = {
    "insufficient_funds": "The account did not have enough money.",
    "payment_limit_exceeded": "The debit was more than the mandate allows.",
    "mandate_not_activated": "The mandate is not activated yet.",
    "mandate_not_due": "The mandate is not due yet.",
    "mandate_expired": "The mandate has expired.",
    "mandate_deactivated": "The mandate has been stopped.",
    "invalid_funding_source": "The provider did not accept the account.",
    "invalid_mandate_type": "The provider did not accept this kind of mandate for a debit.",
    "bad_credentials": "The provider refused the school's credentials.",
    "invalid_request": "The provider did not accept the debit request.",
    "changed_since_approval": "What the family owes, or the mandate, changed after approval. Nothing was debited.",
    "not_sent": "The provider never received the debit.",
    "provider_failed": "The provider could not carry out the debit.",
    "reversed": "The provider reversed the debit.",
    "pending_documentation": "This provider's debit instructions are not available through SchoolOS yet.",
    "vault_error": "The stored details could not be opened.",
}
#: A failure that a retry under the same approval can never fix: the world changed, so a fresh approval is needed.
NEEDS_FRESH_APPROVAL = ("changed_since_approval",)
NOT_RETRYABLE = NEEDS_FRESH_APPROVAL + ("pending_documentation", "reversed")


def _words(code: str) -> str:
    return FAILURE_WORDS.get(code, "The debit did not go through.")


def _need_run(membership) -> None:
    if not (permissions.can_prepare(membership) or permissions.can_approve(membership)):
        raise MandateRefused("Only someone who prepares or approves direct-debit batches can start one.", "not_operator")


def _reference(item) -> str:
    """The reference the provider is given for this debit: deterministic, numeric, and different for each deliberate retry round."""
    return identifiers.numeric_reference("debit", item.idempotency_key, item.retry_round)


def _idempotency_key(batch, item) -> str:
    return "dd-" + hashlib.sha256(f"{batch.id}|{item.id}|{item.family_id}".encode()).hexdigest()[:32]


# -- starting ------------------------------------------------------------------------------------------


def start(membership, batch_id) -> MandateDebitBatch:
    """Start debiting an APPROVED batch: every selected family's debit is put in the queue, in the same transaction as the decision."""
    from . import jobs

    withdrawn = None
    with transaction.atomic():
        _need_run(membership)
        batch = debit_batches.get_batch(membership, batch_id, lock=True)
        if batch.status != BatchStatus.APPROVED:
            raise MandateRefused("Only an approved batch can be started.", "not_approved")
        if not batch.approved_snapshot_hash or batch.approved_snapshot_hash != batch.snapshot_hash:
            raise MandateRefused("This batch's approval is not for what it now contains. It needs approving again.", "approval_mismatch")
        if debit_batches.live_hash(batch) != batch.approved_snapshot_hash:
            withdrawn = "What families owe, or their mandates, changed after this batch was approved."
            debit_batches.withdraw(batch, membership, withdrawn)
        else:
            now = timezone.now()
            items = list(MandateDebitInstruction.objects.select_for_update(of=("self",)).filter(batch=batch, selected=True))
            for item in items:
                item.idempotency_key = item.idempotency_key or _idempotency_key(batch, item)
                item.request_ref = _reference(item)
                item.status = InstructionStatus.PENDING
                item.save()
                jobs.enqueue_debit(item)
            batch.status, batch.started_by, batch.started_at = BatchStatus.PROCESSING, membership, now
            batch.save()
            debit_batches.record(batch, "started", membership, debits=len(items), total=batch.total_amount_minor)
            audit.record(batch.school, "debit_started", actor=membership, obj=batch, debits=len(items), total_minor=batch.total_amount_minor)
    if withdrawn:
        raise MandateRefused(withdrawn + " Its approval was withdrawn and it is back with the maker.", "batch_changed", version=batch.version)
    return batch


# -- a debit: three phases ------------------------------------------------------------------------------


def _stop_stale(item_id) -> Outcome:
    with transaction.atomic():
        item = MandateDebitInstruction.objects.select_for_update(of=("self",)).select_related("batch", "family").get(pk=item_id)
        item.status, item.error_code, item.safe_error_message = InstructionStatus.FAILED, "changed_since_approval", _words("changed_since_approval")
        item.completed_at = timezone.now()
        item.save()
        audit.record(item.school, "approval_invalidated", obj=item, batch=str(item.batch_id), family=str(item.family_id), why="changed_since_approval")
        _update_batch(item.batch_id)
    return Outcome(FAILED, "changed_since_approval")


def _is_fresh(item) -> bool:
    """The debit is still exactly what was approved: the same mandate, provider, amount and charges, and what the ledger allows still allows it."""
    batch = item.batch
    ev = evaluation.evaluate(
        batch.session_id, batch.term_id, item.family, mandate=item.mandate, adjusted_to=item.proposed_debit_minor if item.amount_adjusted else None,
    )
    return (
        ev.status == "eligible" and ev.proposed_minor == item.proposed_debit_minor
        and evaluation.fingerprint_of_evaluation(ev, selected=True) == item.fingerprint
    )


@sensitive_variables("account", "sealed", "secret")
def execute_instruction(instruction_id, *, attempt: int = 1) -> Outcome:
    """Carry out one debit. Called by the worker, with no transaction open."""
    from . import jobs

    # -- phase 1: decide, and write the intent, in one short transaction -----------------------------
    with transaction.atomic():
        item = MandateDebitInstruction.objects.select_for_update(of=("self",)).select_related(
            "batch", "family", "mandate__provider_connection", "mandate__payer__guardian",
        ).get(pk=instruction_id)
        if item.status in _DONE or item.batch.status != BatchStatus.PROCESSING:
            return Outcome(DONE)
        earlier = item.transactions.filter(request_ref=item.request_ref).first()
        if earlier is not None and earlier.status in (DebitOutcome.PENDING, DebitOutcome.UNKNOWN, DebitOutcome.SUCCESS, DebitOutcome.REVERSED, DebitOutcome.REFUNDED):
            # A request with this reference may have reached the provider: ASK. Never send again.
            if item.status != InstructionStatus.UNKNOWN and earlier.status != DebitOutcome.SUCCESS:
                item.status = InstructionStatus.UNKNOWN
                item.save(update_fields=["status", "updated_at"])
            jobs.enqueue_requery(item)
            return Outcome(DONE, "requery")
        if item.mandate is None or not _is_fresh(item):
            fresh = False
        else:
            fresh = True
        if fresh:
            mandate = item.mandate
            item.status, item.attempt_count, item.last_attempt_at = InstructionStatus.DEBITING, item.attempt_count + 1, timezone.now()
            item.save()
            tx = earlier or MandateTransaction(
                school=item.school, family=item.family, payer=mandate.payer, mandate=mandate, instruction=item, provider_connection=mandate.provider_connection,
                provider=mandate.provider, request_ref=item.request_ref, amount_minor=item.proposed_debit_minor,
            )
            tx.status, tx.provider_status, tx.failure_code = DebitOutcome.PENDING, "requested", ""
            tx.save()
            audit.record(item.school, "debit_requested", obj=item, batch=str(item.batch_id), mandate=str(mandate.id), amount_minor=item.proposed_debit_minor)
            sealed, connection = bytes(mandate.sealed_account_details or b""), mandate.provider_connection
            request = DebitRequest(
                request_ref=item.request_ref, mandate_ref=mandate.provider_mandate_reference, mandate_code=mandate.mandate_code,
                amount_minor=item.proposed_debit_minor, funding_bank_code=mandate.bank_code, narration="School fees",
            )
    if not fresh:
        return _stop_stale(instruction_id)

    # -- phase 2: the provider is asked, with nothing held --------------------------------------------
    try:
        connector, secret = open_for_provider(connection)
        if connector.info.capabilities.requires_account_number_for_debit:
            opened = get_vault().open(account_context_for(mandate), sealed) if sealed else {}
            if not opened.get("account_number"):
                raise VaultError("The account details are not held.")
            request = DebitRequest(**{**request.__dict__, "funding_account": opened["account_number"]})
        result = connector.create_debit(secret, request, **provider_call_args(connection))
    except ProviderUnavailable:
        return _record(instruction_id, None, unknown=True, source="debit")
    except BadCredentials:
        record_failure(connection, "bad_credentials")
        return _record(instruction_id, DebitResult(outcome=DebitOutcome.FAILED, failure_code="bad_credentials"), source="debit")
    except NotSupported as error:
        return _record(instruction_id, DebitResult(outcome=DebitOutcome.FAILED, failure_code=error.code), source="debit")
    except ConnectorError as error:
        return _record(instruction_id, DebitResult(outcome=DebitOutcome.FAILED, failure_code="invalid_request" if error.code == "invalid_request" else "provider_failed"), source="debit")
    except VaultError:
        return _record(instruction_id, DebitResult(outcome=DebitOutcome.FAILED, failure_code="vault_error"), source="debit")
    record_success(connection)
    # -- phase 3: what the provider said, in one short transaction ------------------------------------
    return _record(instruction_id, result, source="debit")


def _update_batch(batch_id) -> None:
    """The batch is finished when nothing in it is still being carried out or asked about (an UNKNOWN debit keeps it open)."""
    batch = MandateDebitBatch.objects.select_for_update(of=("self",)).get(pk=batch_id)
    items = list(MandateDebitInstruction.objects.filter(batch=batch, selected=True))
    batch.success_count = sum(1 for i in items if i.status == InstructionStatus.SUCCESS)
    batch.failed_count = sum(1 for i in items if i.status == InstructionStatus.FAILED)
    if batch.status in (BatchStatus.PROCESSING, BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED, BatchStatus.COMPLETED) and items:
        if any(i.status in _IN_FLIGHT for i in items):
            batch.status, batch.completed_at = BatchStatus.PROCESSING, None
        elif batch.failed_count == 0:
            batch.status, batch.completed_at = BatchStatus.COMPLETED, timezone.now()
        elif batch.success_count:
            batch.status, batch.completed_at = BatchStatus.PARTIALLY_SUCCESSFUL, timezone.now()
        else:
            batch.status, batch.completed_at = BatchStatus.FAILED, timezone.now()
    batch.save()


def _record(instruction_id, result: DebitResult | None, *, unknown: bool = False, source: str) -> Outcome:
    """Write what the provider said about a debit. Idempotent: what is already settled is never touched again."""
    from . import jobs

    to_settle = None
    with transaction.atomic():
        item = MandateDebitInstruction.objects.select_for_update(of=("self",)).select_related("batch", "family", "mandate__payer__guardian").get(pk=instruction_id)
        tx = item.transactions.select_for_update().filter(request_ref=item.request_ref).first()
        if item.status == InstructionStatus.SUCCESS or tx is None:
            return Outcome(DONE)
        outcome = DebitOutcome.UNKNOWN if unknown or result is None else result.outcome
        if result is not None:
            tx.provider_status, tx.provider_status_code = result.provider_status[:120], result.provider_status_code[:20]
            tx.provider_transaction_reference = result.provider_reference or tx.provider_transaction_reference
            tx.provider_meta = {**(tx.provider_meta or {}), **result.meta}
            item.provider_status, item.provider_status_code = tx.provider_status, tx.provider_status_code
            item.provider_transaction_reference = tx.provider_transaction_reference
        if outcome == DebitOutcome.SUCCESS:
            tx.status, tx.confirmed_at = DebitOutcome.SUCCESS, timezone.now()
            item.status, item.completed_at, item.error_code, item.safe_error_message = InstructionStatus.SUCCESS, timezone.now(), "", ""
            tx.save()
            item.save()
            to_settle = tx
            audit.record(item.school, "debit_succeeded", obj=item, batch=str(item.batch_id), amount_minor=tx.amount_minor)
        elif outcome == DebitOutcome.PENDING:
            tx.status = DebitOutcome.PENDING
            item.status = InstructionStatus.DEBITING
            tx.save()
            item.save()
            jobs.enqueue_requery(item, delay=True)
            audit.record(item.school, "debit_pending", obj=item, batch=str(item.batch_id))
        elif outcome == DebitOutcome.NOT_FOUND:
            # The provider says it never received this request: it can be sent again, with the same reference.
            tx.status, tx.failure_code = DebitOutcome.FAILED, "not_sent"
            tx.save()
            if item.attempt_count >= MAX_ATTEMPTS:
                item.status, item.error_code, item.safe_error_message, item.completed_at = InstructionStatus.FAILED, "not_sent", _words("not_sent"), timezone.now()
            else:
                item.status = InstructionStatus.RETRYING
                jobs.enqueue_debit(item, tag=f"resend{item.attempt_count}")
            item.save()
        elif outcome in (DebitOutcome.FAILED, DebitOutcome.REVERSED, DebitOutcome.REFUNDED):
            code = (result.failure_code if result else "") or ("reversed" if outcome != DebitOutcome.FAILED else "provider_failed")
            tx.status, tx.failure_code = DebitOutcome.FAILED, code[:40]
            item.status, item.error_code, item.safe_error_message, item.completed_at = InstructionStatus.FAILED, code[:40], _words(code), timezone.now()
            tx.save()
            item.save()
            audit.record(item.school, "debit_failed", obj=item, batch=str(item.batch_id), code=code)
            transaction.on_commit(lambda: notifications.debit_failed(item, _words(code)))
        else:  # unknown: the provider did not answer, or answered something SchoolOS does not understand. It MAY have happened.
            tx.status = DebitOutcome.UNKNOWN
            item.status = InstructionStatus.UNKNOWN
            tx.save()
            item.save()
            jobs.enqueue_requery(item, delay=True)
            audit.record(item.school, "debit_outcome_unknown", obj=item, batch=str(item.batch_id))
        _update_batch(item.batch_id)
    if to_settle is not None:
        settlement.settle(to_settle)
        notifications.debit_succeeded(item, to_settle.amount_minor)
    return Outcome(DONE, outcome)


# -- asking what happened -------------------------------------------------------------------------------


@sensitive_variables("secret")
def requery_instruction(instruction_id, *, attempt: int = 1, force: bool = False) -> Outcome:
    """Ask the provider what happened to a debit whose outcome is not final. The provider's answer, and only that, decides. `force` asks even
    about one already settled (a callback said something happened to it: a reversal, a refund)."""
    item = MandateDebitInstruction.objects.select_related("mandate__provider_connection", "family").get(pk=instruction_id)
    finished = item.status in (InstructionStatus.CANCELLED, InstructionStatus.SKIPPED, InstructionStatus.FAILED) or (item.status == InstructionStatus.SUCCESS and not force)
    if finished or not item.request_ref or item.mandate is None:
        return Outcome(DONE)
    mandate, connection = item.mandate, item.mandate.provider_connection
    try:
        connector, secret = open_for_provider(connection)
        result = connector.get_debit_status(
            secret, mandate_ref=mandate.provider_mandate_reference, request_ref=item.request_ref, **provider_call_args(connection),
            provider_customer_ref=mandate.provider_customer_ref, bank_code=mandate.bank_code,
        )
    except (ProviderUnavailable, BadCredentials) as error:
        if isinstance(error, BadCredentials):
            record_failure(connection, "bad_credentials")
        if attempt >= MAX_ATTEMPTS:
            audit.record(item.school, "debit_outcome_unresolved", obj=item, batch=str(item.batch_id))
        return Outcome(RETRY, getattr(error, "code", "provider_unavailable"))
    except (ConnectorError, VaultError) as error:
        return Outcome(FAILED, getattr(error, "code", "vault_error"))
    record_success(connection)
    return _record_after_query(instruction_id, result, attempt)


def _record_after_query(instruction_id, result: DebitResult, attempt: int) -> Outcome:
    outcome = _record(instruction_id, result, source="requery")
    item = MandateDebitInstruction.objects.get(pk=instruction_id)
    if result.outcome in (DebitOutcome.REVERSED, DebitOutcome.REFUNDED):
        tx = item.transactions.filter(request_ref=item.request_ref).first()
        if tx is not None and tx.status == DebitOutcome.SUCCESS:
            settlement.reverse(tx, outcome=result.outcome, reason="The provider reported the debit as " + result.outcome + ".")
    return outcome


# -- retrying ---------------------------------------------------------------------------------------------


def retry(membership, batch_id, *, item_ids=None) -> dict:
    """Try the failed debits again under the SAME approval - but only those whose approved facts are all unchanged. A debit whose outcome is not
    known is never retried (it is asked about); one that changed needs a fresh maker and checker."""
    from . import jobs

    _need_run(membership)
    retried, needs_approval = [], []
    with transaction.atomic():
        batch = debit_batches.get_batch(membership, batch_id, lock=True)
        if batch.status not in (BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED):
            raise MandateRefused("Only a batch that finished with failures can be retried.", "not_retryable")
        if not batch.approved_snapshot_hash or batch.approved_snapshot_hash != batch.snapshot_hash:
            raise MandateRefused("This batch's approval is not for what it now contains. It needs approving again.", "approval_mismatch")
        wanted = {str(x) for x in item_ids} if item_ids else None
        items = MandateDebitInstruction.objects.select_for_update(of=("self",)).select_related("family", "mandate__provider_connection").filter(
            batch=batch, selected=True, status=InstructionStatus.FAILED,
        )
        for item in items:
            if wanted is not None and str(item.id) not in wanted:
                continue
            if item.error_code in NOT_RETRYABLE or item.mandate is None or not _is_fresh(item):
                needs_approval.append({"itemId": str(item.id), "family": item.family.display_name, "code": item.error_code})
                continue
            item.retry_round += 1
            item.request_ref = _reference(item)
            item.status, item.error_code, item.safe_error_message, item.completed_at = InstructionStatus.RETRYING, "", "", None
            item.save()
            jobs.enqueue_debit(item, tag=f"round{item.retry_round}")
            retried.append(str(item.id))
        if retried:
            batch.status, batch.completed_at = BatchStatus.PROCESSING, None
            batch.save()
            debit_batches.record(batch, "retried", membership, retried=len(retried), needs_approval=len(needs_approval))
            audit.record(batch.school, "debit_retried", actor=membership, obj=batch, retried=len(retried), needs_approval=len(needs_approval))
    return {"retried": retried, "needsFreshApproval": needs_approval}


def progress(batch) -> dict:
    """Counts for a screen: what has been debited, what failed, what is still being carried out or asked about."""
    items = list(MandateDebitInstruction.objects.filter(batch=batch, selected=True))
    by_status: dict = {}
    for item in items:
        by_status[item.status] = by_status.get(item.status, 0) + 1
    waiting = MandateProviderJob.objects.filter(instruction__batch=batch, status__in=(JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRY)).count()
    return {
        "status": batch.status, "total": len(items), "byStatus": by_status, "success": by_status.get(InstructionStatus.SUCCESS, 0),
        "failed": by_status.get(InstructionStatus.FAILED, 0), "unknown": by_status.get(InstructionStatus.UNKNOWN, 0), "queued": waiting,
        "done": batch.status in (BatchStatus.COMPLETED, BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED),
    }

