"""A family's statement: what was charged, taken off, paid and is still owed, student by student.

A statement is READ from the ledger every time (`build`), so it can never disagree with it. Issuing one
(`issue`) only gives it a number, a date and an issuer, plus a small snapshot of the totals that day kept
for reference - the snapshot is never the source of truth for money, and reading a statement never uses it.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import audit, ledger, periods, reports
from .errors import Refused
from .models import AccountStatus, Family, FamilyCollectionAccount, FamilyStatement, ReceivableStatus, StudentReceivable
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


def collection_account_facts(family: Family) -> list[dict]:
    """What a family may be told about the account it pays into. Never provider credentials or internals."""
    accounts = FamilyCollectionAccount.objects.filter(family=family).exclude(status=AccountStatus.CLOSED)
    return [
        {"provider": a.provider, "bankName": a.bank_name, "accountNumber": a.account_number, "accountName": a.account_name, "status": a.status}
        for a in accounts
    ]


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
