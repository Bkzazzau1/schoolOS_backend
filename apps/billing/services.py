from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from apps.organizations.models import Organization, OrganizationRole
from apps.schools.models import School

from .models import (
    OrganizationSubscription,
    Plan,
    PlanEntitlement,
    SubscriptionEvent,
    SubscriptionStatus,
    UsageSnapshot,
)

BASELINE_PLAN_CODE = "standard"
BASELINE_PLAN_DEFAULTS = {
    "name": "SchoolOS Standard",
    "description": "Baseline SchoolOS plan for organization-managed schools.",
    "currency": "NGN",
    # The accepted per-student commercial amount is represented now, while the
    # collection cadence remains deliberately unset until pricing policy is final.
    "billing_interval": "",
    "base_amount_minor": 0,
    "student_unit_amount_minor": 50_000,
    "is_public": True,
    "is_active": True,
    "sort_order": 10,
}
BASELINE_ENTITLEMENTS = {
    "school_provisioning": {"enabled": True, "limit_value": None},
    "multi_school": {"enabled": True, "limit_value": None},
}

FULL_ACCESS_STATUSES = {
    SubscriptionStatus.TRIAL,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
    SubscriptionStatus.GRACE,
}


def baseline_plan() -> Plan:
    """Return the baseline plan, creating its catalog rows if deployment data is missing."""

    plan, _ = Plan.objects.get_or_create(
        code=BASELINE_PLAN_CODE,
        defaults=BASELINE_PLAN_DEFAULTS,
    )
    for code, defaults in BASELINE_ENTITLEMENTS.items():
        PlanEntitlement.objects.get_or_create(
            plan=plan,
            code=code,
            defaults=defaults,
        )
    return plan


@transaction.atomic
def ensure_organization_subscription(
    organization: Organization,
) -> OrganizationSubscription:
    """Every commercial account has one current subscription record.

    Existing deployments remain usable even if a data migration was skipped or
    restored from an older backup: the runtime can repair the missing baseline
    subscription idempotently.
    """

    plan = baseline_plan()
    subscription, created = OrganizationSubscription.objects.select_for_update().get_or_create(
        organization=organization,
        defaults={
            "plan": plan,
            "status": SubscriptionStatus.ACTIVE,
        },
    )
    update_fields = []
    if subscription.plan_id is None:
        subscription.plan = plan
        update_fields.append("plan")
    if update_fields:
        subscription.save(update_fields=[*update_fields, "updated_at"])
    if created:
        SubscriptionEvent.objects.create(
            subscription=subscription,
            event="subscription_created",
            to_status=subscription.status,
            detail={"planCode": plan.code, "source": "account_bootstrap"},
        )
    return subscription


def access_mode(subscription: OrganizationSubscription) -> str:
    if subscription.status in FULL_ACCESS_STATUSES:
        return "full"
    if subscription.status == SubscriptionStatus.RESTRICTED:
        return "account_restricted"
    return "suspended"


def _plan_entitlement_map(subscription: OrganizationSubscription) -> dict[str, PlanEntitlement]:
    if subscription.plan_id is None:
        return {}
    return {
        entitlement.code: entitlement
        for entitlement in subscription.plan.entitlements.all()
    }


def resolved_entitlements(subscription: OrganizationSubscription) -> dict[str, dict]:
    mode = access_mode(subscription)
    return {
        code: {
            "enabled": entitlement.enabled,
            "available": entitlement.enabled and mode == "full",
            "limit": entitlement.limit_value,
            "metadata": entitlement.metadata,
        }
        for code, entitlement in _plan_entitlement_map(subscription).items()
    }


