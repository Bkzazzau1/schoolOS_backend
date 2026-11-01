"""automation_summary() must never describe TERM-interval billing in terms of one shared
organization-wide period (Phase D, Stage 3, Task 4): each school is billed from its own
academic calendar (apps.billing.cycle.term_billing), so subscription.current_period_start/end
is never advanced for a TERM subscription, and "any one open invoice on this subscription" is
the wrong question - one school's unpaid invoice must never be reported as blocking a fully
paid sibling's own billing. These tests read the real organization-subscription API response,
not the bare function, to prove what the proprietor's own screen actually sees."""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.billing.cycle import publish_school_billing_meter
from apps.billing.models import BillingInterval, BillingInvoice, InvoiceStatus, UsageSnapshot
from apps.organizations.services import provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase


def _make_term(school, *, code, starts_on, ends_on):
    session = AcademicSession.objects.create(
        school=school, code=f"session-{code}", name=f"Session {code}",
        starts_on=starts_on, ends_on=ends_on + timedelta(days=90),
    )
    return AcademicTerm.objects.create(
        session=session, code=code, name=f"Term {code}", sequence=1,
        starts_on=starts_on, ends_on=ends_on,
    )


def _census(school, count, *, at, key):
    return publish_school_billing_meter(
        school=school, billable_student_count=count, source="canonical_student_roster",
        source_version=f"test:{key}", measured_at=at,
    )


class TermAutomationSummaryTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        today = timezone.localdate()
        self.term = _make_term(
            self.school, code="t1", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50),
        )

    def _automation(self):
        response = self.get(f"organizations/{self.organization.id}/subscription/")
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["automation"]

    def test_a_school_awaiting_its_first_census_is_reported_per_school_not_org_wide(self):
        automation = self._automation()
        self.assertEqual(automation["state"], "awaiting_meter")
        self.assertEqual(automation["billingInterval"], BillingInterval.TERM)
        self.assertEqual(automation["schoolsAwaitingCensus"], 1)
        self.assertEqual(automation["schoolsAwaitingPayment"], 0)
        self.assertEqual(automation["schoolsUpToDate"], 0)
        # Per-school term billing has no single shared period - never a stale org-wide date.
        self.assertIsNone(automation["periodStart"])
        self.assertIsNone(automation["periodEnd"])

    def test_a_school_with_a_census_but_no_invoice_yet_reports_payment_outstanding(self):
        _census(self.school, 50, at=timezone.now(), key="a")
        automation = self._automation()
        self.assertEqual(automation["state"], "invoice_outstanding")
        self.assertEqual(automation["schoolsAwaitingPayment"], 1)
        self.assertEqual(automation["schoolsAwaitingCensus"], 0)

    def test_one_schools_unpaid_invoice_never_reports_a_paid_siblings_billing_as_blocked(self):
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        today = timezone.localdate()
        term_b = _make_term(school_b, code="b1", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))

        _census(self.school, 50, at=timezone.now(), key="paid")
        start_a, end_a = (
            timezone.make_aware(datetime.combine(self.term.starts_on, time.min)),
            timezone.make_aware(datetime.combine(self.term.ends_on + timedelta(days=1), time.min)),
        )
        snapshot_a = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=start_a, period_end=end_a, active_school_count=1,
            billable_student_count=50, source="test",
        )
        invoice_a = BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=self.school,
            usage_snapshot=snapshot_a,
            number="TEST-A", currency="NGN", base_amount_minor=4_000_000, amount_due_minor=4_000_000,
            amount_paid_minor=4_000_000, status=InvoiceStatus.PAID, period_start=start_a, period_end=end_a,
        )
        self.assertTrue(invoice_a.id)

        _census(school_b, 50, at=timezone.now(), key="unpaid")
        start_b, end_b = (
            timezone.make_aware(datetime.combine(term_b.starts_on, time.min)),
            timezone.make_aware(datetime.combine(term_b.ends_on + timedelta(days=1), time.min)),
        )
        snapshot_b = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=start_b, period_end=end_b, active_school_count=1,
            billable_student_count=50, source="test",
        )
        BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=school_b,
            usage_snapshot=snapshot_b,
            number="TEST-B", currency="NGN", base_amount_minor=4_000_000, amount_due_minor=4_000_000,
            amount_paid_minor=0, status=InvoiceStatus.OPEN, period_start=start_b, period_end=end_b,
        )

        automation = self._automation()
        # School B's open invoice is reported, but never as something blocking school A, which
        # is fully paid - the org-wide branch's "any one open invoice" check would have wrongly
        # reported this whole organization as outstanding.
        self.assertEqual(automation["schoolsUpToDate"], 1)
        self.assertEqual(automation["schoolsAwaitingPayment"], 1)
        self.assertEqual(automation["state"], "invoice_outstanding")

    def test_automatic_invoicing_off_is_reported_before_any_per_school_breakdown(self):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000, automatic=False)
        automation = self._automation()
        self.assertEqual(automation["state"], "automatic_invoicing_off")
        self.assertFalse(automation["enabled"])
