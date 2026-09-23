import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.organizations.models import Organization, OrganizationRole
from apps.schools.models import School

from .models import (
    BillingInvoice,
    InvoiceStatus,
    OrganizationSubscription,
    PaymentAttempt,
    PaymentAttemptStatus,
    Plan,
    PlanEntitlement,
    ProviderWebhookEvent,
    SubscriptionEvent,
    SubscriptionStatus,
    UsageSnapshot,
    WebhookProcessingStatus,
)
from .providers import PaymentProviderError, provider_for

BASELINE_PLAN_CODE = "standard"
BASELINE_PLAN_DEFAULTS = {
    "name": "SchoolOS Standard",
    "description": "Baseline SchoolOS plan for organization-managed schools.",
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


def serialize_invoice(invoice: BillingInvoice, *, include_attempts: bool = False) -> dict:
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
        "periodStart": invoice.period_start.isoformat() if invoice.period_start else None,
        "periodEnd": invoice.period_end.isoformat() if invoice.period_end else None,
        "issuedAt": invoice.issued_at.isoformat(),
        "dueAt": invoice.due_at.isoformat() if invoice.due_at else None,
        "paidAt": invoice.paid_at.isoformat() if invoice.paid_at else None,
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
    subscription = OrganizationSubscription.objects.select_for_update().select_related("plan").get(
        pk=subscription.pk
    )
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
    amount_due = plan.base_amount_minor + (plan.student_unit_amount_minor * students)
    now = timezone.now()
    invoice = BillingInvoice.objects.create(
        organization=organization,
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
        due_at=snapshot.period_end or subscription.current_period_end,
        paid_at=now if amount_due == 0 else None,
        metadata={
            "planCode": plan.code,
            "billingInterval": plan.billing_interval,
            "usageSource": snapshot.source,
        },
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


def initialize_invoice_payment(
    invoice: BillingInvoice,
    *,
    actor,
    email: str,
    provider_code: str = "paystack",
) -> PaymentAttempt:
    invoice = BillingInvoice.objects.select_related("subscription", "organization").get(pk=invoice.pk)
    if invoice.status == InvoiceStatus.PAID:
        raise ValidationError({"message": "This invoice has already been paid."})
    if invoice.status != InvoiceStatus.OPEN:
        raise ValidationError({"message": "This invoice is not available for payment."})
    remaining = invoice.amount_due_minor - invoice.amount_paid_minor
    if remaining <= 0:
        raise ValidationError({"message": "This invoice has no outstanding balance."})

    reference = f"sos-{uuid.uuid4().hex}"
    attempt = PaymentAttempt.objects.create(
        invoice=invoice,
        provider=provider_code,
        reference=reference,
        amount_minor=remaining,
        currency=invoice.currency,
        initiated_by=actor,
    )
    try:
        provider = provider_for(provider_code)
        checkout = provider.initialize_transaction(
            reference=reference,
            email=email,
            amount_minor=remaining,
            currency=invoice.currency,
            metadata={
                "schoolosInvoiceId": str(invoice.id),
                "schoolosInvoiceNumber": invoice.number,
                "organizationId": str(invoice.organization_id),
            },
        )
    except PaymentProviderError as problem:
        attempt.status = PaymentAttemptStatus.FAILED
        attempt.failure_message = str(problem)[:250]
        attempt.save(update_fields=["status", "failure_message", "updated_at"])
        raise ValidationError({"message": str(problem)}) from problem

    attempt.status = PaymentAttemptStatus.PENDING
    attempt.checkout_url = checkout.authorization_url
    attempt.access_code = checkout.access_code
    attempt.save(
        update_fields=["status", "checkout_url", "access_code", "updated_at"]
    )
    subscription = invoice.subscription
    if subscription.provider != provider_code:
        subscription.provider = provider_code
        subscription.save(update_fields=["provider", "updated_at"])
    return attempt


@transaction.atomic
def settle_payment_attempt(
    reference: str,
    *,
    provider_code: str,
    provider_data: dict,
    source: str,
) -> PaymentAttempt:
    attempt = (
        PaymentAttempt.objects.select_for_update()
        .select_related("invoice", "invoice__subscription")
        .get(reference=reference, provider=provider_code)
    )
    if attempt.status == PaymentAttemptStatus.SUCCEEDED:
        return attempt

    provider_status = provider_data.get("status")
    if provider_status != "success":
        raise ValidationError({"message": "The payment has not completed successfully."})

    try:
        provider_amount = int(provider_data.get("amount"))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"message": "The payment provider returned an invalid amount."}) from exc
    provider_currency = str(provider_data.get("currency") or "").upper()
    if provider_amount != attempt.amount_minor or provider_currency != attempt.currency.upper():
        raise ValidationError({"message": "The payment amount or currency does not match the invoice."})

    now = timezone.now()
    provider_transaction_id = str(provider_data.get("id") or "")[:128]
    attempt.status = PaymentAttemptStatus.SUCCEEDED
    attempt.provider_transaction_id = provider_transaction_id
    attempt.succeeded_at = now
    attempt.failure_message = ""
    attempt.provider_detail = {
        "source": source,
        "providerStatus": provider_status,
        "paidAt": provider_data.get("paid_at") or provider_data.get("paidAt"),
    }
    attempt.save(
        update_fields=[
            "status",
            "provider_transaction_id",
            "succeeded_at",
            "failure_message",
            "provider_detail",
            "updated_at",
        ]
    )

    invoice = BillingInvoice.objects.select_for_update().get(pk=attempt.invoice_id)
    if invoice.status != InvoiceStatus.PAID:
        invoice.amount_paid_minor = invoice.amount_due_minor
        invoice.status = InvoiceStatus.PAID
        invoice.paid_at = now
        invoice.save(update_fields=["amount_paid_minor", "status", "paid_at"])

        subscription = invoice.subscription
        target_status = (
            SubscriptionStatus.ACTIVE
            if subscription.status in {
                SubscriptionStatus.PAST_DUE,
                SubscriptionStatus.GRACE,
                SubscriptionStatus.RESTRICTED,
            }
            else subscription.status
        )
        transition_subscription(
            subscription,
            status=target_status,
            event="invoice_paid",
            detail={
                "invoiceId": str(invoice.id),
                "invoiceNumber": invoice.number,
                "paymentReference": attempt.reference,
                "provider": provider_code,
                "source": source,
            },
        )
    return attempt


