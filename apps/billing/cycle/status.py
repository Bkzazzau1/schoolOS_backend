"""Subscription lifecycle status: debt-driven escalation and recovery.

Split out of apps.billing.cycle (a single >700-line module) by responsibility, per
apps/core/tests/test_code_size.py's own rule. Used by both billing-period flavors
(org_period.py's shared-period path and term_billing.py's per-school path), which is exactly
why this lives below both of them rather than inside the runner that calls them.
"""

from datetime import timedelta

from ..models import BillingInvoice, InvoiceStatus, OrganizationSubscription, SubscriptionEvent, SubscriptionStatus

#: Statuses under which the automatic cycle runner keeps invoicing. Debt-driven escalation
#: (past_due/grace/restricted) only ever reaches as far as RESTRICTED automatically (see
#: _reconcile_overdue_locked) - it still halts default sync entity types and still shows on the
#: organization's own dashboard, but it must never stop issuing or collecting on ANY school's
#: invoice, including a different, paid-up sibling's next one (Phase C: "one unpaid school must
#: not prevent another school's invoice from being issued"). Only SUSPENDED (reachable only
#: through a deliberate operator action, never automatically) and CANCELLED genuinely stop
#: automatic billing.
BILLING_CONTINUES_STATUSES = {
    SubscriptionStatus.TRIAL,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.GRACE,
    SubscriptionStatus.RESTRICTED,
}


def _transition_locked(
    subscription: OrganizationSubscription,
    status: str,
    event: str,
    detail=None,
):
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


def _clear_grace_locked(subscription: OrganizationSubscription):
    if subscription.grace_ends_at is None:
        return
    subscription.grace_ends_at = None
    subscription.save(update_fields=["grace_ends_at", "updated_at"])


def _reconcile_overdue_locked(subscription: OrganizationSubscription, *, policy, now):
    if subscription.status in {SubscriptionStatus.SUSPENDED, SubscriptionStatus.CANCELLED}:
        return None

    open_invoice = (
        BillingInvoice.objects.filter(
            subscription=subscription,
            status=InvoiceStatus.OPEN,
        )
        .order_by("issued_at", "id")
        .first()
    )
    if open_invoice is None:
        if subscription.status in {
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.GRACE,
            SubscriptionStatus.RESTRICTED,
        }:
            _clear_grace_locked(subscription)
            _transition_locked(
                subscription,
                SubscriptionStatus.ACTIVE,
                "billing_balance_cleared",
            )
        return None

    overdue_invoice = (
        BillingInvoice.objects.filter(
            subscription=subscription,
            status=InvoiceStatus.OPEN,
            due_at__isnull=False,
            due_at__lte=now,
        )
        .order_by("due_at", "issued_at", "id")
        .first()
    )
    if overdue_invoice is None:
        return open_invoice

    grace_starts_at = overdue_invoice.due_at + timedelta(days=policy.past_due_days)
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
            "invoiceId": str(overdue_invoice.id),
            "invoiceNumber": overdue_invoice.number,
            "dueAt": overdue_invoice.due_at.isoformat(),
            "graceStartsAt": grace_starts_at.isoformat(),
            "restrictsAt": restricts_at.isoformat(),
        },
    )
    return open_invoice
