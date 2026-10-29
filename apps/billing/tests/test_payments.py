"""Secure subscription payments: Paystack checkout, server-to-server verification, and the signed,
idempotent webhook receiver. A frontend success notification must never independently mark an
invoice paid - every settlement here goes through settle_payment_attempt's own amount/currency check,
never a client-supplied flag."""

import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import override_settings

from apps.billing.models import (
    InvoiceStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    ProviderWebhookEvent,
    WebhookProcessingStatus,
)
from apps.billing.providers import PaymentProviderError
from apps.billing.services import capture_usage_snapshot, issue_latest_usage_invoice

from .base import BillingTestCase, verified_user

PAYSTACK_SETTINGS = override_settings(PAYSTACK_SECRET_KEY="sk_test_dummy_secret", PAYSTACK_API_BASE_URL="https://api.paystack.co")


def _sign(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()


@PAYSTACK_SETTINGS
class PaymentsTestCase(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.set_plan(base_amount_minor=1_000_000, automatic=False)
        capture_usage_snapshot(self.organization, billable_student_count=0, source="manual_test")
        self.invoice = issue_latest_usage_invoice(self.organization)

    def checkout(self, *, who=None, invoice=None):
        invoice = invoice or self.invoice
        return self.post(
            f"organizations/{self.organization.id}/billing/invoices/{invoice.id}/checkout/",
            who=who,
        )

    def verify(self, reference, *, who=None):
        return self.post(
            f"organizations/{self.organization.id}/billing/payments/{reference}/verify/",
            who=who,
        )


class InvoiceCheckoutTests(PaymentsTestCase):
    def test_checkout_creates_a_pending_attempt_for_the_exact_outstanding_amount(self):
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.side_effect = lambda method, path, payload=None: {
                "status": True,
                "data": {
                    "authorization_url": "https://checkout.paystack.com/abc123",
                    "access_code": "abc123",
                    "reference": payload["reference"],
                },
            }
            response = self.checkout()
        self.assertEqual(response.status_code, 201, response.json())
        attempt = PaymentAttempt.objects.get(invoice=self.invoice)
        self.assertEqual(attempt.status, PaymentAttemptStatus.PENDING)
        self.assertEqual(attempt.amount_minor, self.invoice.amount_due_minor)
        self.assertEqual(attempt.checkout_url, "https://checkout.paystack.com/abc123")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.OPEN)  # unchanged until actually paid

    def test_checkout_is_refused_for_an_invoice_that_is_already_paid(self):
        self.invoice.status = InvoiceStatus.PAID
        self.invoice.amount_paid_minor = self.invoice.amount_due_minor
        self.invoice.save(update_fields=["status", "amount_paid_minor"])
        response = self.checkout()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PaymentAttempt.objects.filter(invoice=self.invoice).exists())

    def test_checkout_is_refused_to_a_member_without_billing_authority(self):
        stranger = verified_user("stranger@example.ng")
        response = self.checkout(who=stranger)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PaymentAttempt.objects.filter(invoice=self.invoice).exists())

    def test_a_failed_provider_initialize_marks_the_attempt_failed_and_refuses(self):
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.side_effect = PaymentProviderError("Paystack could not be reached or returned an invalid response.")
            response = self.checkout()
        self.assertEqual(response.status_code, 400)
        attempt = PaymentAttempt.objects.get(invoice=self.invoice)
        self.assertEqual(attempt.status, PaymentAttemptStatus.FAILED)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.OPEN)


class PaymentVerifyTests(PaymentsTestCase):
    def _initiate(self):
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.side_effect = lambda method, path, payload=None: {
                "status": True,
                "data": {
                    "authorization_url": "https://checkout.paystack.com/abc123",
                    "access_code": "abc123",
                    "reference": payload["reference"],
                },
            }
            self.checkout()
        return PaymentAttempt.objects.get(invoice=self.invoice)

    def test_verifying_a_matching_successful_transaction_settles_the_invoice(self):
        attempt = self._initiate()
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.return_value = {
                "status": True,
                "data": {
                    "status": "success",
                    "amount": attempt.amount_minor,
                    "currency": attempt.currency,
                    "id": 555,
                    "paid_at": "2026-01-01T00:00:00Z",
                },
            }
            response = self.verify(attempt.reference)
        self.assertEqual(response.status_code, 200, response.json())
        self.invoice.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((self.invoice.status, attempt.status), (InvoiceStatus.PAID, PaymentAttemptStatus.SUCCEEDED))

    def test_a_provider_amount_mismatch_is_refused_and_settles_nothing(self):
        attempt = self._initiate()
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.return_value = {
                "status": True,
                "data": {"status": "success", "amount": attempt.amount_minor + 1, "currency": attempt.currency},
            }
            response = self.verify(attempt.reference)
        self.assertEqual(response.status_code, 400)
        self.invoice.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual((self.invoice.status, attempt.status), (InvoiceStatus.OPEN, PaymentAttemptStatus.PENDING))

    def test_a_provider_report_of_a_still_pending_transaction_settles_nothing(self):
        attempt = self._initiate()
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.return_value = {"status": True, "data": {"status": "abandoned"}}
            response = self.verify(attempt.reference)
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttemptStatus.PENDING)

    def test_verifying_an_already_succeeded_payment_is_a_no_op_not_a_second_settlement(self):
        attempt = self._initiate()
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.return_value = {
                "status": True,
                "data": {"status": "success", "amount": attempt.amount_minor, "currency": attempt.currency},
            }
            self.verify(attempt.reference)
            response_again = self.verify(attempt.reference)
        self.assertEqual(response_again.status_code, 200)
        self.assertEqual(mocked.call_count, 1)  # already-succeeded attempts never call the provider again

    def test_verify_is_refused_to_an_outsider(self):
        attempt = self._initiate()
        stranger = verified_user("stranger2@example.ng")
        response = self.verify(attempt.reference, who=stranger)
        self.assertEqual(response.status_code, 403)


