"""A family's statement: what was charged, taken off, paid and is still owed, student by student.

A statement is READ from the ledger every time (`build`), so it can never disagree with it. Issuing one
(`issue`) only gives it a number, a date and an issuer, plus a small snapshot of the totals that day kept
for reference - the snapshot is never the source of truth for money, and reading a statement never uses it.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import account_shapes, audit, ledger, periods, reports
from .errors import Refused
from .models import AccountStatus, Family, FamilyCollectionAccount, FamilyStatement, ReceivableStatus, StatementStatus, StudentReceivable
from .permissions import require_operator

_NUMBERING_TRIES = 8


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
