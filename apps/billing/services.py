import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.organizations.models import Organization, OrganizationRole
from apps.schools.models import School

from .models import (
    BillingInvoice,
    BillingInvoiceLine,
    InvoiceStatus,
    OrganizationSubscription,
    PaymentAttempt,
    Plan,
    PlanEntitlement,
    SubscriptionEvent,
    SubscriptionStatus,
    UsageSnapshot,
)
from .pricing import PricingPolicyUndefined, fixed_fees_for_subscription_period, preview_fee_for_subscription

BASELINE_PLAN_CODE = "standard"
BASELINE_PLAN_DEFAULTS = {
    "name": "SkulGen Standard",
    "description": "Baseline SkulGen plan for organization-managed schools.",
    "currency": "NGN",
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


def serialize_plan_entitlements(plan: Plan) -> dict[str, dict]:
    return {
        entitlement.code: {
            "enabled": entitlement.enabled,
            "limit": entitlement.limit_value,
            "metadata": entitlement.metadata,
        }
        for entitlement in plan.entitlements.all()
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
            "Your SkulGen subscription currently restricts account changes. Existing school data remains available."
        )

    entitlement = _plan_entitlement_map(subscription).get(code)
    if entitlement is None or not entitlement.enabled:
        raise PermissionDenied(
            "Your current SkulGen plan does not include this account capability."
        )
    if (
        current_value is not None
        and entitlement.limit_value is not None
        and current_value >= entitlement.limit_value
    ):
        raise PermissionDenied(
            "Your current SkulGen plan has reached the limit for this capability."
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


@transaction.atomic
def admin_set_status(
    subscription: OrganizationSubscription, new_status: str, *, actor_user, source: str
) -> OrganizationSubscription:
    """A platform operator forcing a subscription's status directly (the console; also
    usable anywhere else an operator-driven change is needed). Audited the same way
    apps.billing.admin's own save_model already is, so both paths show up in the one
    SubscriptionEvent history."""

    if new_status not in SubscriptionStatus.values:
        raise ValidationError({"status": "That is not a real subscription status."})
    subscription = OrganizationSubscription.objects.select_for_update().get(pk=subscription.pk)
    previous = subscription.status
    if previous == new_status:
        return subscription
    subscription.status = new_status
    subscription.save(update_fields=["status", "updated_at"])
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="admin_status_changed",
        from_status=previous,
        to_status=new_status,
        detail={"adminUserId": str(actor_user.id), "adminEmail": actor_user.email, "source": source},
    )
    return subscription


@transaction.atomic
def admin_set_plan(
    subscription: OrganizationSubscription, new_plan: Plan, *, actor_user, source: str
) -> OrganizationSubscription:
    subscription = OrganizationSubscription.objects.select_for_update().get(pk=subscription.pk)
    previous_plan_id = subscription.plan_id
    if previous_plan_id == new_plan.id:
        return subscription
    subscription.plan = new_plan
    subscription.save(update_fields=["plan", "updated_at"])
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="admin_plan_changed",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={
            "adminUserId": str(actor_user.id),
            "adminEmail": actor_user.email,
            "source": source,
            "fromPlanId": str(previous_plan_id) if previous_plan_id else None,
            "toPlanId": str(new_plan.id),
        },
    )
    return subscription


def can_manage_billing(role: str) -> bool:
    return role in {
        OrganizationRole.OWNER,
        OrganizationRole.BILLING_ADMINISTRATOR,
    }


