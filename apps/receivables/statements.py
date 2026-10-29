"""A family's statement: what was charged, taken off, paid and is still owed, student by student.

A statement is READ from the ledger every time (`build`), so it can never disagree with it. Issuing one
(`issue`) only gives it a number, a date and an issuer, plus a small snapshot of the totals that day kept
for reference - the snapshot is never the source of truth for money, and reading a statement never uses it.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bankconnect.constants import ReconStatus
from apps.bankconnect.models import BankTransaction, ReconciliationDecision, TransactionAllocation
from apps.students.models import Student

from . import account_shapes, audit, credit, families, ledger, periods, reports
from .errors import Refused
from .models import (
    AccountStatus, CreditKind, Family, FamilyCollectionAccount, FamilyCreditEntry, FamilyStatement, ReceivableStatus, StatementStatus,
    StudentReceivable,
)
from .permissions import require_operator

#: A decision with one of these actions is the one that ends a payment's story: nothing after it changes
#: who it was ever good for.
_TERMINAL_ACTIONS = ("reversed", "refunded", "duplicate")

_NUMBERING_TRIES = 8

#: A payment in one of these states no longer counts as money collected: it went back, or is held as a
#: possible duplicate until a person confirms it (mirrors apps.bankconnect.summary.NOT_COLLECTED).
_NOT_COLLECTED = (ReconStatus.REVERSED, ReconStatus.REFUNDED, ReconStatus.DUPLICATE)


def _line(receivable: StudentReceivable, pos: ledger.Position) -> dict:
    return {
        "id": str(receivable.id),
        "item": receivable.item_name,
        "sessionName": receivable.session.name,
        "termName": receivable.term.name if receivable.term_id else "",
        "category": receivable.item_category,
        "installment": receivable.installment_number,
        "installments": receivable.installment_count,
        "dueDate": receivable.due_date.isoformat(),
        "grossMinor": pos.gross,
        "adjustmentsMinor": pos.adjustments,
        "netMinor": pos.net,
        "paidMinor": pos.paid,
        "outstandingMinor": pos.outstanding,
        "status": receivable.status,
    }


def _totals(lines: list[dict]) -> dict:
    return {
        key: sum(line[key] for line in lines)
        for key in ("grossMinor", "adjustmentsMinor", "netMinor", "paidMinor", "outstandingMinor")
    }


#: Accounts a family may pay into. One being set up, or paused by the school, is not offered: paying it could go astray.
_PAYABLE = (AccountStatus.ACTIVE, AccountStatus.DORMANT, AccountStatus.SETTLED, AccountStatus.GRACE)


def collection_account_facts(family: Family) -> list[dict]:
    """What a family may be told about where to pay. Never provider credentials or internals.

    The account belongs to the FAMILY, so this is the same whichever child a payment is for. What it looks like depends on the
    provider: `numberLabel` is what that provider calls the number, `details` are the extra facts it needs the payer to know,
    and `note` is how to pay it. A family can hold accounts with several providers. An account that is not payable right now
    is listed with its status but without its number."""
    rows = []
    for a in FamilyCollectionAccount.objects.filter(family=family).exclude(status=AccountStatus.CLOSED):
        payable = a.status in _PAYABLE
        rows.append({
            "id": str(a.id), "provider": a.provider, "bankName": a.bank_name, "accountName": a.account_name, "status": a.status,
            "accountNumber": a.account_number if payable else "", "numberLabel": account_shapes.label_for(a.provider),
            "details": a.public_details if payable else [], "note": account_shapes.note_for(a.provider), "canPay": payable,
            "isTest": bool(a.provider_meta.get("test")),
        })
    order = {AccountStatus.ACTIVE: 0, AccountStatus.DORMANT: 1, AccountStatus.SETTLED: 1, AccountStatus.GRACE: 1}
    return sorted(rows, key=lambda r: (order.get(r["status"], 2), r["bankName"], r["id"]))


def _payment_entry(tx: BankTransaction, family: Family, allocations: list) -> dict:
    reversed_like = tx.reconciliation_status in _NOT_COLLECTED
    allocated = sum(a.amount_minor for a in allocations)
    credit_minor = max(credit.total_credit_from(tx, family), 0) if not reversed_like else 0
    # Whether THIS family still has any stake in this payment right now - not just whether the payment
    # as a whole is still good. A payment moved to someone else's student entirely leaves a family with
    # neither an allocation nor credit from it, even though the payment itself was never reversed.
    counted = not reversed_like and (allocated > 0 or credit_minor > 0)
    return {
        "transactionId": str(tx.id),
        "date": tx.transaction_date.isoformat(),
        "provider": tx.provider,
        "reference": tx.transaction_reference or tx.external_transaction_id,
        "amountMinor": tx.amount_minor,
        "status": tx.reconciliation_status,
        "countsTowardBalance": counted,
        "allocations": [
            {
                "studentId": str(a.student_id), "studentName": a.student.full_name, "purpose": a.purpose,
                "amountMinor": a.amount_minor, "receivableId": str(a.receivable_id) if a.receivable_id else None,
                "item": a.receivable.item_name if a.receivable_id else "",
            }
            for a in allocations
        ] if counted else [],
        "allocatedMinor": allocated if counted else 0,
        "creditMinor": credit_minor if counted else 0,
        "unappliedMinor": max(tx.amount_minor - allocated - credit_minor, 0) if counted else 0,
    }


def _families_holding_at_terminal_decision(tx: BankTransaction) -> set:
    """Whose money this was at the moment it was most recently marked reversed, refunded or a duplicate -
    from that decision's own before-snapshot (`review.snapshot`), which records exactly who held it right
    then. A family the payment had already been moved away from by an earlier, unrelated decision (a
    correction of where it was put) never appears here, however long ago that was."""
    terminal = (
        ReconciliationDecision.objects.filter(transaction=tx, action__in=_TERMINAL_ACTIONS).order_by("-at", "-id").first()
    )
    if terminal is None:
        return set()
    student_ids = {row.get("studentId") for row in terminal.before.get("allocations", []) if row.get("studentId")}
    if not student_ids:
        return set()
    found = set()
    for student in Student.objects.filter(id__in=student_ids):
        owner = families.family_of(student)
        if owner is not None:
            found.add(owner.id)
    return found


def _credit_clawed_back_by_terminal_decision(tx: BankTransaction, family: Family) -> bool:
    """Whether this family's credit from this payment was taken back specifically by the decision that
    most recently marked it reversed, refunded or a duplicate - not by an earlier, unrelated correction
    of where the payment was put. A `FamilyCreditEntry` carries no decision reference of its own, so this
    is read from timing: a release always writes its credit entries before the `ReconciliationDecision`
    row for that same decision is created, so only an `OVERPAYMENT_REVERSED` entry created after whichever
    decision came right before the terminal one belongs to it."""
    decisions = list(ReconciliationDecision.objects.filter(transaction=tx).order_by("at", "id"))
    terminal_index = None
    for i, d in enumerate(decisions):
        if d.action in _TERMINAL_ACTIONS:
            terminal_index = i  # kept updated through the loop, so this ends up the LAST match
    if terminal_index is None:
        return False
    cutoff = decisions[terminal_index - 1].at if terminal_index > 0 else None
    entries = FamilyCreditEntry.objects.filter(transaction=tx, family=family, kind=CreditKind.OVERPAYMENT_REVERSED)
    if cutoff is not None:
        entries = entries.filter(created_at__gt=cutoff)
    return entries.exists()


def payments(family: Family) -> list[dict]:
    """Every payment that is, or ever was, part of this family's ledger, newest first, with its current
    status and (while it still counts) where it went. History is never rewritten: a payment later reversed,
    refunded or found to be a duplicate is still listed for whoever was actually holding it when that
    happened, with no allocations and `countsTowardBalance: false` - exactly as the ledger it is read
    from, which took its allocations back when that was decided. A payment moved to a different family's
    student entirely (a person correcting where it was put) drops out of this family's history
    altogether: it was never really theirs, and nothing here should say it still is - not even if it was
    later reversed, refunded or found to be a duplicate in the hands of whoever it was moved to."""
    tx_ids = set(TransactionAllocation.objects.filter(family=family).values_list("transaction_id", flat=True))
    tx_ids |= set(FamilyCreditEntry.objects.filter(family=family, transaction__isnull=False).values_list("transaction_id", flat=True))
    if not tx_ids:
        return []
    transactions = BankTransaction.objects.filter(id__in=tx_ids).order_by("-transaction_date", "-created_at")
    current = (
        TransactionAllocation.objects.filter(family=family, transaction_id__in=tx_ids, superseded=False, receivable__isnull=False)
        .select_related("student", "receivable")
    )
    by_tx: dict = {}
    for a in current:
        by_tx.setdefault(a.transaction_id, []).append(a)
    rows = []
    for tx in transactions:
        entry = _payment_entry(tx, family, by_tx.get(tx.id, []))
        if entry["countsTowardBalance"]:
            rows.append(entry)
            continue
        if tx.reconciliation_status not in _NOT_COLLECTED:
            continue  # moved to a different family entirely, and never reversed: not this family's history
        if family.id in _families_holding_at_terminal_decision(tx) or _credit_clawed_back_by_terminal_decision(tx, family):
            rows.append(entry)
    return rows


def build(family: Family, *, session=None, term=None, today: date | None = None) -> dict:
    """The statement, worked out now. With a `session` (and optionally a `term`) only those charges are listed;
    the family's overall position (everything it owes, and its credit) is always given as well."""
    today = today or periods.school_today()
    receivables = StudentReceivable.objects.filter(family=family).select_related("student", "session", "term").exclude(status=ReceivableStatus.VOID)
    if session is not None:
        receivables = receivables.filter(session=session)
    if term is not None:
        receivables = receivables.filter(term=term)
    receivables = list(receivables.order_by("student__surname", "student__first_name", "due_date", "created_at", "id"))
    figures = ledger.positions(receivables)
    by_period = reports.rollup(receivables, figures, today=today)

    students: dict = {}
    for r in receivables:
        entry = students.setdefault(r.student_id, {"studentId": str(r.student_id), "name": r.student.full_name, "studentCode": r.student.student_code, "lines": []})
        entry["lines"].append(_line(r, figures[r.id]))
    ordered = list(students.values())
    for entry in ordered:
        entry["totals"] = _totals(entry["lines"])
    everything = [line for entry in ordered for line in entry["lines"]]
    overall = ledger.family_position(family, today=today)
    return {
        "family": {"id": str(family.id), "code": family.code, "name": family.display_name, "status": family.status},
        "period": {"sessionId": str(session.id) if session else None, "termId": str(term.id) if term else None},
        "students": ordered,
        "byPeriod": by_period,
        "totals": _totals(everything),
        "position": {
            "grossMinor": overall.gross, "adjustmentsMinor": overall.adjustments, "netMinor": overall.net, "paidMinor": overall.paid,
            "outstandingMinor": overall.outstanding, "overdueMinor": overall.overdue, "arrearsMinor": overall.arrears,
            "currentMinor": overall.current, "creditMinor": overall.credit, "collectibleMinor": overall.collectible,
        },
        "collectionAccounts": collection_account_facts(family),
        "asOf": today.isoformat(),
        "currency": "NGN",
    }


