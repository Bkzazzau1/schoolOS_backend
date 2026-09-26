"""What is owed, by session and term.

Every figure is derived from the ledger (`ledger.positions`); nothing here is stored. Charges are grouped by
the canonical session and term they were raised for, in calendar order (a session's own whole-session charges
follow its terms). "Past" is the calendar's word for a period that has ended or been closed, so what is still
owed for a past period is ARREARS.
"""

from datetime import date

from django.db.models import Sum

from . import ledger, periods
from .models import CREDIT_IN, CREDIT_OUT, FamilyCreditEntry, ReceivableStatus, StudentReceivable

NO_TERM_LAST = 99


def _sort_key(r):
    return (r.session.starts_on, r.term.sequence if r.term_id else NO_TERM_LAST, str(r.session_id), str(r.term_id or ""))


def rollup(receivables, figures: dict, *, today: date) -> list[dict]:
    """One line per session/term: billed, taken off, paid, still owed, overdue, and how much of it has been collected."""
    groups: dict = {}
    for r in sorted(receivables, key=_sort_key):
        p = figures[r.id]
        key = (r.session_id, r.term_id)
        g = groups.get(key)
        if g is None:
            start, end = periods.window(r.session, r.term)
            past, closed = periods.is_past(r.session, r.term, today), periods.is_closed(r.session, r.term)
            g = groups[key] = {
                "sessionId": str(r.session_id), "sessionName": r.session.name, "termId": str(r.term_id) if r.term_id else None,
                "termName": r.term.name if r.term_id else "", "label": periods.label(r.session, r.term),
                "startsOn": start.isoformat(), "endsOn": end.isoformat(), "isClosed": closed, "isPast": past,
                "isCurrent": (not past) and start <= today <= end,
                "charges": 0, "grossMinor": 0, "adjustmentsMinor": 0, "netMinor": 0, "paidMinor": 0, "outstandingMinor": 0, "overdueMinor": 0,
                "_students": set(), "_families": set(), "_owing": set(),
            }
        g["charges"] += 1
        g["grossMinor"] += p.gross
        g["adjustmentsMinor"] += p.adjustments
        g["netMinor"] += p.net
        g["paidMinor"] += p.paid
        g["outstandingMinor"] += p.outstanding
        if r.due_date < today:
            g["overdueMinor"] += p.outstanding
        g["_students"].add(r.student_id)
        g["_families"].add(r.family_id)
        if p.outstanding > 0:
            g["_owing"].add(r.family_id)
    lines = []
    for g in groups.values():
        g["students"], g["families"], g["familiesOwing"] = len(g.pop("_students")), len(g.pop("_families")), len(g.pop("_owing"))
        # Basis points of what was payable that has been paid (10000 = all of it); None when nothing was payable.
        g["collectionRateBp"] = g["paidMinor"] * 10_000 // g["netMinor"] if g["netMinor"] > 0 else None
        lines.append(g)
    return lines


def _live(school, session=None):
    rows = StudentReceivable.objects.filter(school=school).exclude(status=ReceivableStatus.VOID).select_related("session", "term")
    return rows.filter(session=session) if session is not None else rows


def by_term(school, *, session=None, today: date | None = None) -> dict:
    """The school's charges by session and term, with totals."""
    today = today or periods.school_today()
    receivables = list(_live(school, session))
    lines = rollup(receivables, ledger.positions(receivables), today=today)
    totals = {k: sum(line[k] for line in lines) for k in ("charges", "grossMinor", "adjustmentsMinor", "netMinor", "paidMinor", "outstandingMinor", "overdueMinor")}
    totals["arrearsMinor"] = sum(line["outstandingMinor"] for line in lines if line["isPast"])
    totals["collectionRateBp"] = totals["paidMinor"] * 10_000 // totals["netMinor"] if totals["netMinor"] > 0 else None
    return {"periods": lines, "totals": totals, "currency": "NGN", "asOf": today.isoformat()}


def school_credit(school) -> int:
    """Credit families hold, added up across the school."""
    balance = 0
    for row in FamilyCreditEntry.objects.filter(school=school).values("kind").annotate(total=Sum("amount_minor")):
        if row["kind"] in CREDIT_IN:
            balance += row["total"]
        elif row["kind"] in CREDIT_OUT:
            balance -= row["total"]
    return balance


def school_position(school, *, today: date | None = None) -> dict:
    """What the school is owed right now, by period. `available` is false until the school has raised any charge:
    a school with no fee ledger has no figures to show, and shows none."""
    today = today or periods.school_today()
    if not StudentReceivable.objects.filter(school=school).exclude(status=ReceivableStatus.VOID).exists():
        return {"available": False, "currency": "NGN", "outstandingMinor": 0, "overdueMinor": 0, "arrearsMinor": 0, "currentMinor": 0,
                "creditMinor": school_credit(school), "familiesOwing": 0, "periods": []}
    receivables = list(_live(school))
    figures = ledger.positions(receivables)
    lines = rollup(receivables, figures, today=today)
    outstanding = sum(line["outstandingMinor"] for line in lines)
    arrears = sum(line["outstandingMinor"] for line in lines if line["isPast"])
    owing = {r.family_id for r in receivables if figures[r.id].outstanding > 0}
    return {
        "available": True, "currency": "NGN", "outstandingMinor": outstanding, "overdueMinor": sum(line["overdueMinor"] for line in lines),
        "arrearsMinor": arrears, "currentMinor": outstanding - arrears, "creditMinor": school_credit(school), "familiesOwing": len(owing),
        "periods": lines,
    }
