"""Putting a bank payment towards the charges a family owes.

The rules, each enforced here and again by the database where a database can:

* A payment is allocated at most once: what is left to allocate is its amount less what is already
  allocated to charges and already held as credit, so asking twice does nothing the second time.
* An allocation never exceeds what the payment has left, and never exceeds what the charge still needs.
* Money beyond what the family owes is NOT allocated and NOT lost: it becomes family credit.
* Everything is one transaction that first takes the family's row, so two payments (or a payment and an
  adjustment) for the same family are handled one after the other, never at once.
* History is never destroyed. An allocation that is replaced is marked superseded and a new one is
  written; a payment that is reversed has its allocations superseded and its credit taken back.
"""

from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import Sum

from apps.bankconnect.constants import Direction, Purpose
from apps.bankconnect.models import BankTransaction, ReconciliationDecision, TransactionAllocation

from . import audit, credit, ledger
from .errors import Refused
from .models import CreditKind, Family, FamilyCreditEntry, ReceivableStatus, StudentReceivable

#: What a fee category is called in the bank-payment screens, which know only these purposes.
PURPOSE_FOR_CATEGORY = {
    "tuition": Purpose.TUITION, "ict": Purpose.OTHER, "levy": Purpose.CAPITAL, "books": Purpose.BOOKS,
    "transport": Purpose.TRANSPORT, "meals": Purpose.OTHER, "examination": Purpose.OTHER,
    "uniform": Purpose.UNIFORMS, "other": Purpose.OTHER,
}


@dataclass
class Allocated:
    """What one allocation did."""

    allocated_minor: int = 0
    credit_minor: int = 0
    allocations: list = field(default_factory=list)

    @property
    def total_minor(self) -> int:
        return self.allocated_minor + self.credit_minor


def purpose_for(receivable: StudentReceivable) -> str:
    return PURPOSE_FOR_CATEGORY.get(receivable.item_category, Purpose.OTHER)


def unallocated(tx: BankTransaction) -> int:
    """What is left of the payment that is neither allocated to a charge nor held as credit."""
    allocated = TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False).aggregate(t=Sum("amount_minor"))["t"] or 0
    return tx.amount_minor - allocated - credit.total_credit_from(tx)


def _check(tx: BankTransaction, family: Family) -> None:
    if tx.school_id != family.school_id:
        raise Refused("A payment can only be allocated to a family at the same school.", "wrong_school")
    if tx.direction != Direction.CREDIT:
        raise Refused("Only money received can be allocated.", "not_a_credit")


def _put_towards(tx, family, receivable, amount, source, decision) -> TransactionAllocation:
    return TransactionAllocation.objects.create(
        school=tx.school, transaction=tx, student=receivable.student, purpose=purpose_for(receivable), amount_minor=amount,
        source=source, decision=decision, family=family, receivable=receivable,
    )


def _hold_as_credit(tx, family, amount, actor) -> None:
    number = FamilyCreditEntry.objects.filter(family=family, transaction=tx, kind=CreditKind.OVERPAYMENT).count() + 1
    credit.add(
        family, CreditKind.OVERPAYMENT, amount, actor=actor, reason="Payment was more than the family owed",
        transaction_ref=tx, ref=f"overpay:{tx.id}:{number}",
    )


