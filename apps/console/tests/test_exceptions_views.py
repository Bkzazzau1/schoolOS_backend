"""The console's exception-review pages (Phase D, Stage 3, Task 7 items 4 and 7): census
rows needs operator review, and invoices with more than one genuinely successful provider
collection. Both are platform-staff-only, like every other console page, and both are
documented, auditable resolution paths that never silently rewrite financial history."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing.census import flag_pricing_exception
from apps.billing.models import (
    BillingInvoice,
    CensusStatus,
    DuplicateCollectionReview,
    InvoiceStatus,
    PaymentAttempt,
    PaymentAttemptStatus,
    SchoolEnrollmentCensus,
    UsageSnapshot,
)
from apps.billing.tests.base import BillingTestCase

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class ExceptionConsoleAccessTests(TestCase):
    def setUp(self):
        self.ordinary_user = User.objects.create_user("owner@school.ng", PASSWORD)
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)

    def test_an_ordinary_account_cannot_reach_census_exceptions(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.get(reverse("console:census_exceptions"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_an_ordinary_account_cannot_reach_duplicate_payments(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.get(reverse("console:duplicate_payments"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_platform_staff_can_open_both_pages(self):
        self.client.force_login(self.staff_user)
        self.assertEqual(self.client.get(reverse("console:census_exceptions")).status_code, 200)
        self.assertEqual(self.client.get(reverse("console:duplicate_payments")).status_code, 200)


class CensusExceptionConsoleTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user("ops2@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)
        self.start = timezone.now() - timedelta(days=10)
        self.end = timezone.now() + timedelta(days=50)
        self.census = flag_pricing_exception(
            self.school, period_start=self.start, period_end=self.end,
            reason="Zero billable students at cutoff.",
        )

    def _url(self):
        return reverse("console:census_exceptions")

    def test_lists_schools_needing_review(self):
        response = self.client.get(self._url())
        self.assertEqual(list(response.context["exceptions"]), [self.census])

    def test_correcting_resolves_it_with_a_documented_reason(self):
        response = self.client.post(
            self._url(),
            {
                "census_id": str(self.census.id),
                "billable_student_count": "65",
                "reason": "Registrar confirmed the real headcount by phone.",
            },
        )
        self.assertRedirects(response, self._url())

        self.census.refresh_from_db()
        self.assertEqual(self.census.status, CensusStatus.CORRECTED)
        self.assertIsNone(self.census.billable_student_count)  # the historical record, untouched

        corrected = SchoolEnrollmentCensus.objects.get(corrected_from=self.census)
        self.assertEqual(corrected.status, CensusStatus.FROZEN)
        self.assertEqual(corrected.billable_student_count, 65)

        response = self.client.get(self._url())
        self.assertEqual(list(response.context["exceptions"]), [])

    def test_correcting_without_a_reason_is_refused(self):
        self.client.post(self._url(), {"census_id": str(self.census.id), "billable_student_count": "65", "reason": ""})
        self.census.refresh_from_db()
        self.assertEqual(self.census.status, CensusStatus.NEEDS_REVIEW)


class DuplicatePaymentConsoleTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user("ops3@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)
        self.set_plan(automatic=False)

        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=timezone.now() - timedelta(days=1), period_end=timezone.now() + timedelta(days=89),
            active_school_count=1, billable_student_count=50, source="test",
        )
        invoice = BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=self.school,
            usage_snapshot=snapshot, number="TEST-DUP", currency="NGN",
            base_amount_minor=4_000_000, amount_due_minor=4_000_000, amount_paid_minor=4_000_000,
            status=InvoiceStatus.PAID, period_start=snapshot.period_start, period_end=snapshot.period_end,
        )
        primary = PaymentAttempt.objects.create(
            invoice=invoice, provider="paystack", reference="sos-dup-a", amount_minor=4_000_000,
            currency="NGN", status=PaymentAttemptStatus.SUCCEEDED, initiated_by=self.owner,
        )
        duplicate = PaymentAttempt.objects.create(
            invoice=invoice, provider="paystack", reference="sos-dup-b", amount_minor=4_000_000,
            currency="NGN", status=PaymentAttemptStatus.SUCCEEDED, initiated_by=self.owner,
        )
        self.review = DuplicateCollectionReview.objects.create(
            invoice=invoice, primary_attempt=primary, duplicate_attempt=duplicate,
            detail={"reference": duplicate.reference, "primaryReference": primary.reference},
        )

    def _url(self):
        return reverse("console:duplicate_payments")

    def test_lists_unresolved_duplicate_collections(self):
        response = self.client.get(self._url())
        self.assertEqual(list(response.context["reviews"]), [self.review])
        self.assertIsNone(response.context["reviews"][0].resolved_at)

    def test_marking_resolved_records_who_and_when_and_never_touches_the_payment_attempts(self):
        response = self.client.post(self._url(), {"review_id": str(self.review.id), "resolution_note": "Refunded the duplicate via Paystack dashboard, ref SOS-REFUND-1."})
        self.assertRedirects(response, self._url())

        self.review.refresh_from_db()
        self.assertEqual(self.review.resolved_by, self.staff_user)
        self.assertIsNotNone(self.review.resolved_at)
        self.assertIn("Refunded", self.review.resolution_note)

        # Resolving the review is bookkeeping only - it never un-succeeds either attempt.
        self.review.primary_attempt.refresh_from_db()
        self.review.duplicate_attempt.refresh_from_db()
        self.assertEqual(self.review.primary_attempt.status, PaymentAttemptStatus.SUCCEEDED)
        self.assertEqual(self.review.duplicate_attempt.status, PaymentAttemptStatus.SUCCEEDED)

    def test_resolving_without_a_note_is_refused(self):
        self.client.post(self._url(), {"review_id": str(self.review.id), "resolution_note": ""})
        self.review.refresh_from_db()
        self.assertIsNone(self.review.resolved_at)
