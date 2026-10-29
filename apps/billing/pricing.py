"""Fixed-bracket subscription pricing: the commercial replacement for per-student billing.

A school pays a fixed fee for the bracket its authoritative billable enrollment falls into,
never an amount computed by multiplying its student count by a per-student price. Brackets
are versioned and effective-dated so changing prices, or adding brackets, never rewrites an
invoice already issued under the configuration in effect when it was billed. A school whose
enrollment is zero, or exceeds every configured bracket without an authorized
EnterpriseAgreement, has no safely invented price - PricingPolicyUndefined is raised so the
caller can surface that as an explicit decision rather than silently charging something.
"""

from dataclasses import dataclass, replace

from django.db import models as django_models
from django.db import transaction
from django.utils import timezone

from apps.schools.models import School

from .academic_periods import active_session_for_school
from .models import BillingInterval, EnterpriseAgreement, PricingAuditEvent, PricingBracket

#: The approved annual-payment discount: 10% off, kept as a numerator/denominator pair so the
#: rounding rule (truncate toward zero - the discount is never less generous than advertised)
#: is explicit at every call site that uses it.
ANNUAL_DISCOUNT_NUMERATOR = 9
ANNUAL_DISCOUNT_DENOMINATOR = 10


class PricingPolicyUndefined(Exception):
    """No fixed fee can be safely determined; a commercial decision is required."""


class OverlappingBracketError(Exception):
    """A new or edited bracket would overlap another bracket's enrollment range during a
    period when both are in effect."""


@dataclass(frozen=True)
class FeeResult:
    fixed_fee_minor: int  # the net amount actually owed
    currency: str
    source: str  # "bracket" or "enterprise"
    bracket_id: str | None
    enterprise_agreement_id: str | None
    min_students: int | None
    max_students: int | None
    gross_fee_minor: int = 0  # pre-discount amount; equals fixed_fee_minor when no discount applies
    discount_minor: int = 0
    term_count: int | None = None  # how many terms gross_fee_minor covers (annual payments only)


def _time_window_q(as_of, *, from_field: str, until_field: str):
    return django_models.Q(**{f"{from_field}__lte": as_of}) & (
        django_models.Q(**{f"{until_field}__isnull": True}) | django_models.Q(**{f"{until_field}__gt": as_of})
    )


def active_bracket_for(billable_student_count: int, *, as_of=None) -> PricingBracket | None:
    """The single standard bracket covering this enrollment count at this moment, or None if
    the count exceeds every configured bracket (meaning: check for an enterprise agreement)."""

    as_of = as_of or timezone.now()
    return (
        PricingBracket.objects.filter(
            min_students__lte=billable_student_count,
        )
        .filter(django_models.Q(max_students__isnull=True) | django_models.Q(max_students__gte=billable_student_count))
        .filter(_time_window_q(as_of, from_field="effective_from", until_field="effective_until"))
        .order_by("-effective_from", "-id")
        .first()
    )


def active_enterprise_agreement_for(school, *, as_of=None) -> EnterpriseAgreement | None:
    as_of = as_of or timezone.now()
    return (
        EnterpriseAgreement.objects.filter(school=school)
        .filter(_time_window_q(as_of, from_field="effective_from", until_field="effective_until"))
        .order_by("-effective_from", "-id")
        .first()
    )


def fixed_fee_for_school(school, *, billable_student_count: int, as_of=None) -> FeeResult:
    """The fixed fee this school owes for one billing period, under the approved
    fixed-bracket pricing model. Raises PricingPolicyUndefined rather than inventing a
    price for a zero-enrollment school or one above every configured bracket with no
    enterprise agreement on file."""

    as_of = as_of or timezone.now()
    if billable_student_count <= 0:
        raise PricingPolicyUndefined(
            "This school has zero billable students. SkulGen has no approved policy yet for "
            "pricing a zero-enrollment school; configure one before billing it."
        )

    bracket = active_bracket_for(billable_student_count, as_of=as_of)
    if bracket is not None:
        return FeeResult(
            fixed_fee_minor=bracket.fixed_fee_minor,
            currency=bracket.currency,
            source="bracket",
            bracket_id=str(bracket.id),
            enterprise_agreement_id=None,
            min_students=bracket.min_students,
            max_students=bracket.max_students,
            gross_fee_minor=bracket.fixed_fee_minor,
            term_count=1,
        )

    agreement = active_enterprise_agreement_for(school, as_of=as_of)
    if agreement is not None:
        return FeeResult(
            fixed_fee_minor=agreement.fixed_fee_minor,
            currency=agreement.currency,
            source="enterprise",
            bracket_id=None,
            enterprise_agreement_id=str(agreement.id),
            min_students=None,
            max_students=None,
            gross_fee_minor=agreement.fixed_fee_minor,
            term_count=1,
        )

    raise PricingPolicyUndefined(
        f"No fixed-bracket price covers {billable_student_count} billable students, and "
        "this school has no authorized enterprise agreement on file. Configure one before "
        "billing it; SkulGen never auto-prices an enterprise school."
    )


