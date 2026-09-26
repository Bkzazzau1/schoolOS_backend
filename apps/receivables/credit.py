"""A family's credit: money it has put in that is not (or is no longer) needed by a charge.

Credit is a ledger of entries (see `FamilyCreditEntry`); its balance is their sum. It arises when a
payment is more than the family owed, or when a charge needs less than was put towards it. It is used
automatically against the family's next charges, and can be paid back. It is never revenue: nothing here
counts unallocated money as earned fees.

Every function that changes credit expects to run inside a transaction that already holds the family row
(`lock_family`), which is what serialises two things happening to one family at once. None of them
sends notifications or updates the collection account; `lifecycle.settle_family` does that once, after
all the changes of one operation are made.
"""

from datetime import date

from django.db import IntegrityError, transaction

from . import audit, ledger, periods, policy
from .errors import Refused
from .models import CREDIT_OUT, CreditKind, Family, FamilyCreditEntry


def lock_family(family: Family) -> Family:
    """Take the family's row for this transaction: nobody else can change its ledger until it ends."""
    return Family.objects.select_for_update().get(pk=family.pk)


def _clean_amount(amount) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise Refused("An amount must be a whole number of kobo greater than zero.", "invalid_amount")
    return amount


def add(
    family: Family, kind: str, amount: int, *, actor=None, reason: str = "", transaction_ref=None, receivable=None,
    reverses=None, ref: str = "",
) -> FamilyCreditEntry:
    """Append one entry. An entry that takes credit out is refused if the family does not have that much.
    With a `ref`, asking twice returns the first entry instead of making a second."""
    if kind not in CreditKind.values:
        raise Refused("That is not a kind of credit entry.", "invalid_kind")
    amount = _clean_amount(amount)
    if ref:
        existing = FamilyCreditEntry.objects.filter(school=family.school, ref=ref).first()
        if existing is not None:
            return existing
    if kind in CREDIT_OUT and ledger.credit_balance(family) < amount:
        raise Refused("The family does not have that much credit.", "insufficient_credit")
    try:
        with transaction.atomic():
            return FamilyCreditEntry.objects.create(
                school=family.school, family=family, kind=kind, amount_minor=amount, actor=actor, reason=reason[:300],
                transaction=transaction_ref, receivable=receivable, reverses=reverses, ref=ref,
            )
    except IntegrityError:
        if ref:
            existing = FamilyCreditEntry.objects.filter(school=family.school, ref=ref).first()
            if existing is not None:
                return existing
        raise


def outstanding_in_order(family: Family, *, today: date | None = None, prefer_student=None) -> list:
    """`[(receivable, outstanding)]` for the family's charges that still owe something, in payment order."""
    today = today or periods.school_today()
    receivables = list(ledger.live_receivables(family))
    figures = ledger.positions(receivables)
    owing = [r for r in receivables if figures[r.id].outstanding > 0]
    return [(r, figures[r.id].outstanding) for r in policy.get_policy()(owing, today=today, prefer_student=prefer_student)]


def apply_available(family: Family, *, actor=None, today: date | None = None) -> int:
    """Use whatever credit the family has against what it owes, in payment order. Returns how much was used."""
    remaining = ledger.credit_balance(family)
    used = 0
    if remaining <= 0:
        return 0
    for receivable, outstanding in outstanding_in_order(family, today=today):
        take = min(remaining, outstanding)
        add(family, CreditKind.APPLIED, take, actor=actor, receivable=receivable, reason="Credit applied automatically")
        remaining -= take
        used += take
        if remaining <= 0:
            break
    if used:
        audit.record(family.school, "credit_applied", actor=actor, obj=family, amount=used)
    return used


def reverse_application(entry: FamilyCreditEntry, *, actor=None, reason: str = "") -> FamilyCreditEntry:
    """Undo one use of credit: the credit is back and the charge it paid is owed again."""
    if entry.kind != CreditKind.APPLIED:
        raise Refused("Only an application of credit can be reversed.", "not_an_application")
    if hasattr(entry, "reversal"):
        raise Refused("That application was already reversed.", "already_reversed")
    return add(
        entry.family, CreditKind.APPLICATION_REVERSED, entry.amount_minor, actor=actor, reason=reason,
        receivable=entry.receivable, reverses=entry,
    )


def ensure_available(family: Family, amount: int, *, actor=None, reason: str = "") -> None:
    """Make sure the family holds at least `amount` of credit, by taking back its most recent uses of credit
    (newest first) if it must. This is what happens when money that paid for something is itself reversed:
    the charges it paid come due again."""
    while ledger.credit_balance(family) < amount:
        latest = (
            FamilyCreditEntry.objects.filter(family=family, kind=CreditKind.APPLIED, reversal__isnull=True)
            .order_by("-created_at", "-id").first()
        )
        if latest is None:
            raise Refused("The credit from this payment has been used and cannot be taken back.", "credit_in_use")
        reverse_application(latest, actor=actor, reason=reason)


@transaction.atomic
def refund(family: Family, amount: int, *, actor, reason: str) -> FamilyCreditEntry:
    """Record that credit was paid back to the family. This records the fact; the money itself is paid out
    by the school. Only someone who works the ledger may do it, and it needs a reason."""
    from .permissions import can_operate_receivables

    if actor is None or actor.school_id != family.school_id or not can_operate_receivables(actor):
        raise Refused("Only the owner or the Finance Office can record a refund.", "not_authorised")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the credit is being refunded.", "reason_required")
    family = lock_family(family)
    entry = add(family, CreditKind.REFUNDED, amount, actor=actor, reason=reason)
    audit.record(family.school, "credit_refunded", actor=actor, obj=family, amount=entry.amount_minor, reason=reason)
    from . import lifecycle

    lifecycle.settle_family(family, actor)
    return entry


def total_credit_from(transaction_obj, family: Family | None = None) -> int:
    """Credit that came from one bank payment (its overpayment and anything released from charges it had
    paid), less any of it taken back because the payment was reversed."""
    total = 0
    entries_ = FamilyCreditEntry.objects.filter(transaction=transaction_obj)
    if family is not None:
        entries_ = entries_.filter(family=family)
    for kind, amount in entries_.values_list("kind", "amount_minor"):
        if kind in (CreditKind.OVERPAYMENT, CreditKind.RELEASED):
            total += amount
        elif kind == CreditKind.OVERPAYMENT_REVERSED:
            total -= amount
    return total


def entries(family: Family):
    return FamilyCreditEntry.objects.filter(family=family).select_related("receivable", "transaction")
