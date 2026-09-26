"""Where a confirmed debit meets the receivables ledger.

Money is only ever posted to a family's charges when the provider says a debit succeeded (`DebitOutcome.SUCCESS`, in the provider's own documented
words), and it is posted exactly once: `MandateTransaction.payment` is set in the same transaction that creates the payment, and the database refuses
a second one. A pending, failed or unknown debit changes nothing in the ledger.

There is no second settlement engine. The debit becomes one canonical receivables payment (`BankTransaction`, the ledger's own record of money
received, marked as a direct debit and known to belong to its family for certain) and is then put towards charges by the SAME allocation service every
payment uses - in payment order, limited to the charges the approved debit was for, with anything beyond what is owed held as family credit rather than
lost. A reversal or refund takes the payment back out with the SAME release mechanism, and is recorded: nothing is deleted.
"""

from django.db import transaction
from django.utils import timezone

from apps.bankconnect.constants import Direction, ReconStatus
from apps.bankconnect.models import BankTransaction
from apps.receivables import allocation, families, payments

from . import audit
from .constants import DebitOutcome
from .models import MandateTransaction

DIRECT_DEBIT = "direct_debit"


def settle(tx: MandateTransaction, *, actor=None) -> BankTransaction | None:
    """Put a confirmed debit towards the family's charges, once. Safe to call again: it does nothing the second time."""
    with transaction.atomic():
        tx = MandateTransaction.objects.select_for_update(of=("self",)).select_related("instruction", "mandate", "family", "provider_connection").get(pk=tx.pk)
        if tx.status != DebitOutcome.SUCCESS:
            return None
        if tx.payment_id:
            return tx.payment
        family = families.surviving(tx.family)
        payment = BankTransaction.objects.create(
            school=tx.school, connection=None, provider=tx.provider, bank_name=tx.mandate.bank_name, masked_account_number=tx.mandate.account_mask,
            external_transaction_id=f"mandate:{tx.id}", transaction_reference=tx.request_ref, transaction_type=DIRECT_DEBIT, direction=Direction.CREDIT,
            amount_minor=tx.amount_minor, currency=tx.currency, sender_name=(tx.mandate.payer.guardian.name or "")[:200], narration="Direct debit for school fees",
            transaction_date=tx.confirmed_at or timezone.now(), raw_provider_reference=tx.provider_transaction_reference, family=family,
            is_sandbox=tx.provider_connection.is_sandbox, reconciliation_status=ReconStatus.MATCHED, reconciliation_confidence=100,
            match_reasons=["Debited from the family's own mandate, so the family is known for certain."], engine_version="mandate",
        )
        tx.payment = payment
        tx.save(update_fields=["payment", "updated_at"])
        only = [a["receivableId"] for a in (tx.instruction.receivable_allocation or [])]
        result = allocation.allocate(payment, family, source="auto", actor=actor, only_receivables=only or None)
        audit.record(
            tx.school, "debit_settled", actor=actor, obj=tx, family=str(family.id), amount_minor=tx.amount_minor, allocated_minor=result.allocated_minor,
            credit_minor=result.credit_minor,
        )
    return payment


def reverse(tx: MandateTransaction, *, outcome: str, reason: str, actor=None) -> bool:
    """The provider reversed or refunded a debit that had been settled. The payment comes back out of the ledger (the family owes what it owed
    before) and the transaction says why. Nothing is deleted. Returns whether anything changed."""
    if outcome not in (DebitOutcome.REVERSED, DebitOutcome.REFUNDED):
        raise ValueError("A debit is reversed or refunded.")
    with transaction.atomic():
        tx = MandateTransaction.objects.select_for_update(of=("self",)).select_related("payment").get(pk=tx.pk)
        if tx.status in (DebitOutcome.REVERSED, DebitOutcome.REFUNDED) or tx.status != DebitOutcome.SUCCESS:
            return False
        if tx.payment_id:
            payments.release(tx.payment, actor=actor, reason=reason)
            BankTransaction.objects.filter(pk=tx.payment_id).update(reconciliation_status=ReconStatus.REVERSED)
        now = timezone.now()
        tx.status = outcome
        if outcome == DebitOutcome.REVERSED:
            tx.reversed_at = now
        else:
            tx.refunded_at = now
        tx.save(update_fields=["status", "reversed_at", "refunded_at", "updated_at"])
        audit.record(tx.school, "debit_reversed", actor=actor, obj=tx, outcome=outcome, reason=reason[:300], amount_minor=tx.amount_minor)
    return True
