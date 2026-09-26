"""Where bank payments meet the ledger.

`apps.bankconnect` decides WHO a payment is for; this module turns that decision into money against
charges. It has three entry points, all safe to call more than once and all doing nothing for a school
that has not set up families:

* `identify_family` - a payment made into a family's own collection account belongs to that family with
  certainty: the account is the identity, so no guessing from a narration is needed;
* `settle` - put the payment towards the family's charges, from either that certainty or from a student
  the engine or a person chose (in which case that student's charges are paid first);
* `release` - the payment is no longer counted as fees: take it back out of the ledger.

A person's decision always wins over the account: if someone assigns a payment to a particular student,
that is what is settled, even if the payment came in through a family account.
"""

from dataclasses import dataclass

from django.db import transaction

from apps.bankconnect.models import BankTransaction, TransactionAllocation

from . import allocation, collection_accounts, families
from .models import Family


@dataclass
class Outcome:
    family: Family
    result: allocation.Allocated
    #: True when the family was known from the account paid into, not chosen by a match on a student.
    from_account: bool


def identify_family(tx: BankTransaction) -> Family | None:
    """The family a payment belongs to with certainty, from the account it was paid into."""
    if tx.family_id:
        return tx.family
    account = collection_accounts.find_by_receiving_reference(tx.school, tx.provider, tx.receiving_account_ref)
    # An account left on a family that was merged into another still receives money: it is the merged family's.
    return families.surviving(account.family) if account is not None else None


def settle(tx: BankTransaction, *, decision=None, actor=None) -> list[Outcome]:
    """Put a payment towards charges. Uses the student(s) somebody chose if there are any, otherwise the
    family of the account it was paid into. Returns what was done (empty if there was nothing to settle)."""
    if tx.direction != "credit":
        return []
    with transaction.atomic():
        chosen = list(TransactionAllocation.objects.select_for_update().filter(transaction=tx, receivable__isnull=True, superseded=False).select_related("student"))
        outcomes: list[Outcome] = []
        if chosen:
            for row in chosen:
                family = families.family_of(row.student)
                if family is None:
                    continue  # this student has no family yet: the plain student-level record stands
                result = allocation.allocate(
                    tx, family, amount_minor=row.amount_minor, prefer_student=row.student_id, source=row.source,
                    decision=row.decision or decision, actor=actor,
                )
                # The student-level record has been turned into charge-level allocations; it stays, superseded.
                row.superseded = True
                row.save(update_fields=["superseded"])
                outcomes.append(Outcome(family, result, from_account=False))
            return outcomes
        family = identify_family(tx)
        if family is None:
            return []
        return [Outcome(family, allocation.allocate(tx, family, source="auto", decision=decision, actor=actor), from_account=True)]


def release(tx: BankTransaction, *, actor=None, reason: str = "") -> list:
    """The payment no longer counts as fees. Safe to call for any payment: with nothing allocated it does nothing."""
    return allocation.release_transaction(tx, actor=actor, reason=reason)