def annual_fee_for_school(school, *, billable_student_count: int, as_of=None) -> FeeResult:
    """The discounted annual fee for one full academic session, from the session's own real
    term count - never an assumed number of terms. 10% off the sum of what this school's
    per-term fixed-bracket fee would be for every term in its current session, truncated
    toward zero to the nearest kobo (so the discount is never less generous than advertised).
    """

    as_of = as_of or timezone.now()
    session = active_session_for_school(school, as_of_date=as_of.date())
    if session is None:
        raise PricingPolicyUndefined(
            "No active academic session is configured for this school. Annual pricing needs "
            "the session's real term count; SkulGen will not assume one."
        )
    term_count = session.terms.count()
    if term_count < 1:
        raise PricingPolicyUndefined(
            "This school's active academic session has no terms configured yet."
        )

    per_term = fixed_fee_for_school(school, billable_student_count=billable_student_count, as_of=as_of)
    gross = per_term.fixed_fee_minor * term_count
    net = (gross * ANNUAL_DISCOUNT_NUMERATOR) // ANNUAL_DISCOUNT_DENOMINATOR
    return replace(per_term, fixed_fee_minor=net, gross_fee_minor=gross, discount_minor=gross - net, term_count=term_count)


@dataclass(frozen=True)
class SchoolFee:
    school_id: str
    billable_student_count: int
    fee: FeeResult


def fixed_fees_for_subscription_period(subscription, snapshot) -> list[SchoolFee]:
    """Each active school's own fee for one organization's billing period - always determined
    per school, never by combining every school's enrollment into one bracket (Phase C,
    section 7). A single-school organization gets a one-entry list.

    The automatic cycle path captures each school's own authoritative count in the usage
    snapshot's metadata (see cycle._capture_period_usage_locked); that per-school detail is
    what makes multi-school pricing possible here. The manual/console path
    (services.capture_usage_snapshot) only ever records one organization-wide count, so it can
    only safely price a single-school organization - raising PricingPolicyUndefined, not
    guessing, for anything else.
    """

    as_of = snapshot.period_end or timezone.now()
    school_meters = (snapshot.metadata or {}).get("schoolMeters")
    plan = subscription.plan
    fee_for = annual_fee_for_school if (plan and plan.billing_interval == BillingInterval.ANNUAL) else fixed_fee_for_school

    if not school_meters:
        if snapshot.active_school_count != 1:
            raise PricingPolicyUndefined(
                f"This organization has {snapshot.active_school_count} active schools. This usage "
                "snapshot has no per-school breakdown, so only a single-school organization can be "
                "safely priced from it."
            )
        school = School.objects.get(organization=subscription.organization, is_active=True)
        count = snapshot.billable_student_count or 0
        return [SchoolFee(school_id=str(school.id), billable_student_count=count, fee=fee_for(school, billable_student_count=count, as_of=as_of))]

    not_ready = [detail["schoolId"] for detail in school_meters if not detail.get("ready")]
    if not_ready:
        raise PricingPolicyUndefined(
            f"Not every active school has an authoritative roster count yet ({len(not_ready)} missing)."
        )
    schools_by_id = {str(s.id): s for s in School.objects.filter(id__in=[detail["schoolId"] for detail in school_meters])}
    results = []
    for detail in school_meters:
        school = schools_by_id[detail["schoolId"]]
        count = detail["count"]
        results.append(SchoolFee(school_id=detail["schoolId"], billable_student_count=count, fee=fee_for(school, billable_student_count=count, as_of=as_of)))
    return results


def _ranges_overlap(a_min: int, a_max: int | None, b_min: int, b_max: int | None) -> bool:
    """Inclusive numeric-range overlap; a None max means unbounded above."""
    a_max_ok = a_max is None or a_max >= b_min
    b_max_ok = b_max is None or b_max >= a_min
    return a_max_ok and b_max_ok