@transaction.atomic
def allocate(
    tx: BankTransaction, family: Family, *, amount_minor: int | None = None, prefer_student=None, source: str = "auto",
    decision=None, actor=None, settle: bool = True, today: date | None = None,
) -> Allocated:
    """Put (some of) a payment towards a family's charges, in payment order, and hold any excess as credit."""
    family = credit.lock_family(family)
    tx = BankTransaction.objects.select_for_update().get(pk=tx.pk)
    _check(tx, family)
    room = unallocated(tx)
    if amount_minor is not None:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
            raise Refused("An amount must be a whole number of kobo greater than zero.", "invalid_amount")
        room = min(room, amount_minor)
    result = Allocated()
    if room <= 0:
        return result
    before = ledger.family_position(family, today=today)
    remaining = room
    for receivable, outstanding in credit.outstanding_in_order(family, today=today, prefer_student=prefer_student):
        if remaining <= 0:
            break
        take = min(remaining, outstanding)
        result.allocations.append(_put_towards(tx, family, receivable, take, source, decision))
        remaining -= take
    result.allocated_minor = room - remaining
    if remaining > 0:
        _hold_as_credit(tx, family, remaining, actor)
        result.credit_minor = remaining
    audit.record(
        tx.school, "payment_allocated", actor=actor, obj=family, transaction=str(tx.id), allocated=result.allocated_minor,
        credit=result.credit_minor, source=source,
    )
    if settle:
        from . import lifecycle

        lifecycle.settle_family(family, actor, payment=lifecycle.PaymentEvent(tx, result, before), today=today)
    return result


def _families_touching(tx: BankTransaction) -> list:
    ids = set(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False).values_list("family_id", flat=True))
    ids |= set(FamilyCreditEntry.objects.filter(transaction=tx).values_list("family_id", flat=True))
    return sorted(ids, key=str)  # always locked in the same order, so two releases can never wait on each other


def _release(tx: BankTransaction, *, actor, reason: str) -> list:
    """Take a payment back out of the ledger: its allocations are superseded and its credit is removed. If
    some of that credit has since paid other charges, those uses are reversed so those charges are owed
    again. Does not settle the family - the caller does, once, after everything it means to change."""
    families = list(Family.objects.select_for_update().filter(id__in=_families_touching(tx)).order_by("id"))
    for family in families:
        TransactionAllocation.objects.filter(transaction=tx, family=family, receivable__isnull=False, superseded=False).update(superseded=True)
        owed_back = credit.total_credit_from(tx, family)
        if owed_back > 0:
            credit.ensure_available(family, owed_back, actor=actor, reason=reason)
            credit.add(family, CreditKind.OVERPAYMENT_REVERSED, owed_back, actor=actor, reason=reason, transaction_ref=tx)
    return families


@transaction.atomic
def release_transaction(tx: BankTransaction, *, actor=None, reason: str = "") -> list:
    """The payment is no longer counted as fees: it was reversed, refunded, found to be a duplicate or
    for something else. Its allocations and credit come out of the ledger, and what it had paid is owed
    again. Returns the families affected."""
    tx = BankTransaction.objects.select_for_update().get(pk=tx.pk)
    families = _release(tx, actor=actor, reason=reason)
    from . import lifecycle

    for family in families:
        audit.record(tx.school, "payment_released", actor=actor, obj=family, transaction=str(tx.id), reason=reason)
        lifecycle.settle_family(family, actor)
    return families


