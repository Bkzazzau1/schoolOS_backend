"""Term-based billing periods, from the school's own academic calendar.

SkulGen's primary subscription period is the academic term. These helpers turn a school's
real AcademicTerm/AcademicSession records into billing-period boundaries - never an invented
or guessed date.

TERM-interval billing is per school, by that school's own current academic term
(apps.billing.cycle._run_term_billing_cycle / _issue_term_invoice_for_school_locked) - never a
single subscription-wide period, so a multi-school organization's schools can each sit on
their own independent calendar. MONTHLY and ANNUAL periods never depended on a school's
calendar and are unaffected; they still use one subscription-wide period.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.schools.models import School


def term_bounds(term: AcademicTerm):
    """A term's billing period: its real calendar start, through midnight the day after it
    ends (an exclusive upper bound, consistent with how every other period end is used)."""

    start = timezone.make_aware(datetime.combine(term.starts_on, time.min))
    end = timezone.make_aware(datetime.combine(term.ends_on + timedelta(days=1), time.min))
    return start, end


def active_term_for_school(school: School, *, as_of_date):
    return (
        AcademicTerm.objects.filter(
            session__school=school,
            starts_on__lte=as_of_date,
            ends_on__gte=as_of_date,
        )
        .order_by("-starts_on")
        .first()
    )


def active_session_for_school(school: School, *, as_of_date):
    """The academic session covering this date - how annual billing knows how many real terms
    a session has, instead of assuming a fixed count."""

    return (
        AcademicSession.objects.filter(
            school=school,
            starts_on__lte=as_of_date,
            ends_on__gte=as_of_date,
        )
        .order_by("-starts_on")
        .first()
    )


