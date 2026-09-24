import calendar
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.schools.models import School

from .models import (
    BillingCyclePolicy,
    BillingInterval,
    BillingInvoice,
    InvoiceStatus,
    OrganizationSubscription,
    SchoolBillingMeterSnapshot,
    SubscriptionEvent,
    SubscriptionStatus,
    UsageSnapshot,
)


class BillingCycleNotReady(Exception):
    """Expected configuration/meter condition that should skip a cycle safely."""


AUTOMATIC_ACCESS_STATUSES = {
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.GRACE,
    SubscriptionStatus.RESTRICTED,
    SubscriptionStatus.TRIAL,
}


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
    source = source.strip()[:64]
    source_version = source_version.strip()[:128]
    if not source:
        raise ValueError("source is required")
    if not source_version:
        raise ValueError("source_version is required for idempotency")
    measured_at = measured_at or timezone.now()

    snapshot, _ = SchoolBillingMeterSnapshot.objects.get_or_create(
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
    coverage = _meter_coverage(subscription)
    outstanding = BillingInvoice.objects.filter(
        subscription=subscription,
        status=InvoiceStatus.OPEN,
    ).order_by("due_at", "issued_at").first()

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
        interval in {BillingInterval.TERM, BillingInterval.CUSTOM}
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


def _transition_locked(subscription: OrganizationSubscription, status: str, event: str, detail=None):
    previous = subscription.status
    if previous == status:
        return False
    subscription.status = status
    subscription.save(update_fields=["status", "updated_at"])
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event=event,
        from_status=previous,
        to_status=status,
        detail=detail or {},
    )
    return True


def _reconcile_overdue_locked(subscription: OrganizationSubscription, *, policy, now):
    if subscription.status in {SubscriptionStatus.SUSPENDED, SubscriptionStatus.CANCELLED}:
        return None
    invoice = (
        BillingInvoice.objects.filter(
            subscription=subscription,
            status=InvoiceStatus.OPEN,
            due_at__isnull=False,
        )
        .order_by("due_at", "issued_at")
        .first()
    )
    if invoice is None or invoice.due_at > now:
        return invoice

    grace_starts_at = invoice.due_at + timedelta(days=policy.past_due_days)
    restricts_at = grace_starts_at + timedelta(days=policy.grace_days)
    if now >= restricts_at:
        target = SubscriptionStatus.RESTRICTED
        event = "billing_restricted"
    elif now >= grace_starts_at:
        target = SubscriptionStatus.GRACE
        event = "billing_grace_started"
    else:
        target = SubscriptionStatus.PAST_DUE
        event = "invoice_past_due"

    if subscription.grace_ends_at != restricts_at:
        subscription.grace_ends_at = restricts_at
        subscription.save(update_fields=["grace_ends_at", "updated_at"])
    _transition_locked(
        subscription,
        target,
        event,
        {
            "invoiceId": str(invoice.id),
            "invoiceNumber": invoice.number,
            "dueAt": invoice.due_at.isoformat(),
            "graceStartsAt": grace_starts_at.isoformat(),
            "restrictsAt": restricts_at.isoformat(),
        },
    )
    return invoice


def _ensure_period_locked(subscription: OrganizationSubscription, *, now):
    start = subscription.current_period_start
    end = subscription.current_period_end
    if (start is None) != (end is None):
        raise BillingCycleNotReady("The subscription billing period is incomplete.")
    if start is not None and end is not None:
        return start, end

    plan = subscription.plan
    interval = plan.billing_interval if plan else ""
    if interval in {BillingInterval.TERM, BillingInterval.CUSTOM}:
        raise BillingCycleNotReady("The next term/custom billing period has not been configured.")
    end = _next_period_end(now, interval)
    if end is None:
        raise BillingCycleNotReady("The billing interval cannot initialize a period automatically.")

    subscription.current_period_start = now
    subscription.current_period_end = end
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "updated_at"]
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="billing_period_initialized",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={"periodStart": now.isoformat(), "periodEnd": end.isoformat()},
    )
    return now, end


def _capture_period_usage_locked(subscription: OrganizationSubscription, *, now):
    period_start = subscription.current_period_start
    period_end = subscription.current_period_end
    if period_start is None or period_end is None:
        raise BillingCycleNotReady("The subscription has no complete billing period.")

    coverage = _meter_coverage(subscription, as_of=now)
    if coverage["meteredSchools"] != coverage["activeSchools"]:
        raise BillingCycleNotReady(
            f"Authoritative roster meters are ready for {coverage['meteredSchools']} of "
            f"{coverage['activeSchools']} active schools."
        )

    existing = UsageSnapshot.objects.filter(
        subscription=subscription,
        source="automatic_roster_meter",
        period_start=period_start,
        period_end=period_end,
    ).order_by("-captured_at", "-id").first()
    if existing is not None:
        return existing

    return UsageSnapshot.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        period_start=period_start,
        period_end=period_end,
        active_school_count=coverage["activeSchools"],
        billable_student_count=coverage["billableStudents"] or 0,
        source="automatic_roster_meter",
        metadata={"schoolMeters": coverage["details"]},
    )


