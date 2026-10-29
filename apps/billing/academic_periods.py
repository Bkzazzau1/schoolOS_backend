"""Term-based billing periods, from the school's own academic calendar.

SkulGen's primary subscription period is the academic term. These helpers turn a school's
real AcademicTerm records into billing-period boundaries - never an invented or guessed date.
Only single-school organizations are handled here; a multi-school organization needs
per-school invoice lines (Phase C, priority four), not built yet, so callers get a clear
BillingCycleNotReady instead of a combined or arbitrary period.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicTerm
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
