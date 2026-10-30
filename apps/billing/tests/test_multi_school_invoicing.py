"""Multi-school organizations (Phase C, priorities four and the later per-school-billing
step): pricing is determined separately for each school, never by combining every school's
enrollment into one bracket - matching the brief's own worked example: School A (80 students,
NGN40,000), School B (450 students, NGN120,000), School C (1,200 students, NGN250,000),
organization total NGN410,000 - never 1,730 students priced as one school.

Each school also gets its OWN invoice, not a shared line on one combined invoice: paying
school A's invoice settles school A alone, never school B or C - the independent per-school
billing this file's tests are mainly about now."""

from datetime import timedelta

from django.utils import timezone

from apps.billing.cycle import BillingCycleNotReady, run_subscription_cycle
from apps.billing.models import BillingInvoice, InvoiceStatus
from apps.billing.premium_access import school_premium_access
from apps.organizations.services import provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase


class MultiSchoolInvoicingTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.school_b, _ = provision_school(actor=self.owner, organization_id=self.organization.id, name="Campus B", school_type=SchoolType.SECONDARY, location="Kano")
        self.school_c, _ = provision_school(actor=self.owner, organization_id=self.organization.id, name="Campus C", school_type=SchoolType.SECONDARY, location="Jos")

    def _ready_period(self):
        run_subscription_cycle(self.subscription.id)
        self.publish_meter(self.school)
        self.publish_meter(self.school_b)
        self.publish_meter(self.school_c)
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

    def test_each_schools_bracket_is_priced_on_its_own_as_its_own_separate_invoice(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()

        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_issued")
        self.assertEqual(result["schoolCount"], 3)
        invoices = {inv.school_id: inv for inv in self.subscription.invoices.all()}
        self.assertEqual(len(invoices), 3)

        self.assertEqual((invoices[self.school.id].amount_due_minor, invoices[self.school.id].billable_student_count), (4_000_000, 80))
        self.assertEqual((invoices[self.school_b.id].amount_due_minor, invoices[self.school_b.id].billable_student_count), (12_000_000, 450))
        self.assertEqual((invoices[self.school_c.id].amount_due_minor, invoices[self.school_c.id].billable_student_count), (25_000_000, 1_200))
        for school_id, invoice in invoices.items():
            self.assertEqual(invoice.status, InvoiceStatus.OPEN)
            line = invoice.lines.get()
            self.assertEqual(line.school_id, school_id)
            self.assertEqual(line.pricing_source, "bracket")

        # Never one combined NGN410,000 invoice - three separate ones summing to it.
        self.assertEqual(sum(inv.amount_due_minor for inv in invoices.values()), 41_000_000)

    def test_paying_one_schools_invoice_never_settles_or_unlocks_a_sibling_school(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)

        school_a_invoice = BillingInvoice.objects.get(school=self.school)
        school_a_invoice.status = InvoiceStatus.PAID
        school_a_invoice.amount_paid_minor = school_a_invoice.amount_due_minor
        school_a_invoice.save(update_fields=["status", "amount_paid_minor"])

        school_b_invoice = BillingInvoice.objects.get(school=self.school_b)
        school_c_invoice = BillingInvoice.objects.get(school=self.school_c)
        self.assertEqual(school_b_invoice.status, InvoiceStatus.OPEN)
        self.assertEqual(school_c_invoice.status, InvoiceStatus.OPEN)

        as_of = self._midpoint(school_a_invoice.period_start, school_a_invoice.period_end)
        self.assertTrue(school_premium_access(self.school, as_of=as_of))
        self.assertFalse(school_premium_access(self.school_b, as_of=as_of))
        self.assertFalse(school_premium_access(self.school_c, as_of=as_of))

    def _midpoint(self, start, end):
        return start + (end - start) / 2

    def test_one_unpriceable_schools_enrollment_blocks_the_whole_cycle_not_just_its_own_invoice(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        # school_c is left at zero billable students: no safe price exists for it yet.
        self._ready_period()
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_changing_one_schools_bracket_price_afterwards_never_rewrites_the_issued_invoice(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)
        school_a_invoice = BillingInvoice.objects.get(school=self.school)
        original_amount = school_a_invoice.amount_due_minor

        from apps.billing.models import PricingBracket
        from apps.billing.pricing import create_pricing_bracket

        old_bracket = PricingBracket.objects.get(min_students=1, max_students=100, effective_until__isnull=True)
        old_bracket.effective_until = timezone.now()
        old_bracket.save(update_fields=["effective_until"])
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=99_999_999, effective_from=timezone.now())

        school_a_invoice.refresh_from_db()
        self.assertEqual(school_a_invoice.amount_due_minor, original_amount)
        self.assertEqual(school_a_invoice.lines.get().fixed_fee_minor, 4_000_000)

    def test_retrying_the_same_cycle_run_issues_each_school_at_most_one_invoice(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)
        first_ids = set(str(inv.id) for inv in self.subscription.invoices.all())

        # A retry for the same (already-advanced) period reports the now-open invoices as
        # outstanding rather than issuing a second round for any school.
        result_again = run_subscription_cycle(self.subscription.id)
        self.assertIn(result_again["result"], ("invoice_outstanding", "scheduled"))
        second_ids = set(str(inv.id) for inv in self.subscription.invoices.all())
        self.assertEqual(first_ids, second_ids)
