"""Enrollment census policy (Phase D, Stage 3, Task 1): the authoritative, pricing-grade
billable-enrollment count for one school's one billing period, frozen from the real roster
rather than read fresh at whatever moment invoicing happens to run.

Two independent behaviors live here, and only one of them is gated behind commercial
approval:

- `flag_pricing_exception` persists a "this school needs operator review" record whenever
  the cycle cannot safely price a school (zero enrollment, or above every bracket with no
  enterprise agreement) - active unconditionally, with no commercial-policy gate, because it
  changes no invoicing behavior: it only records, for apps.console and reporting, a decision
  the cycle was already making anyway (skip and don't invent a price).

- `freeze_school_census` deliberately delays pricing until a configured cutoff after the
  term's start, so a school isn't priced off a roster that is still mid-admission. This is
  real commercial policy (how long is a census "still open"?) and stays completely inactive
  - the cycle keeps pricing from the latest authoritative meter the moment one exists, exactly
  as before this module existed - until a plan's BillingCyclePolicy.census_cutoff_days is
  explicitly set. See the proposed cutoff policy in the Stage 3 completion report.

Corrections never rewrite a frozen row's own recorded count, source, or the BillingInvoice
already issued from it (if any) - they supersede it with a new current row, preserving full
audit history (Task 1 requirement 10).
"""

from django.db import transaction
from django.utils import timezone

from .models import BillingInvoice, CensusStatus, SchoolEnrollmentCensus

# Deferred to call time, not module load time: apps.billing.cycle's own package __init__
# imports term_billing, which imports this module - importing cycle.metering up here would
# be a circular import. By the time freeze_school_census() actually runs, every module
# involved has finished initializing.


def current_census(school, *, period_start, period_end) -> SchoolEnrollmentCensus | None:
    """The current (non-superseded) census row for this school's period, if any - None means
    nothing about this model has fired yet for this period, not that enrollment is unknown."""

    return (
        SchoolEnrollmentCensus.objects.filter(
            school=school, period_start=period_start, period_end=period_end,
        )
        .exclude(status=CensusStatus.CORRECTED)
        .order_by("-created_at", "-id")
        .first()
    )


def flag_pricing_exception(
    school, *, period_start, period_end, reason: str, meter=None,
) -> SchoolEnrollmentCensus:
    """Records that this school's period could not be safely priced right now - zero
    enrollment, or above every bracket with no enterprise agreement - so an operator can
    review it (apps.console) instead of it only ever existing as a per-run skip reason.
    Idempotent: replaying the same unresolved exception updates the existing NEEDS_REVIEW
    row's reason/meter rather than creating a new one every cycle run."""

    existing = current_census(school, period_start=period_start, period_end=period_end)
    if existing is not None and existing.status == CensusStatus.NEEDS_REVIEW:
        existing.exception_reason = reason[:250]
        existing.source_snapshot = meter
        existing.billable_student_count = meter.billable_student_count if meter else None
        existing.save(update_fields=["exception_reason", "source_snapshot", "billable_student_count", "updated_at"])
        return existing
    if existing is not None:
        # Already OPEN/FROZEN from some other path - never downgrade a real decision to an
        # exception behind its back.
        return existing
    return SchoolEnrollmentCensus.objects.create(
        school=school,
        period_start=period_start,
        period_end=period_end,
        status=CensusStatus.NEEDS_REVIEW,
        exception_reason=reason[:250],
        source_snapshot=meter,
        billable_student_count=meter.billable_student_count if meter else None,
    )