def _periods_overlap(a_start, a_end, b_start, b_end) -> bool:
    """Half-open time-period overlap ([start, end)); a None end means still in effect."""
    a_end_ok = a_end is None or a_end > b_start
    b_end_ok = b_end is None or b_end > a_start
    return a_end_ok and b_end_ok


@transaction.atomic
def create_pricing_bracket(
    *,
    min_students: int,
    max_students: int | None,
    fixed_fee_minor: int,
    effective_from,
    effective_until=None,
    currency: str = "NGN",
    actor=None,
) -> PricingBracket:
    if min_students < 1:
        raise ValueError("min_students must be at least 1")
    if max_students is not None and max_students < min_students:
        raise ValueError("max_students cannot be less than min_students")
    if fixed_fee_minor < 0:
        raise ValueError("fixed_fee_minor cannot be negative")
    if effective_until is not None and effective_until <= effective_from:
        raise ValueError("effective_until must be after effective_from")

    for existing in PricingBracket.objects.all():
        if _ranges_overlap(min_students, max_students, existing.min_students, existing.max_students) and _periods_overlap(
            effective_from, effective_until, existing.effective_from, existing.effective_until
        ):
            raise OverlappingBracketError(
                f"Bracket {min_students}-{max_students or '+'} would overlap the existing "
                f"bracket {existing.min_students}-{existing.max_students or '+'} "
                f"({existing.effective_from.isoformat()} onward)."
            )

    bracket = PricingBracket.objects.create(
        min_students=min_students,
        max_students=max_students,
        fixed_fee_minor=fixed_fee_minor,
        effective_from=effective_from,
        effective_until=effective_until,
        currency=currency,
        created_by=actor,
    )
    PricingAuditEvent.objects.create(
        actor=actor,
        action="pricing_bracket_created",
        target_type="pricing_bracket",
        target_id=str(bracket.id),
        detail={
            "minStudents": min_students,
            "maxStudents": max_students,
            "fixedFeeMinor": fixed_fee_minor,
            "currency": currency,
            "effectiveFrom": effective_from.isoformat(),
            "effectiveUntil": effective_until.isoformat() if effective_until else None,
        },
    )
    return bracket


@transaction.atomic
def close_pricing_bracket(bracket: PricingBracket, *, effective_until=None, actor=None) -> PricingBracket:
    """Ends a bracket's effect from this moment (or a given time) forward, so a replacement
    covering the same enrollment range can be created without overlapping it. Never deletes
    the row: every invoice line already issued under it keeps its own copied fee, so the
    historical bracket stays on record as what was actually charged."""

    effective_until = effective_until or timezone.now()
    if effective_until <= bracket.effective_from:
        raise ValueError("effective_until must be after the bracket's own effective_from")
    bracket.effective_until = effective_until
    bracket.save(update_fields=["effective_until"])
    PricingAuditEvent.objects.create(
        actor=actor,
        action="pricing_bracket_closed",
        target_type="pricing_bracket",
        target_id=str(bracket.id),
        detail={"effectiveUntil": effective_until.isoformat()},
    )
    return bracket


@transaction.atomic
def create_enterprise_agreement(
    *,
    school,
    fixed_fee_minor: int,
    effective_from,
    effective_until=None,
    currency: str = "NGN",
    notes: str = "",
    actor=None,
) -> EnterpriseAgreement:
    if fixed_fee_minor < 0:
        raise ValueError("fixed_fee_minor cannot be negative")
    if effective_until is not None and effective_until <= effective_from:
        raise ValueError("effective_until must be after effective_from")

    agreement = EnterpriseAgreement.objects.create(
        school=school,
        fixed_fee_minor=fixed_fee_minor,
        effective_from=effective_from,
        effective_until=effective_until,
        currency=currency,
        notes=notes,
        created_by=actor,
    )
    PricingAuditEvent.objects.create(
        actor=actor,
        action="enterprise_agreement_created",
        target_type="enterprise_agreement",
        target_id=str(agreement.id),
        detail={
            "schoolId": str(school.id),
            "fixedFeeMinor": fixed_fee_minor,
            "currency": currency,
            "effectiveFrom": effective_from.isoformat(),
            "effectiveUntil": effective_until.isoformat() if effective_until else None,
        },
    )
    return agreement
