"""TERM-interval billing: each active school is billed from its own academic calendar,
independently of every sibling school's calendar and invoice history - never the
organization's shared current_period_start/end, and never a single-school-only shortcut.
Split out of apps.billing.cycle (a single >700-line module) by responsibility, per
apps/core/tests/test_code_size.py's own rule.
"""

from django.utils import timezone

from apps.schools.models import School

from ..academic_periods import active_term_for_school, term_bounds
from ..models import BillingCyclePolicy, BillingInvoice, OrganizationSubscription, UsageSnapshot
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
        return existing, "already_invoiced"

    meter = _latest_meter_for_period(school, period_start=start, as_of=min(now, end))
    if meter is None:
        return None, "awaiting_census"

    try:
        fee = fixed_fee_for_school(school, billable_student_count=meter.billable_student_count, as_of=now)
    except PricingPolicyUndefined:
        return None, "pricing_undefined"

    snapshot = UsageSnapshot.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        period_start=start,
        period_end=end,
        active_school_count=1,
        billable_student_count=meter.billable_student_count,
        source="automatic_roster_meter_term",
        metadata={"schoolId": str(school.id), "meterSource": meter.source, "meterSourceVersion": meter.source_version},
    )
    invoice = _create_school_invoice_locked(
        subscription,
        school_id=str(school.id),
        fee=fee,
        billable_student_count=meter.billable_student_count,
        usage_snapshot=snapshot,
        period_start=start,
        period_end=end,
        policy=policy,
        now=now,
    )
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
