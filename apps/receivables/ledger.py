"""What a charge, a student and a family owe - always worked out from the ledger, never stored.

For one charge:

    gross        the amount charged at publication (fixed)
    adjustments  discounts, scholarships and waivers, less any that were reversed
    net          gross - adjustments: what the family must actually pay for it
    paid         bank payments allocated to it (not superseded) + family credit applied to it
    outstanding  what is still to pay, never below zero

and its STATUS follows: void, settled (net covered), partly paid, or open. A charge is never over-paid:
money beyond `net` is not allocated, it becomes family credit. So a family's outstanding can never go
negative because of a payment nobody accounted for; the excess is a visible credit balance instead.

For a family, outstanding is the sum over its charges, and `credit` is the credit ledger's balance.

Nothing here writes money. The `status` on a receivable is only a cache, refreshed by `refresh_status`
inside the same transaction as every ledger change, and `verify_family` checks that the cache and every
other invariant hold.
"""

from dataclasses import dataclass
from datetime import date

from django.db.models import Sum

from apps.bankconnect.models import TransactionAllocation

from .models import (
    CREDIT_IN, CREDIT_OUT, CreditKind, Family, FamilyCreditEntry, ReceivableAdjustment, ReceivableStatus, StudentReceivable,
)


@dataclass(frozen=True)
class Position:
    """One charge, worked out."""

    gross: int
    adjustments: int
    paid: int

    @property
    def net(self) -> int:
        return self.gross - self.adjustments

    @property
    def outstanding(self) -> int:
        return max(self.net - self.paid, 0)


@dataclass(frozen=True)
class FamilyPosition:
    gross: int
    adjustments: int
    paid: int
    outstanding: int
    overdue: int
    credit: int
    charges: int

    @property
    def net(self) -> int:
        return self.gross - self.adjustments

    @property
    def collectible(self) -> int:
        """What could still be collected: what is owed, less any credit the family already holds."""
        return max(self.outstanding - self.credit, 0)


def _sum_by(rows, key):
    totals: dict = {}
    for row in rows:
        totals[row[key]] = totals.get(row[key], 0) + row["total"]
    return totals


def positions(receivables) -> dict:
    """`{receivable id: Position}` for these charges, in a fixed number of queries."""
    receivables = list(receivables)
    ids = [r.id for r in receivables]
    if not ids:
        return {}
    adjustments: dict = {}
    for receivable_id, amount, reverses_id in ReceivableAdjustment.objects.filter(receivable_id__in=ids).values_list("receivable_id", "amount_minor", "reverses_id"):
        adjustments[receivable_id] = adjustments.get(receivable_id, 0) + (-amount if reverses_id else amount)
    paid = _sum_by(
        TransactionAllocation.objects.filter(receivable_id__in=ids, superseded=False).values("receivable_id").annotate(total=Sum("amount_minor")),
        "receivable_id",
    )
    for receivable_id, kind, total in (
        FamilyCreditEntry.objects.filter(receivable_id__in=ids, kind__in=(CreditKind.APPLIED, CreditKind.APPLICATION_REVERSED))
        .values_list("receivable_id", "kind").annotate(total=Sum("amount_minor"))
    ):
        paid[receivable_id] = paid.get(receivable_id, 0) + (total if kind == CreditKind.APPLIED else -total)
    return {r.id: Position(r.gross_amount_minor, adjustments.get(r.id, 0), paid.get(r.id, 0)) for r in receivables}


def position(receivable: StudentReceivable) -> Position:
    return positions([receivable])[receivable.id]


def derive_status(receivable: StudentReceivable, pos: Position) -> str:
    if receivable.voided_at is not None:
        return ReceivableStatus.VOID
    if pos.net <= 0 or pos.paid >= pos.net:
        return ReceivableStatus.SETTLED
    return ReceivableStatus.PARTIALLY_PAID if pos.paid > 0 else ReceivableStatus.OPEN


def refresh_status(receivable: StudentReceivable, pos: Position | None = None) -> str:
    """Bring the cached status into line with the ledger. Returns the status."""
    status = derive_status(receivable, pos or position(receivable))
    if receivable.status != status:
        receivable.status = status
        receivable.save(update_fields=["status", "updated_at"])
    return status


def refresh_statuses(receivables) -> None:
    receivables = list(receivables)
    figures = positions(receivables)
    for receivable in receivables:
        refresh_status(receivable, figures[receivable.id])


def live_receivables(family: Family):
    return StudentReceivable.objects.filter(family=family).exclude(status=ReceivableStatus.VOID)


def credit_balance(family: Family) -> int:
    """The family's credit: what has come in less what has gone out, summed from the credit ledger."""
    balance = 0
    for row in FamilyCreditEntry.objects.filter(family=family).values("kind").annotate(total=Sum("amount_minor")):
        if row["kind"] in CREDIT_IN:
            balance += row["total"]
        elif row["kind"] in CREDIT_OUT:
            balance -= row["total"]
    return balance


def family_position(family: Family, *, today: date | None = None) -> FamilyPosition:
    today = today or date.today()
    receivables = list(live_receivables(family))
    figures = positions(receivables)
    gross = adjustments = paid = outstanding = overdue = 0
    for r in receivables:
        p = figures[r.id]
        gross += p.gross
        adjustments += p.adjustments
        paid += p.paid
        outstanding += p.outstanding
        if r.due_date < today:
            overdue += p.outstanding
    return FamilyPosition(gross, adjustments, paid, outstanding, overdue, credit_balance(family), len(receivables))


def verify_family(family: Family) -> list[str]:
    """Every invariant of the family's ledger, as a list of what is wrong (empty when all is well). Used by
    the tests and available to an operator; it reads only."""
    problems = []
    receivables = list(StudentReceivable.objects.filter(family=family))
    figures = positions(receivables)
    for r in receivables:
        p = figures[r.id]
        if r.status == ReceivableStatus.VOID:
            if p.paid:
                problems.append(f"{r.id}: a void charge still has {p.paid} paid against it")
            continue
        if p.net < 0:
            problems.append(f"{r.id}: adjustments exceed the charge")
        if p.paid > max(p.net, 0):
            problems.append(f"{r.id}: paid {p.paid} is more than the {p.net} payable")
        if r.status != derive_status(r, p):
            problems.append(f"{r.id}: status {r.status} does not match the ledger ({derive_status(r, p)})")
    if credit_balance(family) < 0:
        problems.append("the family's credit balance is negative")
    for allocation in TransactionAllocation.objects.filter(family=family, receivable__isnull=False).select_related("receivable", "transaction"):
        if allocation.receivable.family_id != family.id:
            problems.append(f"allocation {allocation.id} points at another family's charge")
        if allocation.transaction.school_id != family.school_id or allocation.receivable.school_id != family.school_id:
            problems.append(f"allocation {allocation.id} crosses schools")
    return problems
