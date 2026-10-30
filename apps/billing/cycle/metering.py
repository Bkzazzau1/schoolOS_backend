"""Billing-cycle policy helpers, roster-meter publication, and the automation-status summary
the console reads. Split out of apps.billing.cycle (a single >700-line module) by
responsibility, per apps/core/tests/test_code_size.py's own rule.
"""

import calendar

from django.utils import timezone

from apps.schools.models import School

from ..models import (
    BillingCyclePolicy,
    BillingInterval,
    BillingInvoice,
    InvoiceStatus,
    OrganizationSubscription,
    SchoolBillingMeterSnapshot,
)


def _policy_for(subscription: OrganizationSubscription) -> BillingCyclePolicy | None:
    if not subscription.plan_id:
        return None
    try:
        return subscription.plan.billing_cycle_policy
    except BillingCyclePolicy.DoesNotExist:
        return None


def _policy_complete(policy: BillingCyclePolicy | None) -> bool:
    return bool(
        policy
        and policy.automatic_invoicing_enabled
        and policy.invoice_due_days is not None
        and policy.past_due_days is not None
        and policy.grace_days is not None
    )


def _add_months(value, months: int):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _next_period_end(start, interval: str):
    if interval == BillingInterval.MONTHLY:
        return _add_months(start, 1)
    if interval == BillingInterval.ANNUAL:
        return _add_months(start, 12)
    return None


def publish_school_billing_meter(
    *,
    school: School,
    billable_student_count: int,
    source: str,
    source_version: str,
    measured_at=None,
    authoritative: bool = True,
    metadata: dict | None = None,
) -> SchoolBillingMeterSnapshot:
    """Publish one immutable roster-count observation for billing.

    The caller must be a trusted server-side roster source. This service is the
    integration seam the canonical Student module will call when its active
    billable roster changes. Replaying the same source/version is idempotent.
    """

    if billable_student_count < 0:
        raise ValueError("billable_student_count cannot be negative")
    source = source.strip()
    source_version = source_version.strip()
    if not source:
        raise ValueError("source is required")
    if len(source) > 64:
        raise ValueError("source is too long")
    if not source_version:
        raise ValueError("source_version is required for idempotency")
    if len(source_version) > 128:
        raise ValueError("source_version is too long")

    measured_at = measured_at or timezone.now()
    if timezone.is_naive(measured_at):
        measured_at = timezone.make_aware(measured_at)

    snapshot, created = SchoolBillingMeterSnapshot.objects.get_or_create(
        school=school,
        source=source,
        source_version=source_version,
        defaults={
            "billable_student_count": billable_student_count,
            "authoritative": authoritative,
            "measured_at": measured_at,
            "metadata": metadata or {},
        },
    )
    if not created and (
        snapshot.billable_student_count != billable_student_count
        or snapshot.authoritative != authoritative
    ):
        raise ValueError(
            "This school/source/version was already published with different billing values."
        )
    return snapshot


def _latest_meter_for_period(school: School, *, period_start, as_of):
    return (
        SchoolBillingMeterSnapshot.objects.filter(
            school=school,
            authoritative=True,
            measured_at__gte=period_start,
            measured_at__lte=as_of,
        )
        .order_by("-measured_at", "-id")
        .first()
    )


def _meter_coverage(subscription: OrganizationSubscription, *, as_of=None):
    as_of = as_of or timezone.now()
    schools = list(
        School.objects.filter(
            organization=subscription.organization,
            is_active=True,
        ).order_by("id")
    )
    period_start = subscription.current_period_start
    metered = 0
    total_students = 0
    details = []
    for school in schools:
        if period_start is None:
            meter = (
                SchoolBillingMeterSnapshot.objects.filter(
                    school=school,
                    authoritative=True,
                    measured_at__lte=as_of,
                )
                .order_by("-measured_at", "-id")
                .first()
            )
        else:
            meter = _latest_meter_for_period(
                school,
                period_start=period_start,
                as_of=as_of,
            )
        if meter is None:
            details.append({"schoolId": str(school.id), "ready": False})
            continue
        metered += 1
        total_students += meter.billable_student_count
        details.append(
            {
                "schoolId": str(school.id),
                "ready": True,
                "count": meter.billable_student_count,
                "source": meter.source,
                "sourceVersion": meter.source_version,
                "measuredAt": meter.measured_at.isoformat(),
            }
        )
    return {
        "activeSchools": len(schools),
        "meteredSchools": metered,
        "billableStudents": total_students if metered == len(schools) else None,
        "details": details,
    }


def automation_summary(subscription: OrganizationSubscription) -> dict:
    plan = subscription.plan
    policy = _policy_for(subscription)
    interval = plan.billing_interval if plan else ""
    configured = bool(interval and _policy_complete(policy))
    now = timezone.now()
    coverage_as_of = now
    if subscription.current_period_end is not None and subscription.current_period_end < now:
        coverage_as_of = subscription.current_period_end
    coverage = _meter_coverage(subscription, as_of=coverage_as_of)
    outstanding = (
        BillingInvoice.objects.filter(
            subscription=subscription,
            status=InvoiceStatus.OPEN,
        )
        .order_by("issued_at", "id")
        .first()
    )

    if not plan or not interval:
        state = "cadence_not_configured"
        message = "Set a billing interval before automatic invoicing can start."
    elif policy is None or not policy.automatic_invoicing_enabled:
        state = "automatic_invoicing_off"
        message = "Automatic invoicing is not enabled for this plan."
    elif not _policy_complete(policy):
        state = "policy_incomplete"
        message = "Configure invoice due, past-due and grace durations."
    elif outstanding is not None:
        state = "invoice_outstanding"
        message = "An invoice is awaiting payment before the next cycle can be issued."
    elif (
        interval == BillingInterval.CUSTOM
        and (subscription.current_period_start is None or subscription.current_period_end is None)
    ):
        state = "period_required"
        message = "Set the next billing period dates for this plan."
    elif subscription.current_period_start is not None and coverage["meteredSchools"] < coverage["activeSchools"]:
        state = "awaiting_meter"
        message = "Waiting for authoritative roster counts from every active school."
    elif subscription.current_period_end is None:
        state = "ready_to_initialize"
        message = "The next cycle run will initialize the billing period."
    elif subscription.current_period_end <= now:
        state = "ready_to_invoice"
        message = "The current period has ended and is ready for the cycle runner."
    else:
        state = "scheduled"
        message = "Automatic billing is ready for the current period."

    return {
        "enabled": configured,
        "state": state,
        "message": message,
        "activeSchools": coverage["activeSchools"],
        "meteredSchools": coverage["meteredSchools"],
        "periodStart": (
            subscription.current_period_start.isoformat()
            if subscription.current_period_start
            else None
        ),
        "periodEnd": (
            subscription.current_period_end.isoformat()
            if subscription.current_period_end
            else None
        ),
        "nextInvoiceAt": (
            subscription.current_period_end.isoformat()
            if subscription.current_period_end
            else None
        ),
        "invoiceDueDays": policy.invoice_due_days if policy else None,
        "pastDueDays": policy.past_due_days if policy else None,
        "graceDays": policy.grace_days if policy else None,
    }
