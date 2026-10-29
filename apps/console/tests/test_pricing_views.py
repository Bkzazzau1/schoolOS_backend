"""The console's pricing pages (Phase C, section 12): platform operators manage fixed brackets
and enterprise agreements here - never a school or organization's own account - and every
change is audited. Reuses apps.billing.pricing's already-tested overlap/validation rules; these
tests cover the console wiring (access, forms, error surfacing), not the rules themselves."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.billing.cycle import run_subscription_cycle
from apps.billing.models import EnterpriseAgreement, PricingAuditEvent, PricingBracket
from apps.billing.tests.base import BillingTestCase

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class PricingConsoleAccessTests(TestCase):
    def setUp(self):
        self.ordinary_user = User.objects.create_user("owner@school.ng", PASSWORD)
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)

    def test_an_ordinary_account_cannot_reach_the_pricing_page(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.get(reverse("console:pricing"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_platform_staff_can_open_it(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("console:pricing"))
        self.assertEqual(response.status_code, 200)


class PricingBracketConsoleTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user("ops2@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)

    def test_the_page_lists_the_seeded_brackets(self):
        response = self.client.get(reverse("console:pricing"))
        self.assertGreaterEqual(len(response.context["brackets"]), 5)

    def test_creating_an_overlapping_bracket_is_refused_with_a_message_not_a_500(self):
        response = self.client.post(reverse("console:pricing"), {
            "action": "create_bracket", "min_students": "50", "max_students": "150", "fixed_fee_naira": "50000",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("overlap" in m for m in messages))

    def test_creating_a_bracket_for_an_open_range_works_and_is_audited(self):
        response = self.client.post(reverse("console:pricing"), {
            "action": "create_bracket", "min_students": "5000", "max_students": "", "fixed_fee_naira": "500000",
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        bracket = PricingBracket.objects.get(min_students=5000)
        self.assertEqual((bracket.max_students, bracket.fixed_fee_minor), (None, 50_000_000))
        self.assertTrue(PricingAuditEvent.objects.filter(action="pricing_bracket_created", target_id=str(bracket.id)).exists())

    def test_closing_a_current_bracket_sets_its_effective_until(self):
        bracket = PricingBracket.objects.filter(min_students=1, max_students=100, effective_until__isnull=True).get()
        self.client.post(reverse("console:pricing"), {"action": "close_bracket", "bracket_id": str(bracket.id)})
        bracket.refresh_from_db()
        self.assertIsNotNone(bracket.effective_until)

    def test_an_invalid_fee_is_refused_with_a_message(self):
        response = self.client.post(reverse("console:pricing"), {
            "action": "create_bracket", "min_students": "5000", "max_students": "", "fixed_fee_naira": "not-a-number",
        }, follow=True)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("Naira amount" in m for m in messages))
        self.assertFalse(PricingBracket.objects.filter(min_students=5000).exists())


class SchoolEnterpriseAgreementConsoleTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user("ops3@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)

    def _url(self):
        return reverse("console:school_enterprise_agreement", args=[self.school.id])

    def test_shows_no_agreement_yet_for_a_fresh_school(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["agreements"]), [])

    def test_creating_an_agreement_lets_the_school_be_priced_above_every_bracket(self):
        self.client.post(self._url(), {"fixed_fee_naira": "400000", "notes": "Negotiated for 3,500 students"})
        agreement = EnterpriseAgreement.objects.get(school=self.school)
        self.assertEqual(agreement.fixed_fee_minor, 40_000_000)
        self.assertEqual(agreement.notes, "Negotiated for 3,500 students")

        from apps.billing.pricing import fixed_fee_for_school
        result = fixed_fee_for_school(self.school, billable_student_count=3_500)
        self.assertEqual((result.fixed_fee_minor, result.source), (40_000_000, "enterprise"))


class OrganizationDetailShowsBillingHistoryTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user("ops4@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)

    def test_an_issued_invoices_line_items_and_a_payment_attempt_both_show_on_the_organization_page(self):
        self.set_plan(base_amount_minor=1_000_000)
        self.add_billable_students(self.school, 80)
        run_subscription_cycle(self.subscription.id)
        self.publish_meter()
        cutoff = timezone.now()
        self.subscription.refresh_from_db()
        self.subscription.current_period_start = cutoff - timedelta(hours=1)
        self.subscription.current_period_end = cutoff
        self.subscription.save(update_fields=["current_period_start", "current_period_end"])
        run_subscription_cycle(self.subscription.id)
        invoice = self.subscription.invoices.get()

        from apps.billing.models import PaymentAttempt, PaymentAttemptStatus
        PaymentAttempt.objects.create(invoice=invoice, provider="paystack", reference="sos-console-test", amount_minor=invoice.amount_due_minor, currency=invoice.currency, status=PaymentAttemptStatus.PENDING)

        response = self.client.get(reverse("console:organization_detail", args=[self.organization.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["invoices"]), 1)
        self.assertEqual(response.context["invoices"][0].lines.get().school_id, self.school.id)
        self.assertEqual(len(response.context["payment_attempts"]), 1)
