"""API-level payload shape for Section 13 (the Flutter subscription screens): the subscription
endpoint's pricingPreview and the invoice endpoint's per-school lines, gross/discount totals -
so the app can show the fixed term fee, the bracket, and the annual discount instead of the
retired per-student calculation."""

from datetime import timedelta

from django.utils import timezone

from apps.billing.cycle import run_subscription_cycle
from apps.billing.models import BillingInterval
from apps.organizations.services import provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase


class SubscriptionPricingPreviewTests(BillingTestCase):
    def test_no_preview_before_any_usage_has_ever_been_captured(self):
        self.set_plan(base_amount_minor=1_000_000)
        response = self.get(f"organizations/{self.organization.id}/subscription/")
        self.assertIsNone(response.json()["pricingPreview"])

    def test_the_preview_reflects_the_schools_own_bracket_once_metered(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 250)  # 101-300 bracket
        run_subscription_cycle(self.subscription.id)  # initializes the period
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)  # captures usage and issues the invoice

        response = self.get(f"organizations/{self.organization.id}/subscription/")
        preview = response.json()["pricingPreview"]
        self.assertEqual(preview["totalMinor"], 7_500_000)
        self.assertEqual(preview["grossMinor"], 7_500_000)
        self.assertEqual(preview["discountMinor"], 0)
        self.assertEqual(len(preview["lines"]), 1)
        self.assertEqual((preview["lines"][0]["bracketMinStudents"], preview["lines"][0]["bracketMaxStudents"]), (101, 300))

    def test_the_preview_reflects_the_annual_discount_once_a_session_is_configured(self):
        from apps.academics.models import AcademicSession, AcademicTerm

        self.set_plan(interval=BillingInterval.ANNUAL, base_amount_minor=1_000_000)
        session = AcademicSession.objects.create(school=self.school, code="s1", name="Session", starts_on=timezone.localdate() - timedelta(days=10), ends_on=timezone.localdate() + timedelta(days=260))
        for n in range(1, 4):
            AcademicTerm.objects.create(session=session, code=f"t{n}", name=f"Term {n}", sequence=n, starts_on=timezone.localdate(), ends_on=timezone.localdate() + timedelta(days=80))
        self.add_billable_students(self.school, 50)
        run_subscription_cycle(self.subscription.id)  # initializes the period
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)  # captures usage and issues the invoice

        response = self.get(f"organizations/{self.organization.id}/subscription/")
        preview = response.json()["pricingPreview"]
        self.assertEqual(preview["grossMinor"], 12_000_000)
        self.assertEqual(preview["totalMinor"], 10_800_000)
        self.assertEqual(preview["discountMinor"], 1_200_000)
        self.assertEqual(preview["lines"][0]["termCount"], 3)


class InvoiceSerializationTests(BillingTestCase):
    def _ready_period(self):
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

    def test_the_invoice_list_includes_per_school_lines_and_totals(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 80)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)

        response = self.get(f"organizations/{self.organization.id}/billing/invoices/")
        invoice = response.json()["invoices"][0]
        self.assertEqual(invoice["grossAmountMinor"], 4_000_000)
        self.assertEqual(invoice["discountMinor"], 0)
        self.assertEqual(invoice["pricingMode"], "fixed_bracket")
        self.assertEqual(len(invoice["lines"]), 1)
        line = invoice["lines"][0]
        self.assertEqual((line["schoolName"], line["billableStudentCount"], line["fixedFeeMinor"], line["pricingSource"]), (self.school.name, 80, 4_000_000, "bracket"))

    def test_a_multi_school_invoices_lines_each_name_their_own_school(self):
        self.set_plan(base_amount_minor=1_000_000)
        other_school, _ = provision_school(actor=self.owner, organization_id=self.organization.id, name="Second Campus", school_type=SchoolType.PRIMARY, location="Kano")
        self.add_billable_students(self.school, 80)
        self.add_billable_students(other_school, 450)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter(self.school)
        self.publish_meter(other_school)
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)

        response = self.get(f"organizations/{self.organization.id}/billing/invoices/")
        invoices = response.json()["invoices"]
        # Each school gets its own invoice now, not a shared line on one combined invoice.
        self.assertEqual(len(invoices), 2)
        by_school_name = {inv["schoolName"]: inv for inv in invoices}
        self.assertEqual(sorted(by_school_name), sorted([self.school.name, "Second Campus"]))
        self.assertEqual(by_school_name[self.school.name]["amountDueMinor"], 4_000_000)
        self.assertEqual(by_school_name["Second Campus"]["amountDueMinor"], 12_000_000)
        for invoice in invoices:
            self.assertEqual(len(invoice["lines"]), 1)
            self.assertEqual(invoice["lines"][0]["schoolName"], invoice["schoolName"])
