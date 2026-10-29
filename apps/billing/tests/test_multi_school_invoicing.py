"""Multi-school organizations (Phase C, priority four): pricing is determined separately for
each school, never by combining every school's enrollment into one bracket. An organization's
invoice carries one line per school, each identifying its own enrollment, bracket, amount and
period - matching the brief's own worked example: School A (80 students, NGN40,000), School B
(450 students, NGN120,000), School C (1,200 students, NGN250,000), organization total
NGN410,000 - never 1,730 students priced as one school."""

from datetime import timedelta

from django.utils import timezone

from apps.billing.cycle import BillingCycleNotReady, run_subscription_cycle
from apps.billing.models import BillingInvoice, BillingInvoiceLine, InvoiceStatus
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

    def test_each_schools_bracket_is_priced_on_its_own_and_summed_into_one_invoice(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()

        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_issued")
        invoice = self.subscription.invoices.get()
        self.assertEqual(invoice.amount_due_minor, 41_000_000)  # NGN40,000 + NGN120,000 + NGN250,000
        self.assertEqual(invoice.status, InvoiceStatus.OPEN)
        self.assertEqual(BillingInvoiceLine.objects.filter(invoice=invoice).count(), 3)

        lines = {line.school_id: line for line in invoice.lines.all()}
        self.assertEqual((lines[self.school.id].billable_student_count, lines[self.school.id].fixed_fee_minor, lines[self.school.id].bracket_min_students, lines[self.school.id].bracket_max_students), (80, 4_000_000, 1, 100))
        self.assertEqual((lines[self.school_b.id].billable_student_count, lines[self.school_b.id].fixed_fee_minor, lines[self.school_b.id].bracket_min_students, lines[self.school_b.id].bracket_max_students), (450, 12_000_000, 301, 600))
        self.assertEqual((lines[self.school_c.id].billable_student_count, lines[self.school_c.id].fixed_fee_minor, lines[self.school_c.id].bracket_min_students, lines[self.school_c.id].bracket_max_students), (1_200, 25_000_000, 1_001, 2_000))
        for line in lines.values():
            self.assertEqual(line.pricing_source, "bracket")
            self.assertEqual((line.period_start, line.period_end), (invoice.period_start, invoice.period_end))

    def test_one_unpriceable_schools_enrollment_blocks_the_whole_invoice_not_just_its_own_line(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        # school_c is left at zero billable students: no safe price exists for it yet.
        self._ready_period()
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_changing_one_schools_bracket_price_afterwards_never_rewrites_the_issued_lines(self):
        self.set_plan()
        self.add_billable_students(self.school, 80)
        self.add_billable_students(self.school_b, 450)
        self.add_billable_students(self.school_c, 1_200)
        self._ready_period()
        run_subscription_cycle(self.subscription.id)
        invoice = self.subscription.invoices.get()
        original_total = invoice.amount_due_minor

        from apps.billing.models import PricingBracket
        from apps.billing.pricing import create_pricing_bracket

        old_bracket = PricingBracket.objects.get(min_students=1, max_students=100, effective_until__isnull=True)
        old_bracket.effective_until = timezone.now()
        old_bracket.save(update_fields=["effective_until"])
        create_pricing_bracket(min_students=1, max_students=100, fixed_fee_minor=99_999_999, effective_from=timezone.now())

        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_due_minor, original_total)
        line_a = invoice.lines.get(school=self.school)
        self.assertEqual(line_a.fixed_fee_minor, 4_000_000)