def require_entitlement(
    organization: Organization,
    code: str,
    *,
    current_value: int | None = None,
) -> OrganizationSubscription:
    subscription = ensure_organization_subscription(organization)
    mode = access_mode(subscription)
    if mode != "full":
        raise PermissionDenied(
            "Your SchoolOS subscription currently restricts account changes. Existing school data remains available."
        )

    entitlement = _plan_entitlement_map(subscription).get(code)
    if entitlement is None or not entitlement.enabled:
        raise PermissionDenied(
            "Your current SchoolOS plan does not include this account capability."
        )
    if (
        current_value is not None
        and entitlement.limit_value is not None
        and current_value >= entitlement.limit_value
    ):
        raise PermissionDenied(
            "Your current SchoolOS plan has reached the limit for this capability."
        )
    return subscription


def require_school_provisioning(
    organization: Organization,
    *,
    current_school_count: int,
) -> OrganizationSubscription:
    subscription = require_entitlement(
        organization,
        "school_provisioning",
        current_value=current_school_count,
    )
    if current_school_count >= 1:
        require_entitlement(organization, "multi_school")
    return subscription


def can_manage_billing(role: str) -> bool:
    return role in {
        OrganizationRole.OWNER,
        OrganizationRole.BILLING_ADMINISTRATOR,
    }


def serialize_plan(plan: Plan | None) -> dict | None:
    if plan is None:
        return None
    return {
        "id": str(plan.id),
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "currency": plan.currency,
        "billingInterval": plan.billing_interval or None,
        "baseAmountMinor": plan.base_amount_minor,
        "studentUnitAmountMinor": plan.student_unit_amount_minor,
    }


def serialize_subscription(
    subscription: OrganizationSubscription,
    *,
    membership_role: str,
) -> dict:
    organization = subscription.organization
    current_school_count = School.objects.filter(
        organization=organization,
        is_active=True,
    ).count()
    latest_usage = (
        UsageSnapshot.objects.filter(organization=organization)
        .order_by("-captured_at", "-id")
        .first()
    )
    entitlements = resolved_entitlements(subscription)
    return {
        "organizationId": str(organization.id),
        "status": subscription.status,
        "accessMode": access_mode(subscription),
        "plan": serialize_plan(subscription.plan),
        "currentPeriodStart": (
            subscription.current_period_start.isoformat()
            if subscription.current_period_start
            else None
        ),
        "currentPeriodEnd": (
            subscription.current_period_end.isoformat()
            if subscription.current_period_end
            else None
        ),
        "trialEndsAt": subscription.trial_ends_at.isoformat() if subscription.trial_ends_at else None,
        "graceEndsAt": subscription.grace_ends_at.isoformat() if subscription.grace_ends_at else None,
        "cancelAtPeriodEnd": subscription.cancel_at_period_end,
        "canManageBilling": can_manage_billing(membership_role),
        "entitlements": entitlements,
        "usage": {
            "activeSchools": current_school_count,
            "billableStudents": (
                latest_usage.billable_student_count if latest_usage else None
            ),
            "capturedAt": latest_usage.captured_at.isoformat() if latest_usage else None,
        },
    }


@transaction.atomic
def transition_subscription(
    subscription: OrganizationSubscription,
    *,
    status: str,
    event: str,
    detail: dict | None = None,
) -> OrganizationSubscription:
    """Single audited state-transition helper for future provider/webhook code."""

    if status not in SubscriptionStatus.values:
        raise ValueError(f"Unknown subscription status: {status}")
    locked = OrganizationSubscription.objects.select_for_update().get(pk=subscription.pk)
    previous = locked.status
    if previous != status:
        locked.status = status
        locked.save(update_fields=["status", "updated_at"])
    SubscriptionEvent.objects.create(
        subscription=locked,
        event=event,
        from_status=previous,
        to_status=status,
        detail=detail or {},
    )
    return locked


def capture_usage_snapshot(
    organization: Organization,
    *,
    billable_student_count: int | None,
    source: str,
    metadata: dict | None = None,
) -> UsageSnapshot:
    subscription = ensure_organization_subscription(organization)
    return UsageSnapshot.objects.create(
        organization=organization,
        subscription=subscription,
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
        active_school_count=School.objects.filter(
            organization=organization,
            is_active=True,
        ).count(),
        billable_student_count=billable_student_count,
        source=source[:64] or "manual",
        metadata=metadata or {},
    )