def verify_payment_attempt(attempt: PaymentAttempt) -> PaymentAttempt:
    if attempt.status == PaymentAttemptStatus.SUCCEEDED:
        return attempt
    try:
        data = provider_for(attempt.provider).verify_transaction(attempt.reference)
    except PaymentProviderError as problem:
        raise ValidationError({"message": str(problem)}) from problem
    if data.get("status") != "success":
        return attempt
    return settle_payment_attempt(
        attempt.reference,
        provider_code=attempt.provider,
        provider_data=data,
        source="provider_verify",
    )


def process_paystack_webhook(raw_body: bytes, signature: str | None) -> dict:
    provider = provider_for("paystack")
    if not provider.valid_webhook_signature(raw_body, signature):
        raise PermissionDenied("Invalid Paystack webhook signature.")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError({"message": "Invalid webhook payload."}) from exc
    if not isinstance(payload, dict):
        raise ValidationError({"message": "Invalid webhook payload."})

    payload_hash = hashlib.sha256(raw_body).hexdigest()
    event_type = str(payload.get("event") or "")[:80]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    provider_object_ref = str(data.get("id") or data.get("reference") or "")[:128]
    event, created = ProviderWebhookEvent.objects.get_or_create(
        provider="paystack",
        payload_hash=payload_hash,
        defaults={
            "event_type": event_type,
            "provider_object_ref": provider_object_ref,
        },
    )
    if not created and event.status in {
        WebhookProcessingStatus.PROCESSED,
        WebhookProcessingStatus.IGNORED,
    }:
        return {"duplicate": True, "status": event.status}

    try:
        if event_type != "charge.success":
            event.status = WebhookProcessingStatus.IGNORED
            event.detail = {"reason": "unsupported_event"}
        else:
            reference = data.get("reference")
            if not isinstance(reference, str) or not reference:
                raise ValidationError({"message": "Webhook payment reference is missing."})
            try:
                settle_payment_attempt(
                    reference,
                    provider_code="paystack",
                    provider_data=data,
                    source="webhook",
                )
            except PaymentAttempt.DoesNotExist:
                event.status = WebhookProcessingStatus.IGNORED
                event.detail = {"reason": "unknown_payment_reference"}
            else:
                event.status = WebhookProcessingStatus.PROCESSED
                event.detail = {"reference": reference}
    except Exception as problem:
        event.status = WebhookProcessingStatus.FAILED
        event.detail = {"error": type(problem).__name__, "message": str(problem)[:200]}
        event.processed_at = timezone.now()
        event.save(update_fields=["status", "detail", "processed_at"])
        raise

    event.processed_at = timezone.now()
    event.save(update_fields=["status", "detail", "processed_at"])
    return {"duplicate": False, "status": event.status}