def freeze_school_census(
    school, *, period_start, period_end, cutoff_at, now=None, is_early: bool = False,
) -> SchoolEnrollmentCensus | None:
    """Freezes this school's billable count for this period at the latest authoritative
    meter as of `cutoff_at`, once `now` has reached it. Returns None - freezing nothing -
    when the cutoff hasn't passed yet. Never re-freezes an already FROZEN/NEEDS_REVIEW
    census; call correct_school_census() for that.

    `is_early` marks a voluntary freeze requested before the ordinary cutoff (Task 2) - it
    changes no pricing logic, only how the result is labeled for display and reporting.
    """

    from .cycle.metering import _latest_meter_for_period

    now = now or timezone.now()
    if now < cutoff_at:
        return None
    with transaction.atomic():
        census = current_census(school, period_start=period_start, period_end=period_end)
        if census is None:
            census = SchoolEnrollmentCensus.objects.create(
                school=school, period_start=period_start, period_end=period_end,
                status=CensusStatus.OPEN, cutoff_at=cutoff_at,
            )
        if census.status != CensusStatus.OPEN:
            return census

        meter = _latest_meter_for_period(school, period_start=period_start, as_of=cutoff_at)
        if meter is None or meter.billable_student_count <= 0:
            census.status = CensusStatus.NEEDS_REVIEW
            census.exception_reason = (
                "No authoritative roster census was published by the cutoff."
                if meter is None
                else "The roster census at the cutoff reported zero billable students."
            )
        else:
            census.status = CensusStatus.FROZEN
            census.billable_student_count = meter.billable_student_count
        census.source_snapshot = meter
        census.cutoff_at = cutoff_at
        census.frozen_at = now
        census.is_early = is_early
        census.save(
            update_fields=[
                "status", "exception_reason", "billable_student_count", "source_snapshot",
                "cutoff_at", "frozen_at", "is_early", "updated_at",
            ]
        )
        return census


def mark_census_invoiced(census: SchoolEnrollmentCensus, invoice: BillingInvoice) -> None:
    """Links a frozen census to the invoice actually issued from it, so a later correction
    can tell whether a real invoice already exists off the superseded count (Task 1
    requirement 10) - this never runs on a NEEDS_REVIEW census, since nothing is invoiced
    from one."""

    census.invoice = invoice
    census.save(update_fields=["invoice", "updated_at"])


def correct_school_census(
    census: SchoolEnrollmentCensus, *, actor, reason: str, meter=None, billable_student_count: int | None = None,
) -> SchoolEnrollmentCensus:
    """Documented, auditable correction of an erroneous census (Task 1 requirement 10).

    Never mutates `census`'s own recorded count, source, or invoice link - those stay
    exactly as they were, the historical truth of what was actually frozen and (if
    applicable) actually billed. This only flips its status to CORRECTED and creates a new
    current row carrying the corrected count. If `census.invoice_id` is already set, that
    invoice is left completely untouched: correcting a census never rewrites, re-prices, or
    refunds a paid invoice - it only ensures the NEXT invoice-issuance attempt (there won't
    be one if this period is already invoiced) and every operator-facing view read the
    corrected figure instead.
    """

    reason = reason.strip()
    if not reason:
        raise ValueError("A reason is required to correct an enrollment census.")
    if billable_student_count is None and meter is None:
        raise ValueError("Provide either a replacement meter or an explicit corrected count.")

    with transaction.atomic():
        now = timezone.now()
        census.status = CensusStatus.CORRECTED
        census.corrected_by = actor
        census.corrected_at = now
        census.correction_reason = reason
        census.save(update_fields=["status", "corrected_by", "corrected_at", "correction_reason", "updated_at"])

        corrected_count = billable_student_count if billable_student_count is not None else meter.billable_student_count
        replacement = SchoolEnrollmentCensus.objects.create(
            school=census.school,
            period_start=census.period_start,
            period_end=census.period_end,
            status=CensusStatus.NEEDS_REVIEW if corrected_count <= 0 else CensusStatus.FROZEN,
            cutoff_at=census.cutoff_at,
            billable_student_count=corrected_count,
            source_snapshot=meter,
            is_early=census.is_early,
            frozen_at=now,
            corrected_from=census,
            exception_reason="" if corrected_count > 0 else "The corrected count is still zero.",
        )
        return replacement
