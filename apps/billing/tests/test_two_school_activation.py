"""Phase D, Stage 2, Task 4: a controlled organization with two schools, where School A
completes a real (mocked-provider) Paystack checkout and server-side verification for its
own fixed-bracket invoice while School B never pays.

Every other billing test either drives school_premium_access()/the entitlement view
directly off a hand-set InvoiceStatus.PAID, or drives the Paystack checkout/verify pipeline
for a single-school organization. This file is the one place both are exercised together:
the real checkout -> provider success -> verify/settle pipeline from Tasks 1-3, read back
through the real entitlement view and the real school_premium_access() gate, for a specific
school inside a two-school organization - proving the payment that actually ran unlocks the
school that made it, and only that school.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone

from apps.billing.cycle import run_subscription_cycle
from apps.billing.models import BillingInvoice, InvoiceStatus, PaymentAttempt, PaymentAttemptStatus
from apps.billing.premium_access import school_premium_access
from apps.organizations.services import provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase

PAYSTACK_SETTINGS = override_settings(
    PAYSTACK_SECRET_KEY="sk_test_dummy_secret", PAYSTACK_API_BASE_URL="https://api.paystack.co"
)


@PAYSTACK_SETTINGS
class TwoSchoolActivationTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        self.set_plan()
        self.give_entitlement("cbt_exam_publish")
        self.give_entitlement("report_card_generation")
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 30)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter(self.school)
        self.publish_meter(self.school_b)
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)

        self.invoice_a = BillingInvoice.objects.get(school=self.school)
        self.invoice_b = BillingInvoice.objects.get(school=self.school_b)
        self.assertEqual(self.invoice_a.status, InvoiceStatus.OPEN)
        self.assertEqual(self.invoice_b.status, InvoiceStatus.OPEN)

        # The cutoff above deliberately rewinds the period so run_subscription_cycle() treats
        # it as already over and issues the next one - but that leaves the just-issued
        # invoices' own period_end in the past too, which would make the real entitlement
        # view's own as_of=now() lookup miss them (current_school_invoice requires
        # period_end > as_of). Extend both out to a normal, still-open-ended period so this
        # test's later HTTP reads see the same "current period" as school_premium_access()
        # does with an explicit as_of in the middle of it.
        far_future = timezone.now() + timedelta(days=89)
        BillingInvoice.objects.filter(id__in=[self.invoice_a.id, self.invoice_b.id]).update(
            period_end=far_future
        )
        self.invoice_a.refresh_from_db()
        self.invoice_b.refresh_from_db()

    def _pay_real_invoice(self, invoice):
        """Drives the actual Paystack checkout + verify HTTP endpoints - the same pipeline
        Tasks 1-3 harden - never a direct InvoiceStatus.PAID write."""
        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.side_effect = lambda method, path, payload=None: {
                "status": True,
                "data": {
                    "authorization_url": "https://checkout.paystack.com/abc123",
                    "access_code": "abc123",
                    "reference": payload["reference"],
                },
            }
            checkout = self.post(
                f"organizations/{self.organization.id}/billing/invoices/{invoice.id}/checkout/",
            )
        self.assertEqual(checkout.status_code, 201, checkout.json())
        reference = checkout.json()["payment"]["reference"]

        with patch("apps.billing.providers.PaystackProvider._json_request") as mocked:
            mocked.return_value = {
                "status": True,
                "data": {
                    "status": "success",
                    "amount": invoice.amount_due_minor,
                    "currency": invoice.currency,
                    "id": 42,
                    "paid_at": "2026-01-01T00:00:00Z",
                },
            }
            verify = self.post(
                f"organizations/{self.organization.id}/billing/payments/{reference}/verify/",
            )
        self.assertEqual(verify.status_code, 200, verify.json())
        return reference

    def test_school_a_pays_and_unlocks_while_school_b_stays_unpaid(self):
        self._pay_real_invoice(self.invoice_a)

        self.invoice_a.refresh_from_db()
        self.invoice_b.refresh_from_db()
        self.assertEqual(self.invoice_a.status, InvoiceStatus.PAID)
        self.assertEqual(self.invoice_b.status, InvoiceStatus.OPEN)

        # School B's invoice and attempts are completely untouched by School A's payment.
        self.assertFalse(PaymentAttempt.objects.filter(invoice=self.invoice_b).exists())

        as_of = self._midpoint(self.invoice_a.period_start, self.invoice_a.period_end)
        self.assertTrue(school_premium_access(self.school, as_of=as_of))
        self.assertFalse(school_premium_access(self.school_b, as_of=as_of))

        # The real entitlement view, as a billing authority would read it, agrees.
        detail_a = self.get(f"schools/{self.school.id}/billing/entitlements/").json()
        detail_b = self.get(f"schools/{self.school_b.id}/billing/entitlements/").json()
        self.assertEqual(detail_a["paymentStatus"], "paid")
        self.assertEqual(detail_b["paymentStatus"], "unpaid")
        self.assertTrue(detail_a["entitlements"]["cbt_exam_publish"]["schoolPaid"])
        self.assertFalse(detail_b["entitlements"]["cbt_exam_publish"]["schoolPaid"])

    def test_school_bs_own_payment_afterwards_never_touches_school_as_already_settled_invoice(self):
        self._pay_real_invoice(self.invoice_a)
        self._pay_real_invoice(self.invoice_b)

        self.invoice_a.refresh_from_db()
        self.invoice_b.refresh_from_db()
        self.assertEqual(self.invoice_a.status, InvoiceStatus.PAID)
        self.assertEqual(self.invoice_b.status, InvoiceStatus.PAID)

        as_of = self._midpoint(self.invoice_a.period_start, self.invoice_a.period_end)
        self.assertTrue(school_premium_access(self.school, as_of=as_of))
        self.assertTrue(school_premium_access(self.school_b, as_of=as_of))

        # Two real, separate payment attempts for two real, separate invoices - never a
        # shared one, and the second payment never created a duplicate-collection review
        # against the first (they were for different invoices entirely).
        attempt_a = PaymentAttempt.objects.get(invoice=self.invoice_a)
        attempt_b = PaymentAttempt.objects.get(invoice=self.invoice_b)
        self.assertNotEqual(attempt_a.reference, attempt_b.reference)
        self.assertEqual(
            (attempt_a.status, attempt_b.status),
            (PaymentAttemptStatus.SUCCEEDED, PaymentAttemptStatus.SUCCEEDED),
        )

    def _midpoint(self, start, end):
        return start + (end - start) / 2
