"""The automatic cycle runner: one subscription at a time (run_subscription_cycle), and the
whole-platform batch driver (run_billing_cycles). Split out of apps.billing.cycle (a single
>700-line module) by responsibility, per apps/core/tests/test_code_size.py's own rule.
"""

from django.db import transaction
from django.utils import timezone

from ..exceptions import BillingCycleNotReady
from ..models import BillingInterval, OrganizationSubscription, SubscriptionStatus
from .invoicing import _issue_period_invoice_locked
from .metering import _policy_complete, _policy_for
from .org_period import _advance_period_locked, _capture_period_usage_locked, _ensure_period_locked
from .status import BILLING_CONTINUES_STATUSES, _reconcile_overdue_locked, _transition_locked
from .term_billing import _run_term_billing_cycle


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

    # Debt-driven escalation (past_due/grace/restricted) is tracked below for admin visibility,
    # but - per Phase C's school-specific freemium policy - it no longer withholds any school's
    # next invoice: one school's unresolved debt must never delay a paid-up sibling's billing,
    # and a school that keeps not paying still needs its next period invoiced, not silently
    # skipped. See status.BILLING_CONTINUES_STATUSES's own docstring for exactly what still
    # stops it.
    _reconcile_overdue_locked(subscription, policy=policy, now=now)

    if subscription.status not in BILLING_CONTINUES_STATUSES:
        return {
            "result": "subscription_inactive",
            "subscriptionId": str(subscription.id),
            "status": subscription.status,
        }

    if plan.billing_interval == BillingInterval.TERM:
        return _run_term_billing_cycle(subscription, policy=policy, now=now)

    _, period_end = _ensure_period_locked(subscription, now=now)
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

    snapshot = _capture_period_usage_locked(subscription)
    invoices = _issue_period_invoice_locked(
        subscription,
        snapshot,
        policy=policy,
        now=now,
    )
    _advance_period_locked(subscription)
    return {
        "result": "invoice_issued",
        "subscriptionId": str(subscription.id),
        "invoiceId": str(invoices[0].id),
        "invoiceNumber": invoices[0].number,
        "invoiceIds": [str(invoice.id) for invoice in invoices],
        "schoolCount": len(invoices),
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
        except Exception as exc:  # process remaining accounts, but report the failure
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