def serialize_plan(plan: Plan | None, *, include_entitlements: bool = False) -> dict | None:
    if plan is None:
        return None
    payload = {
        "id": str(plan.id),
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "currency": plan.currency,
        "billingInterval": plan.billing_interval or None,
        "baseAmountMinor": plan.base_amount_minor,
        "studentUnitAmountMinor": plan.student_unit_amount_minor,
    }
    if include_entitlements:
        payload["entitlements"] = serialize_plan_entitlements(plan)
    return payload


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
    preview = preview_fee_for_subscription(subscription)
    return {
        "organizationId": str(organization.id),
        "status": subscription.status,
        "accessMode": access_mode(subscription),
        "plan": serialize_plan(subscription.plan),
        "pricingPreview": (
            {
                "totalMinor": sum(sf.fee.fixed_fee_minor for sf in preview),
                "grossMinor": sum(sf.fee.gross_fee_minor for sf in preview),
                "discountMinor": sum(sf.fee.discount_minor for sf in preview),
                "currency": preview[0].fee.currency,
                "lines": [
                    {
                        "schoolId": sf.school_id,
                        "billableStudentCount": sf.billable_student_count,
                        "fixedFeeMinor": sf.fee.fixed_fee_minor,
                        "pricingSource": sf.fee.source,
                        "bracketMinStudents": sf.fee.min_students,
                        "bracketMaxStudents": sf.fee.max_students,
                        "termCount": sf.fee.term_count,
                    }
                    for sf in preview
                ],
            }
            if preview
            else None
        ),
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


def serialize_payment_attempt(attempt: PaymentAttempt) -> dict:
    return {
        "id": str(attempt.id),
        "provider": attempt.provider,
        "reference": attempt.reference,
        "status": attempt.status,
        "amountMinor": attempt.amount_minor,
        "currency": attempt.currency,
        "checkoutUrl": attempt.checkout_url or None,
        "accessCode": attempt.access_code or None,
        "createdAt": attempt.created_at.isoformat(),
        "succeededAt": attempt.succeeded_at.isoformat() if attempt.succeeded_at else None,
    }


def serialize_invoice_line(line: BillingInvoiceLine) -> dict:
    return {
        "id": str(line.id),
        "schoolId": str(line.school_id),
        "schoolName": line.school.name,
        "billableStudentCount": line.billable_student_count,
        "currency": line.currency,
        "fixedFeeMinor": line.fixed_fee_minor,
        "pricingSource": line.pricing_source,
        "bracketMinStudents": line.bracket_min_students,
        "bracketMaxStudents": line.bracket_max_students,
        "grossFeeMinor": line.gross_fee_minor,
        "discountMinor": line.discount_minor,
        "termCount": line.term_count,
    }


def serialize_invoice(invoice: BillingInvoice, *, include_attempts: bool = False) -> dict:
    metadata = invoice.metadata or {}
    payload = {
        "id": str(invoice.id),
        "number": invoice.number,
        "organizationId": str(invoice.organization_id),
        "status": invoice.status,
        "currency": invoice.currency,
        "baseAmountMinor": invoice.base_amount_minor,
        "studentUnitAmountMinor": invoice.student_unit_amount_minor,
        "billableStudentCount": invoice.billable_student_count,
        "amountDueMinor": invoice.amount_due_minor,
        "amountPaidMinor": invoice.amount_paid_minor,
        "grossAmountMinor": metadata.get("grossAmountMinor", invoice.amount_due_minor),
        "discountMinor": metadata.get("discountMinor", 0),
        "pricingMode": metadata.get("pricingMode"),
        "periodStart": invoice.period_start.isoformat() if invoice.period_start else None,
        "periodEnd": invoice.period_end.isoformat() if invoice.period_end else None,
        "issuedAt": invoice.issued_at.isoformat(),
        "dueAt": invoice.due_at.isoformat() if invoice.due_at else None,
        "paidAt": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "lines": [
            serialize_invoice_line(line)
            for line in invoice.lines.select_related("school").order_by("school__name")
        ],
    }
    if include_attempts:
        payload["paymentAttempts"] = [
            serialize_payment_attempt(attempt)
            for attempt in invoice.payment_attempts.order_by("-created_at", "-id")[:10]
        ]
    return payload


@transaction.atomic
def issue_latest_usage_invoice(organization: Organization) -> BillingInvoice:
    """Issue one invoice from the latest server-side usage snapshot.

    The cadence must already be configured on the plan. The caller cannot supply
    a student count or amount, and one usage snapshot can produce at most one
    invoice, making retries safe.
    """

    subscription = ensure_organization_subscription(organization)
    # No select_related("plan") here: it is a nullable FK, so joining it would make this a LEFT
    # OUTER JOIN, and Postgres refuses FOR UPDATE on the nullable side of one.
    subscription = OrganizationSubscription.objects.select_for_update().get(pk=subscription.pk)
    plan = subscription.plan
    if plan is None:
        raise ValidationError({"message": "This organization does not have a billing plan."})
    if not plan.billing_interval:
        raise ValidationError(
            {"message": "The billing interval has not been configured for this plan yet."}
        )

    snapshot = (
        UsageSnapshot.objects.select_for_update()
        .filter(organization=organization, billable_student_count__isnull=False)
        .order_by("-captured_at", "-id")
        .first()
    )
    if snapshot is None:
        raise ValidationError(
            {"message": "No authoritative billable-student usage snapshot is available yet."}
        )
    existing = BillingInvoice.objects.filter(usage_snapshot=snapshot).first()
    if existing is not None:
        return existing

    students = snapshot.billable_student_count or 0
    try:
        school_fees = fixed_fees_for_subscription_period(subscription, snapshot)
    except PricingPolicyUndefined as problem:
        raise ValidationError({"message": str(problem)}) from problem
    amount_due = sum(sf.fee.fixed_fee_minor for sf in school_fees)
    gross_total = sum(sf.fee.gross_fee_minor for sf in school_fees)
    discount_total = sum(sf.fee.discount_minor for sf in school_fees)
    currency = school_fees[0].fee.currency
    now = timezone.now()
    invoice = BillingInvoice.objects.create(
        organization=organization,
        subscription=subscription,
        usage_snapshot=snapshot,
        number=f"SOS-{now:%Y%m%d}-{uuid.uuid4().hex[:10].upper()}",
        currency=currency,
        base_amount_minor=amount_due,
        student_unit_amount_minor=0,
        billable_student_count=students,
        amount_due_minor=amount_due,
        amount_paid_minor=0 if amount_due else amount_due,
        status=InvoiceStatus.OPEN if amount_due else InvoiceStatus.PAID,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        due_at=snapshot.period_end or subscription.current_period_end,
        paid_at=now if amount_due == 0 else None,
        metadata={
            "planCode": plan.code,
            "billingInterval": plan.billing_interval,
            "usageSource": snapshot.source,
            "pricingMode": "fixed_bracket",
            "schoolCount": len(school_fees),
            "grossAmountMinor": gross_total,
            "discountMinor": discount_total,
        },
    )
    BillingInvoiceLine.objects.bulk_create(
        BillingInvoiceLine(
            invoice=invoice,
            school_id=sf.school_id,
            billable_student_count=sf.billable_student_count,
            currency=sf.fee.currency,
            fixed_fee_minor=sf.fee.fixed_fee_minor,
            pricing_source=sf.fee.source,
            bracket_min_students=sf.fee.min_students,
            bracket_max_students=sf.fee.max_students,
            pricing_bracket_id=sf.fee.bracket_id,
            enterprise_agreement_id=sf.fee.enterprise_agreement_id,
            gross_fee_minor=sf.fee.gross_fee_minor,
            discount_minor=sf.fee.discount_minor,
            term_count=sf.fee.term_count,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
        )
        for sf in school_fees
    )
    SubscriptionEvent.objects.create(
        subscription=subscription,
        event="invoice_issued",
        from_status=subscription.status,
        to_status=subscription.status,
        detail={
            "invoiceId": str(invoice.id),
            "invoiceNumber": invoice.number,
            "amountMinor": amount_due,
            "currency": invoice.currency,
            "billableStudents": students,
        },
    )
    return invoice


