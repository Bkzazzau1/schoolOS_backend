"""The subscription's own shared billing period - MONTHLY/ANNUAL/CUSTOM intervals only (TERM
is billed per school, per that school's own calendar, by term_billing.py instead). Split out
of apps.billing.cycle (a single >700-line module) by responsibility, per
apps/core/tests/test_code_size.py's own rule.
"""

from ..exceptions import BillingCycleNotReady
from ..models import BillingInterval, OrganizationSubscription, SubscriptionEvent, UsageSnapshot
from .metering import _meter_coverage, _next_period_end


def _ensure_period_locked(subscription: OrganizationSubscription, *, now):
    """The subscription's own shared period, for MONTHLY/ANNUAL/CUSTOM intervals only - TERM
    intervals are billed per school, per that school's own calendar, by
    term_billing._run_term_billing_cycle instead, and never touch current_period_start/end at
    all (no single shared period exists to advance when every school in the organization may
    be on its own academic calendar)."""

    start = subscription.current_period_start
    end = subscription.current_period_end
    if (start is None) != (end is None):
        raise BillingCycleNotReady("The subscription billing period is incomplete.")
    if start is not None and end is not None:
        return start, end

    plan = subscription.plan
    interval = plan.billing_interval if plan else ""
    if interval == BillingInterval.CUSTOM:
        raise BillingCycleNotReady("The next custom billing period has not been configured.")
    start = now
    end = _next_period_end(now, interval)
    if end is None:
        raise BillingCycleNotReady("The billing interval cannot initialize a period automatically.")

    subscription.current_period_start = start
    subscription.current_period_end = end
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "updated_at"]
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="billing_period_initialized",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={"periodStart": start.isoformat(), "periodEnd": end.isoformat()},
    )
    return start, end


def _capture_period_usage_locked(subscription: OrganizationSubscription):
    period_start = subscription.current_period_start
    period_end = subscription.current_period_end
    if period_start is None or period_end is None:
        raise BillingCycleNotReady("The subscription has no complete billing period.")

    coverage = _meter_coverage(subscription, as_of=period_end)
    if coverage["meteredSchools"] != coverage["activeSchools"]:
        raise BillingCycleNotReady(
            f"Authoritative roster meters are ready for {coverage['meteredSchools']} of "
            f"{coverage['activeSchools']} active schools."
        )

    existing = (
        UsageSnapshot.objects.filter(
            subscription=subscription,
            source="automatic_roster_meter",
            period_start=period_start,
            period_end=period_end,
        )
        .order_by("-captured_at", "-id")
        .first()
    )
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


def _advance_period_locked(subscription: OrganizationSubscription):
    """Advances the subscription's own shared period - MONTHLY/ANNUAL/CUSTOM only; see
    _ensure_period_locked's docstring for why TERM never uses this."""

    plan = subscription.plan
    old_end = subscription.current_period_end
    if old_end is None:
        return

    next_start = old_end
    next_end = _next_period_end(old_end, plan.billing_interval)

    if next_end is None:
        subscription.current_period_start = None
        subscription.current_period_end = None
        detail = {"waitingForNextPeriod": True}
    else:
        subscription.current_period_start = next_start
        subscription.current_period_end = next_end
        detail = {
            "periodStart": next_start.isoformat(),
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