def _issue_period_invoice_locked(subscription: OrganizationSubscription, snapshot, *, policy, now):
    existing = BillingInvoice.objects.filter(
        subscription=subscription,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
    ).exclude(status=InvoiceStatus.VOID).first()
    if existing is not None:
        return existing

    plan = subscription.plan
    students = snapshot.billable_student_count or 0
    amount_due = plan.base_amount_minor + (plan.student_unit_amount_minor * students)
    due_at = now + timedelta(days=policy.invoice_due_days)
    invoice = BillingInvoice.objects.create(
        organization=subscription.organization,
        subscription=subscription,
        usage_snapshot=snapshot,
        number=f"SOS-{now:%Y%m%d}-{uuid.uuid4().hex[:10].upper()}",
        currency=plan.currency,
        base_amount_minor=plan.base_amount_minor,
        student_unit_amount_minor=plan.student_unit_amount_minor,
        billable_student_count=students,
        amount_due_minor=amount_due,
        amount_paid_minor=0 if amount_due else amount_due,
        status=InvoiceStatus.OPEN if amount_due else InvoiceStatus.PAID,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        due_at=due_at,
        paid_at=now if amount_due == 0 else None,
        metadata={
            "planCode": plan.code,
            "billingInterval": plan.billing_interval,
            "usageSource": snapshot.source,
            "automatic": True,
        },
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="automatic_invoice_issued",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={
            "invoiceId": str(invoice.id),
            "invoiceNumber": invoice.number,
            "amountMinor": amount_due,
            "currency": invoice.currency,
            "billableStudents": students,
            "dueAt": due_at.isoformat(),
        },
    )
    return invoice


def _advance_period_locked(subscription: OrganizationSubscription):
    plan = subscription.plan
    old_end = subscription.current_period_end
    if old_end is None:
        return
    next_end = _next_period_end(old_end, plan.billing_interval)
    if next_end is None:
        subscription.current_period_start = None
        subscription.current_period_end = None
        detail = {"waitingForNextPeriod": True}
    else:
        subscription.current_period_start = old_end
        subscription.current_period_end = next_end
        detail = {
            "periodStart": old_end.isoformat(),
            "periodEnd": next_end.isoformat(),
        }
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "updated_at"]
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="billing_period_advanced",
        from_status=subscription.status,
        to_status=subscription.status,
        detail=detail,
    )


@transaction.atomic
def run_subscription_cycle(subscription_id, *, now=None) -> dict:
    now = now or timezone.now()
    subscription = (
        OrganizationSubscription.objects.select_for_update()
        .select_related("organization", "plan")
        .get(pk=subscription_id)
    )
    plan = subscription.plan
    policy = _policy_for(subscription)
    if not plan or not plan.billing_interval:
        raise BillingCycleNotReady("Billing cadence is not configured.")
    if not _policy_complete(policy):
        raise BillingCycleNotReady("Automatic billing policy is not fully configured.")

    if subscription.status == SubscriptionStatus.TRIAL:
        if subscription.trial_ends_at is None or now < subscription.trial_ends_at:
            return {"result": "trial", "subscriptionId": str(subscription.id)}
        _transition_locked(subscription, SubscriptionStatus.ACTIVE, "trial_ended")

    outstanding = _reconcile_overdue_locked(subscription, policy=policy, now=now)
    if outstanding is not None and outstanding.status == InvoiceStatus.OPEN:
        return {
            "result": "invoice_outstanding",
            "subscriptionId": str(subscription.id),
            "invoiceId": str(outstanding.id),
            "status": subscription.status,
        }

    if subscription.status not in AUTOMATIC_ACCESS_STATUSES:
        return {
            "result": "subscription_inactive",
            "subscriptionId": str(subscription.id),
            "status": subscription.status,
        }

    period_start, period_end = _ensure_period_locked(subscription, now=now)
    if period_end > now:
        return {
            "result": "scheduled",
            "subscriptionId": str(subscription.id),
            "periodEnd": period_end.isoformat(),
        }

    if subscription.cancel_at_period_end:
        _transition_locked(
            subscription,
            SubscriptionStatus.CANCELLED,
            "subscription_cancelled_at_period_end",
            {"periodEnd": period_end.isoformat()},
        )
        return {"result": "cancelled", "subscriptionId": str(subscription.id)}

    snapshot = _capture_period_usage_locked(subscription, now=now)
    invoice = _issue_period_invoice_locked(
        subscription,
        snapshot,
        policy=policy,
        now=now,
    )
    _advance_period_locked(subscription)
    return {
        "result": "invoice_issued",
        "subscriptionId": str(subscription.id),
        "invoiceId": str(invoice.id),
        "invoiceNumber": invoice.number,
    }


def run_billing_cycles(*, now=None) -> dict:
    now = now or timezone.now()
    summary = {
        "processed": 0,
        "invoicesIssued": 0,
        "scheduled": 0,
        "outstanding": 0,
        "skipped": 0,
        "errors": [],
    }
    subscription_ids = list(
        OrganizationSubscription.objects.filter(
            organization__is_active=True,
        ).values_list("id", flat=True)
    )
    for subscription_id in subscription_ids:
        summary["processed"] += 1
        try:
            result = run_subscription_cycle(subscription_id, now=now)
        except BillingCycleNotReady:
            summary["skipped"] += 1
            continue
        except Exception as exc:  # keep one account from blocking the whole scheduled run
            summary["errors"].append(
                {"subscriptionId": str(subscription_id), "error": str(exc)[:200]}
            )
            continue

        outcome = result.get("result")
        if outcome == "invoice_issued":
            summary["invoicesIssued"] += 1
        elif outcome == "scheduled":
            summary["scheduled"] += 1
        elif outcome == "invoice_outstanding":
            summary["outstanding"] += 1
        else:
            summary["skipped"] += 1
    return summary
