"""Term-based billing periods, from the school's own academic calendar.

SkulGen's primary subscription period is the academic term. These helpers turn a school's
real AcademicTerm/AcademicSession records into billing-period boundaries - never an invented
or guessed date.

A subscription has exactly one current_period_start/current_period_end pair, so a
multi-school organization's TERM-interval billing period cannot yet follow each school's own
independent calendar (that needs a per-school period, not a per-subscription one - a schema
change, not built yet). Pricing is already correctly per-school for a multi-school
organization (see pricing.fixed_fees_for_subscription_period); only the TERM period itself -
used only to decide when the next cycle run is due - still requires exactly one active school,
so callers get a clear BillingCycleNotReady instead of a guess at whose calendar should govern.
MONTHLY and ANNUAL periods never depended on a school's calendar and are unaffected.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.schools.models import School

from .exceptions import BillingCycleNotReady


def single_active_school(organization) -> School:
    schools = list(School.objects.filter(organization=organization, is_active=True))
    if len(schools) != 1:
        raise BillingCycleNotReady(
            f"Term billing requires exactly one active school per organization; this "
            f"organization has {len(schools)}. Multi-school term billing needs per-school "
            "invoice lines, not implemented yet."
        )
    return schools[0]


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


def next_term_bounds(school: School, *, not_before_date):
    """The next configured term starting on or after this date, or (None, None) if the
    academic calendar does not extend that far yet - never an invented date."""

    next_term = (
        AcademicTerm.objects.filter(session__school=school, starts_on__gte=not_before_date)
        .order_by("starts_on")
        .first()
    )
    if next_term is None:
        return None, None
    return term_bounds(next_term)
