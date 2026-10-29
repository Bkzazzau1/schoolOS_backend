"""Secure subscription payments: Paystack checkout, server-to-server verification, and the
signed, idempotent webhook receiver. A frontend success notification must never independently
mark an invoice paid - settle_payment_attempt is the one place that does, and only after
checking the provider's own reported amount and currency against what this attempt expects.
"""

import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    BillingInvoice,
    InvoiceStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    ProviderWebhookEvent,
    SubscriptionStatus,
    WebhookProcessingStatus,
)
from .providers import PaymentProviderError, provider_for
from .services import transition_subscription


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