@transaction.atomic
def correct_allocations(tx: BankTransaction, plan, *, actor, reason: str, decision=None) -> Allocated:
    """A person puts a payment exactly where it belongs. `plan` is `[(receivable, amount_minor), ...]`, all
    for one family. What was allocated before is superseded (and stays on record), the plan is applied,
    and whatever the plan leaves of the payment is held as family credit."""
    from .permissions import can_operate_receivables

    if actor is None or actor.school_id != tx.school_id or not can_operate_receivables(actor):
        raise Refused("Only the owner or the Finance Office can correct where a payment went.", "not_authorised")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the payment is being moved.", "reason_required")
    plan = list(plan)
    if not plan:
        raise Refused("Say where the payment should go.", "empty_plan")
    family_ids = {receivable.family_id for receivable, _ in plan}
    if len(family_ids) != 1:
        raise Refused("A payment can only be put towards one family's charges.", "one_family")
    family = credit.lock_family(Family.objects.get(pk=family_ids.pop()))
    tx = BankTransaction.objects.select_for_update().get(pk=tx.pk)
    _check(tx, family)

    total = 0
    for receivable, amount in plan:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise Refused("Each amount must be a whole number of kobo greater than zero.", "invalid_amount")
        if receivable.school_id != tx.school_id or receivable.status == ReceivableStatus.VOID:
            raise Refused("That charge cannot be paid.", "receivable_not_payable")
        total += amount
    if total > tx.amount_minor:
        raise Refused("The parts add up to more than the payment.", "over_allocated")

    before = {"allocations": [(str(a.receivable_id), a.amount_minor) for a in TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False)]}
    released_from = _release(tx, actor=actor, reason=reason)
    result = Allocated()
    figures = ledger.positions([r for r, _ in plan])
    for receivable, amount in plan:
        room = figures[receivable.id].outstanding
        if amount > room:
            raise Refused(f"'{receivable.item_name}' needs only {room} more; {amount} was asked for.", "over_allocated")
        result.allocations.append(_put_towards(tx, family, receivable, amount, "manual", decision))
        result.allocated_minor += amount
    leftover = tx.amount_minor - result.allocated_minor
    if leftover > 0:
        _hold_as_credit(tx, family, leftover, actor)
        result.credit_minor = leftover
    audit.record(
        tx.school, "allocation_corrected", actor=actor, obj=family, transaction=str(tx.id), reason=reason,
        before=before, after=[(str(a.receivable_id), a.amount_minor) for a in result.allocations], credit=result.credit_minor,
    )
    from . import lifecycle

    # The target family, and any other family the payment had wrongly been put towards, are both settled.
    for affected in {family.id: family, **{f.id: f for f in released_from}}.values():
        lifecycle.settle_family(affected, actor)
    return result


def rebalance(receivable: StudentReceivable, *, actor=None, reason: str = "", void: bool = False) -> int:
    """After a charge needs less than has been put towards it (a scholarship, a waiver, a void), take the
    excess off the charge and hold it as credit. Credit that had been applied to it is taken back first
    (newest first), then bank allocations (newest first). Nothing is deleted. Returns the amount released.
    Runs inside the caller's transaction, which must hold the family row."""
    pos = ledger.position(receivable)
    excess = pos.paid - (0 if void else max(pos.net, 0))
    if excess <= 0:
        return 0
    released = 0
    for entry in FamilyCreditEntry.objects.filter(receivable=receivable, kind=CreditKind.APPLIED, reversal__isnull=True).order_by("-created_at", "-id"):
        if released >= excess:
            break
        credit.reverse_application(entry, actor=actor, reason=reason)
        released += entry.amount_minor
    # (Whatever a reversed application freed beyond what was needed is credit again, used elsewhere or kept.)
    still = ledger.position(receivable)
    excess = still.paid - (0 if void else max(still.net, 0))
    for allocation in TransactionAllocation.objects.filter(receivable=receivable, superseded=False).order_by("-created_at", "-id"):
        if excess <= 0:
            break
        cut = min(allocation.amount_minor, excess)
        allocation.superseded = True
        allocation.save(update_fields=["superseded"])
        if cut < allocation.amount_minor:
            TransactionAllocation.objects.create(
                school=allocation.school, transaction=allocation.transaction, student=allocation.student, purpose=allocation.purpose,
                amount_minor=allocation.amount_minor - cut, source=allocation.source, decision=allocation.decision,
                family=allocation.family, receivable=receivable,
            )
        credit.add(receivable.family, CreditKind.RELEASED, cut, actor=actor, reason=reason or "Charge needs less than was paid", transaction_ref=allocation.transaction, receivable=receivable)
        excess -= cut
        released += cut
    return released


def decision_for(tx, *, action: str, actor, note: str = "", before=None, after=None) -> ReconciliationDecision:
    """A bank-payment decision row for a change made here, so the payment's own history tells the story too."""
    return ReconciliationDecision.objects.create(
        school=tx.school, transaction=tx, action=action[:24], actor=actor, note=note[:500], before=before or {}, after=after or {},
    )