@PAYSTACK_SETTINGS
class PaystackWebhookTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.set_plan(base_amount_minor=1_000_000, automatic=False)
        capture_usage_snapshot(self.organization, billable_student_count=0, source="manual_test")
        self.invoice = issue_latest_usage_invoice(self.organization)
        self.attempt = PaymentAttempt.objects.create(
            invoice=self.invoice, provider="paystack", reference="sos-webhook-ref",
            amount_minor=self.invoice.amount_due_minor, currency=self.invoice.currency,
            status=PaymentAttemptStatus.PENDING, initiated_by=self.owner,
        )

    def send(self, body: dict, *, signature=None):
        raw = json.dumps(body).encode("utf-8")
        sig = signature if signature is not None else _sign("sk_test_dummy_secret", raw)
        return self.client.post(
            "/api/v1/billing/webhooks/paystack/",
            data=raw,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=sig,
        )

    def charge_success_payload(self, **data_overrides):
        data = {"reference": self.attempt.reference, "status": "success", "amount": self.attempt.amount_minor, "currency": self.attempt.currency}
        data.update(data_overrides)
        return {"event": "charge.success", "data": data}

    def test_an_invalid_signature_is_refused_and_settles_nothing(self):
        response = self.send(self.charge_success_payload(), signature="not-the-real-signature")
        self.assertEqual(response.status_code, 403)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PaymentAttemptStatus.PENDING)

    def test_a_validly_signed_charge_success_settles_the_matching_payment(self):
        response = self.send(self.charge_success_payload())
        self.assertEqual(response.status_code, 200, response.json())
        self.invoice.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual((self.invoice.status, self.attempt.status), (InvoiceStatus.PAID, PaymentAttemptStatus.SUCCEEDED))
        event = ProviderWebhookEvent.objects.get(provider="paystack")
        self.assertEqual(event.status, WebhookProcessingStatus.PROCESSED)

    def test_a_replayed_identical_webhook_is_reported_as_a_duplicate_and_changes_nothing_again(self):
        self.send(self.charge_success_payload())
        response = self.send(self.charge_success_payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(ProviderWebhookEvent.objects.filter(provider="paystack").count(), 1)

    def test_an_unsupported_event_type_is_ignored_not_treated_as_payment(self):
        response = self.send({"event": "subscription.disable", "data": {}})
        self.assertEqual(response.status_code, 200)
        event = ProviderWebhookEvent.objects.get(provider="paystack")
        self.assertEqual(event.status, WebhookProcessingStatus.IGNORED)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PaymentAttemptStatus.PENDING)

    def test_a_charge_success_for_an_unknown_reference_is_ignored_not_errored(self):
        response = self.send(self.charge_success_payload(reference="no-such-reference"))
        self.assertEqual(response.status_code, 200)
        event = ProviderWebhookEvent.objects.get(provider="paystack")
        self.assertEqual(event.status, WebhookProcessingStatus.IGNORED)

    def test_a_charge_success_with_a_mismatched_amount_is_recorded_as_failed_and_returns_an_error(self):
        response = self.send(self.charge_success_payload(amount=self.attempt.amount_minor + 999))
        self.assertEqual(response.status_code, 400)
        event = ProviderWebhookEvent.objects.get(provider="paystack")
        self.assertEqual(event.status, WebhookProcessingStatus.FAILED)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, PaymentAttemptStatus.PENDING)

    def test_a_concurrent_second_charge_success_after_settlement_changes_nothing(self):
        self.send(self.charge_success_payload())
        self.invoice.refresh_from_db()
        paid_at = self.invoice.paid_at
        second = self.send(self.charge_success_payload(id=999999))  # same reference, a second provider delivery
        self.assertIn(second.status_code, (200,))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paid_at, paid_at)
