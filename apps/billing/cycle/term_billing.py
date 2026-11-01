"""TERM-interval billing: each active school is billed from its own academic calendar,
independently of every sibling school's calendar and invoice history - never the
organization's shared current_period_start/end, and never a single-school-only shortcut.
Split out of apps.billing.cycle (a single >700-line module) by responsibility, per
apps/core/tests/test_code_size.py's own rule.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.schools.models import School

from ..academic_periods import active_term_for_school, term_bounds
from ..census import current_census, flag_pricing_exception, freeze_school_census, mark_census_invoiced
from ..models import (
    BillingCyclePolicy,
    BillingInvoice,
    CensusStatus,
    OrganizationSubscription,
    SchoolEnrollmentCensus,
    UsageSnapshot,
)
from ..pricing import PricingPolicyUndefined, fixed_fee_for_school
from .invoicing import _create_school_invoice_locked
from .metering import _latest_meter_for_period
from .status import _reconcile_overdue_locked


def _issue_term_invoice_for_school_locked(
    subscription: OrganizationSubscription,
    school: School,
    *,
    policy: BillingCyclePolicy,
    now,
) -> tuple[BillingInvoice | None, str]:
    """One school's own term invoice, from that school's own academic calendar - never the
    organization's shared current_period_start/end, and never single_active_school(). Each
    active school in a TERM-interval organization is billed independently of every sibling's
    own calendar and own invoice history (Phase C, "multiple schools with different academic
    calendars... independently scheduled term invoices").

    Issues as soon as an authoritative roster census exists for the school's current term -
    deliberately not only once the term ends (Phase C, "allow payment during the term"): a
    school must have a real invoice and payment path available early enough in its term to
    unlock premium capabilities before the term is over, not only after. The census itself is
    never invented here either - it is whatever SchoolBillingMeterSnapshot the school's own
    trusted roster source has most recently, authoritatively published (publish_school_billing_meter);
    no invoice is issued until one exists.

    Returns (invoice, "issued") for a genuinely new invoice, (invoice, "already_invoiced") when
    this school/period was already billed (idempotent, not a second charge), or
    (None, <reason>) when nothing could be billed yet - never an exception, since one school's
    unready calendar or census must never stop any sibling school's own billing.
    """

    term = active_term_for_school(school, as_of_date=timezone.localtime(now).date())
    if term is None:
        return None, "no_active_term"
    start, end = term_bounds(term)

    # Matched by the term's own stable identity first, never only by its current dates - a
    # delayed start or an extended closure must never make this look like a brand-new,
    # unbilled period (Phase D Stage 3, Task 4).
    existing = (
        BillingInvoice.objects.filter(subscription=subscription, school=school, academic_term=term)
        .order_by("issued_at", "id")
        .first()
    )
    if existing is None:
        # Backward compatible with invoices issued before academic_term existed on this
        # model - matched by period bounds, the only identity available at the time.
        existing = (
            BillingInvoice.objects.filter(
                subscription=subscription,
                school=school,
                period_start=start,
                period_end=end,
            )
            .order_by("issued_at", "id")
            .first()
        )
    if existing is not None:
        if (existing.period_start, existing.period_end) != (start, end):
            # The term's own dates were amended after this invoice was issued. Its coverage
            # window is kept in sync with the real term - never its amount or status - so a
            # school that already paid for "this term" stays covered through the term's
            # real, current end date rather than appearing to lapse at a now-stale one.
            existing.period_start = start
            existing.period_end = end
            existing.save(update_fields=["period_start", "period_end"])
            # The line's own copy of the coverage window must never go stale alongside it
            # (Stage 4 Task 4/9 audit finding) - nothing reads it for pricing or entitlement
            # (those read the invoice directly), but a stale line would show the wrong
            # coverage window anywhere it's displayed (e.g. the console's invoice detail).
            existing.lines.update(period_start=start, period_end=end)
            # The census this invoice was frozen from (if any) is keyed by its own
            # period_start/period_end snapshot too - without updating it here, a later
            # lookup keyed by the term's now-current bounds would miss this census
            # entirely and risk creating an orphaned duplicate for the same term.
            SchoolEnrollmentCensus.objects.filter(invoice=existing).update(
                period_start=start, period_end=end,
            )
        return existing, "already_invoiced"

    meter = None
    # An early-frozen census (Task 2) always takes priority, with or without a
    # census_cutoff_days policy configured - a school that voluntarily froze its count ahead
    # of time must never have that count silently bypassed by reading the raw latest meter
    # straight past it.
    census = current_census(school, period_start=start, period_end=end)
    if census is not None and census.status == CensusStatus.NEEDS_REVIEW:
        return None, "census_needs_review"
    if census is not None and census.status == CensusStatus.FROZEN:
        meter = census.source_snapshot
        billable_count = census.billable_student_count
    elif policy.census_cutoff_days is None:
        # No cutoff policy configured and no census already frozen - price from whatever
        # authoritative meter exists the moment this cycle run reaches the school, exactly
        # as before census.py existed.
        meter = _latest_meter_for_period(school, period_start=start, as_of=min(now, end))
        if meter is None:
            return None, "awaiting_census"
        billable_count = meter.billable_student_count
    else:
        cutoff_at = (
            timezone.make_aware(datetime.combine(term.starts_on, time.min))
            + timedelta(days=policy.census_cutoff_days)
        )
        census = freeze_school_census(school, period_start=start, period_end=end, cutoff_at=cutoff_at, now=now)
        if census is None:
            return None, "awaiting_census_cutoff"
        if census.status == CensusStatus.NEEDS_REVIEW:
            return None, "census_needs_review"
        meter = census.source_snapshot
        billable_count = census.billable_student_count

    try:
        fee = fixed_fee_for_school(school, billable_student_count=billable_count, as_of=now)
    except PricingPolicyUndefined as problem:
        flag_pricing_exception(school, period_start=start, period_end=end, reason=str(problem), meter=meter)
        return None, "pricing_undefined"

    snapshot = UsageSnapshot.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        period_start=start,
        period_end=end,
        active_school_count=1,
        billable_student_count=billable_count,
        source="automatic_roster_meter_term",
        metadata={
            "schoolId": str(school.id),
            "meterSource": meter.source if meter else None,
            "meterSourceVersion": meter.source_version if meter else None,
        },
    )
    invoice = _create_school_invoice_locked(
        subscription,
        school_id=str(school.id),
        fee=fee,
        billable_student_count=billable_count,
        usage_snapshot=snapshot,
        period_start=start,
        period_end=end,
        policy=policy,
        now=now,
        academic_term=term,
    )
    if census is not None:
        mark_census_invoiced(census, invoice)
    return invoice, "issued"


def _run_term_billing_cycle(subscription: OrganizationSubscription, *, policy: BillingCyclePolicy, now) -> dict:
    """TERM-interval billing for every active school in this organization, each judged solely
    by its own calendar, its own census, and its own invoice history - replacing the
    single-school-only single_active_school() path. One school's missing calendar, missing
    census, or undefined pricing is reported and skipped; it never raises, and never delays or
    blocks a sibling school's own invoice."""

    schools = list(School.objects.filter(organization=subscription.organization, is_active=True).order_by("id"))
    issued = []
    skipped = []
    for school in schools:
        invoice, reason = _issue_term_invoice_for_school_locked(subscription, school, policy=policy, now=now)
        if reason == "issued":
            issued.append(invoice)
        elif reason != "already_invoiced":
            skipped.append({"schoolId": str(school.id), "reason": reason})

    # Debt-driven escalation is still tracked for admin visibility (see status.
    # BILLING_CONTINUES_STATUSES's own docstring) - it just never reaches this far as a block
    # on issuance above.
    _reconcile_overdue_locked(subscription, policy=policy, now=now)

    return {
        "result": "invoice_issued" if issued else "scheduled",
        "subscriptionId": str(subscription.id),
        "invoiceIds": [str(invoice.id) for invoice in issued],
        "schoolCount": len(issued),
        "skipped": skipped,
        "status": subscription.status,
    }