@transaction.atomic
def void(statement: FamilyStatement, *, actor, reason) -> FamilyStatement:
    """Take back a statement issued in error. It is never deleted or edited: it stays on record as void, with who and why,
    and its number is not reused. What the family owes is unaffected (a statement only ever reports the ledger)."""
    require_operator(actor, statement.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why the statement is being voided.", "reason_required")
    if len(reason) > 300:
        raise Refused("A reason can be at most 300 characters.", "reason_too_long")
    statement = FamilyStatement.objects.select_for_update().get(pk=statement.pk)
    if statement.status == StatementStatus.VOID:
        raise Refused("This statement is already void.", "already_void")
    statement.status, statement.voided_at, statement.voided_by, statement.void_reason = StatementStatus.VOID, timezone.now(), actor, reason
    statement.save(update_fields=["status", "voided_at", "voided_by", "void_reason"])
    audit.record(statement.school, "statement_voided", actor=actor, obj=statement, number=statement.number, family=str(statement.family_id), reason=reason)
    return statement


def _next_number(school) -> str:
    year = timezone.localdate().year
    count = FamilyStatement.objects.filter(school=school).count()
    return f"STM-{year}-{count + 1:06d}"


@transaction.atomic
def issue(family: Family, *, session, term=None, actor) -> FamilyStatement:
    """Give the family a numbered statement for a session (or term). Numbers run on within a school."""
    require_operator(actor, family.school)
    if session.school_id != family.school_id or (term is not None and term.session_id != session.id):
        raise Refused("That session or term is not at this school.", "session_not_found")
    snapshot = build(family, session=session, term=term)
    reference = {"totals": snapshot["totals"], "position": snapshot["position"]}
    for attempt in range(_NUMBERING_TRIES):
        number = _next_number(family.school)
        try:
            with transaction.atomic():
                statement = FamilyStatement.objects.create(
                    school=family.school, family=family, session=session, term=term, number=number, issued_by=actor, snapshot=reference,
                )
            break
        except IntegrityError:  # another statement took that number a moment ago
            if attempt == _NUMBERING_TRIES - 1:
                raise Refused("A statement number could not be made. Try again.", "number_unavailable")
    audit.record(family.school, "statement_issued", actor=actor, obj=statement, number=number, family=str(family.id))
    return statement
