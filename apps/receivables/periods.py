"""The academic calendar, as the fee system sees it.

A school's fees follow its canonical sessions and terms (`apps.academics`), never dates or names typed by
a client:

* a fee schedule belongs to one SESSION and, usually, one TERM of it (or, with no term, to the whole
  session);
* a CLOSED session or term is history: it can no longer be billed (its existing charges stay, and can
  still be adjusted, voided and paid);
* a student is charged for a term only if they were enrolled by then - a student who entered in Term 2 owes
  nothing for Term 1;
* a charge's due date must fall within the period it is for (with some lead, since fees are asked for before
  a term starts);
* "arrears" means what is still owed for a period that has ended.

The current session and term are the academics app's own answer (`active_session_for_school`).
"""

from datetime import date, timedelta

from django.utils import timezone

from apps.academics.models import AcademicLifecycleStatus
from apps.academics.services import active_session_for_school, active_term_for_session

def school_today() -> date:
    """The school's calendar day (Africa/Lagos). The one place the fee system reads the clock, so it can be fixed in tests."""
    return timezone.localdate()


#: Fees are asked for before a term starts. A due date may be this many days before the period begins.
DUE_LEAD_DAYS = 45


def current_period(school):
    """`(session, term)` for the school right now: its active session and that session's active term. Either may be None."""
    session = active_session_for_school(school)
    return session, (active_term_for_session(session) if session else None)


def label(session, term=None) -> str:
    return f"{session.name} · {term.name}" if term else session.name


def window(session, term=None) -> tuple[date, date]:
    """The dates a period covers: the term's, or the whole session's when there is no term."""
    period = term or session
    return period.starts_on, period.ends_on


def is_closed(session, term=None) -> bool:
    return session.status == AcademicLifecycleStatus.CLOSED or (term is not None and term.status == AcademicLifecycleStatus.CLOSED)


def is_past(session, term=None, today: date | None = None) -> bool:
    """Whether a period is over: closed by the school, or its end date has passed."""
    today = today or school_today()
    return is_closed(session, term) or window(session, term)[1] < today


def due_date_problem(due: date, session, term=None) -> str | None:
    """Why a due date does not belong to the period, or None if it does."""
    start, end = window(session, term)
    earliest = start - timedelta(days=DUE_LEAD_DAYS)
    name = label(session, term)
    if due > end:
        return f"The due date {due.isoformat()} is after the end of {name} ({end.isoformat()}). Use a schedule for the whole session for instalments that run past a term."
    if due < earliest:
        return f"The due date {due.isoformat()} is more than {DUE_LEAD_DAYS} days before {name} begins ({start.isoformat()})."
    return None


def joined_after(context, term) -> bool:
    """Whether a student's placement began after `term` - so they owe nothing for it. Uses the term they
    entered in; where none was recorded, the date their enrolment started."""
    if term is None:
        return False
    entry = context.entry_term
    if entry is not None:
        return entry.sequence > term.sequence
    return context.enrollment.started_at.date() > term.ends_on
