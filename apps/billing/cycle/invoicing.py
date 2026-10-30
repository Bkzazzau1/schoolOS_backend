"""Issuing one school's own invoice for one period - the shared creation path every automatic
invoicing route (org-wide multi-school periods here; independent per-school term periods in
term_billing.py) uses, so they can never drift into issuing a school's charge differently.
Split out of apps.billing.cycle (a single >700-line module) by responsibility, per
apps/core/tests/test_code_size.py's own rule.
"""

import uuid
from datetime import timedelta

from ..exceptions import BillingCycleNotReady
from ..models import (
    BillingCyclePolicy,
    BillingInvoice,
    BillingInvoiceLine,
    InvoiceStatus,
    OrganizationSubscription,
    SubscriptionEvent,
    UsageSnapshot,
)
from ..pricing import PricingPolicyUndefined, fixed_fees_for_subscription_period


def _create_school_invoice_locked(
    subscription: OrganizationSubscription,
    *,
    school_id,
    fee,
    billable_student_count: int,
    usage_snapshot: UsageSnapshot,
    period_start,
    period_end,
    policy: BillingCyclePolicy,
    now,
) -> BillingInvoice:
    """Issues one school's own invoice for one period - the single creation path every
    automatic invoicing route (org-wide multi-school periods, and independent per-school term
    periods) shares, so they can never drift into issuing a school's charge differently."""

    plan = subscription.plan
    due_at = now + timedelta(days=policy.invoice_due_days)
    amount_due = fee.fixed_fee_minor
    invoice = BillingInvoice.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        school_id=school_id,
        usage_snapshot=usage_snapshot,
        number=f"SOS-{now:%Y%m%d}-{uuid.uuid4().hex[:10].upper()}",
        currency=fee.currency,
        base_amount_minor=amount_due,
        student_unit_amount_minor=0,
        billable_student_count=billable_student_count,
        amount_due_minor=amount_due,
        amount_paid_minor=0 if amount_due else amount_due,
        status=InvoiceStatus.OPEN if amount_due else InvoiceStatus.PAID,
        period_start=period_start,
        period_end=period_end,
        due_at=due_at,
        paid_at=now if amount_due == 0 else None,
        metadata={
            "planCode": plan.code,
            "billingInterval": plan.billing_interval,
            "usageSource": usage_snapshot.source,
            "automatic": True,
            "pricingMode": "fixed_bracket",
        },
    )
    BillingInvoiceLine.objects.create(
        invoice=invoice,
        school_id=school_id,
        billable_student_count=billable_student_count,
        currency=fee.currency,
        fixed_fee_minor=fee.fixed_fee_minor,
        pricing_source=fee.source,
        bracket_min_students=fee.min_students,
        bracket_max_students=fee.max_students,
        pricing_bracket_id=fee.bracket_id,
        enterprise_agreement_id=fee.enterprise_agreement_id,
        gross_fee_minor=fee.gross_fee_minor,
        discount_minor=fee.discount_minor,
        term_count=fee.term_count,
        period_start=period_start,
        period_end=period_end,
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="automatic_invoice_issued",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={
            "invoiceId": str(invoice.id),
            "invoiceNumber": invoice.number,
            "schoolId": school_id,
            "amountMinor": amount_due,
            "currency": invoice.currency,
            "billableStudents": billable_student_count,
            "dueAt": due_at.isoformat(),
        },
    )
    return invoice


def _issue_period_invoice_locked(
    subscription: OrganizationSubscription,
    snapshot: UsageSnapshot,
    *,
    policy: BillingCyclePolicy,
    now,
) -> list[BillingInvoice]:
    """One invoice per active school for this period - never one combined invoice.

    Each school's subscription is paid independently (Phase C): school A's payment never
    unlocks school B's premium features, because they are never the same invoice. Idempotent
    per (subscription, school, period), not just per (subscription, period), so a partial
    retry only creates the schools still missing an invoice for this period.
    """

    try:
        school_fees = fixed_fees_for_subscription_period(subscription, snapshot)
    except PricingPolicyUndefined as problem:
        raise BillingCycleNotReady(str(problem)) from problem

    invoices = []
    for sf in school_fees:
        existing = (
            BillingInvoice.objects.filter(
                subscription=subscription,
                school_id=sf.school_id,
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
            )
            .order_by("issued_at", "id")
            .first()
        )
        if existing is not None:
            invoices.append(existing)
            continue
        invoices.append(
            _create_school_invoice_locked(
                subscription,
                school_id=sf.school_id,
                fee=sf.fee,
                billable_student_count=sf.billable_student_count,
                usage_snapshot=snapshot,
                period_start=snapshot.period_start,
                period_end=snapshot.period_end,
                policy=policy,
                now=now,
            )
        )
    return invoices
