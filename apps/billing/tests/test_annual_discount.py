"""The 10% annual-payment discount (Phase C, priority five): based on fixed-bracket pricing,
respecting the school's own academic-session structure - never an assumed number of terms -
applied only through BillingInterval.ANNUAL (never mandatory, never auto-converted: nothing in
this module ever changes a plan's billing_interval on its own), with the original gross price
and discount preserved on the issued invoice, and integer kobo arithmetic with a documented
truncate-toward-zero rounding rule."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.billing.cycle import run_subscription_cycle
from apps.billing.models import BillingInterval
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from ..pricing import PricingPolicyUndefined, annual_fee_for_school, create_enterprise_agreement
from .base import BillingTestCase, verified_user


def _provision_school(name="BrightGate Academy"):
    owner = verified_user(f"owner-{name.lower().replace(' ', '-')}@example.ng")
    organization, _ = create_organization(actor=owner, name=f"{name} Group")
    school, _ = provision_school(actor=owner, organization_id=organization.id, name=name, school_type=SchoolType.SECONDARY, location="Kaduna")
    return school


def _make_session_with_terms(school, *, term_count, starts_on=None):
    starts_on = starts_on or (timezone.localdate() - timedelta(days=30))
    session = AcademicSession.objects.create(
        school=school, code=f"session-{term_count}", name=f"Session ({term_count} terms)",
        starts_on=starts_on, ends_on=starts_on + timedelta(days=270),
    )
    cursor = starts_on
    for sequence in range(1, term_count + 1):
        term_end = cursor + timedelta(days=80)
        AcademicTerm.objects.create(
            session=session, code=f"t{sequence}", name=f"Term {sequence}", sequence=sequence,
            starts_on=cursor, ends_on=term_end,
        )
        cursor = term_end + timedelta(days=10)
    return session


class AnnualFeeCalculationTests(TestCase):
    def test_the_discount_is_based_on_the_sessions_real_term_count_not_an_assumed_three(self):
        two_term_school = _provision_school("Two Term School")
        _make_session_with_terms(two_term_school, term_count=2)
        result = annual_fee_for_school(two_term_school, billable_student_count=50)
        self.assertEqual(result.term_count, 2)
        self.assertEqual(result.gross_fee_minor, 4_000_000 * 2)
        self.assertEqual(result.fixed_fee_minor, (4_000_000 * 2 * 9) // 10)
        self.assertEqual(result.discount_minor, result.gross_fee_minor - result.fixed_fee_minor)

        four_term_school = _provision_school("Four Term School")
        _make_session_with_terms(four_term_school, term_count=4)
        result4 = annual_fee_for_school(four_term_school, billable_student_count=50)
        self.assertEqual(result4.term_count, 4)
        self.assertEqual(result4.gross_fee_minor, 4_000_000 * 4)

    def test_exactly_ten_percent_off_the_gross_sum_of_real_terms(self):
        school = _provision_school()
        _make_session_with_terms(school, term_count=3)
        result = annual_fee_for_school(school, billable_student_count=450)  # 301-600 bracket
        self.assertEqual(result.gross_fee_minor, 12_000_000 * 3)
        self.assertEqual(result.fixed_fee_minor, 32_400_000)  # 36,000,000 * 0.9
        self.assertEqual(result.discount_minor, 3_600_000)

    def test_the_rounding_rule_truncates_toward_zero_never_rounds_up(self):
        school = _provision_school()
        _make_session_with_terms(school, term_count=3)
        create_enterprise_agreement(school=school, fixed_fee_minor=1_000_001, effective_from=timezone.now() - timedelta(days=1))
        result = annual_fee_for_school(school, billable_student_count=5_000)
        gross = 1_000_001 * 3
        expected_net = (gross * 9) // 10
        self.assertEqual((result.gross_fee_minor, result.fixed_fee_minor), (gross, expected_net))
        self.assertLessEqual(result.fixed_fee_minor * 10, result.gross_fee_minor * 9)  # never rounded up

    def test_no_active_session_is_refused_not_assumed(self):
        school = _provision_school()
        with self.assertRaises(PricingPolicyUndefined):
            annual_fee_for_school(school, billable_student_count=50)

    def test_a_session_with_no_terms_recorded_yet_is_refused(self):
        school = _provision_school()
        AcademicSession.objects.create(school=school, code="empty", name="Empty Session", starts_on=timezone.localdate() - timedelta(days=10), ends_on=timezone.localdate() + timedelta(days=260))
        with self.assertRaises(PricingPolicyUndefined):
            annual_fee_for_school(school, billable_student_count=50)


class AnnualInvoicingTests(BillingTestCase):
    def test_an_annual_subscription_is_invoiced_at_the_discounted_multi_term_fee(self):
        self.set_plan(interval=BillingInterval.ANNUAL, base_amount_minor=1_000_000)
        _make_session_with_terms(self.school, term_count=3)
        self.add_billable_students(self.school, 50)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])

        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_issued")
        invoice = self.subscription.invoices.get()
        self.assertEqual(invoice.amount_due_minor, 10_800_000)  # 3 x NGN40,000 x 0.9
        self.assertEqual(invoice.metadata["grossAmountMinor"], 12_000_000)
        self.assertEqual(invoice.metadata["discountMinor"], 1_200_000)
        line = invoice.lines.get()
        self.assertEqual((line.gross_fee_minor, line.discount_minor, line.term_count), (12_000_000, 1_200_000, 3))

    def test_an_annual_plan_is_never_switched_to_on_its_own(self):
        # set_plan defaults to MONTHLY; nothing in the cycle runner ever changes billing_interval.
        self.set_plan(base_amount_minor=1_000_000)
        self.assertEqual(self.subscription.plan.billing_interval, BillingInterval.MONTHLY)
        self.add_billable_students(self.school, 50)
        run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan.billing_interval, BillingInterval.MONTHLY)
