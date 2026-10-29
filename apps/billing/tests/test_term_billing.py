"""Term-based billing (Phase C, priority three): SkulGen's primary subscription period is the
academic term, and the period must come from the school's own recorded calendar - never an
invented date. These tests use real AcademicSession/AcademicTerm records, the same ones the
school's own academic-affairs staff would create."""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.billing.cycle import BillingCycleNotReady, run_subscription_cycle
from apps.billing.models import BillingInterval, BillingInvoice, InvoiceStatus
from apps.organizations.services import provision_school
from apps.schools.models import SchoolType

from .base import BillingTestCase


def _term_bounds(term):
    start = timezone.make_aware(datetime.combine(term.starts_on, time.min))
    end = timezone.make_aware(datetime.combine(term.ends_on + timedelta(days=1), time.min))
    return start, end


def _make_term(school, *, code, sequence, starts_on, ends_on, session=None):
    session = session or AcademicSession.objects.create(
        school=school, code=f"session-{code}", name=f"Session {code}",
        starts_on=starts_on, ends_on=ends_on + timedelta(days=90),
    )
    return AcademicTerm.objects.create(
        session=session, code=code, name=f"Term {code}", sequence=sequence,
        starts_on=starts_on, ends_on=ends_on,
    )


class TermPeriodInitializationTests(BillingTestCase):
    def test_a_configured_current_term_becomes_the_first_billing_period(self):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        today = timezone.localdate()
        term = _make_term(self.school, code="t1", sequence=1, starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))

        result = run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()

        self.assertEqual(result["result"], "scheduled")
        expected_start, expected_end = _term_bounds(term)
        self.assertEqual(self.subscription.current_period_start, expected_start)
        self.assertEqual(self.subscription.current_period_end, expected_end)

    def test_no_configured_term_is_refused_not_invented(self):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.current_period_start)

    def test_a_multi_school_organization_is_refused_not_combined(self):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        today = timezone.localdate()
        _make_term(self.school, code="t1", sequence=1, starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))
        provision_school(actor=self.owner, organization_id=self.organization.id, name="Second Campus", school_type=SchoolType.PRIMARY, location="Kano")
        with self.assertRaises(BillingCycleNotReady):
            run_subscription_cycle(self.subscription.id)


class TermInvoicingTests(BillingTestCase):
    def _init_current_term(self, *, starts_on, ends_on):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        term = _make_term(self.school, code="t1", sequence=1, starts_on=starts_on, ends_on=ends_on)
        run_subscription_cycle(self.subscription.id)  # initializes the period to the term's real bounds
        return term

    def _make_period_due_and_metered(self):
        self.add_billable_students(self.school, 20)
        self.publish_meter()
        self.subscription.refresh_from_db()
        self.subscription.current_period_end = timezone.now()  # the term has "ended" as far as billing is concerned
        self.subscription.save(update_fields=["current_period_end"])

    def test_the_term_is_invoiced_once_its_period_end_has_passed(self):
        self._init_current_term(starts_on=timezone.localdate() - timedelta(days=10), ends_on=timezone.localdate() + timedelta(days=1))
        self._make_period_due_and_metered()
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_issued")
        invoice = self.subscription.invoices.get()
        self.assertEqual(invoice.amount_due_minor, 4_000_000)  # 20 students: the 1-100 bracket
        self.assertEqual(invoice.status, InvoiceStatus.OPEN)

    def test_a_school_is_never_charged_twice_for_the_same_term(self):
        self._init_current_term(starts_on=timezone.localdate() - timedelta(days=10), ends_on=timezone.localdate() + timedelta(days=1))
        self._make_period_due_and_metered()
        run_subscription_cycle(self.subscription.id)
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)
        # Re-running immediately reports the still-open invoice as outstanding, never a second charge.
        result_again = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result_again["result"], "invoice_outstanding")
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

    def test_advancing_past_a_billed_term_picks_up_the_next_configured_term(self):
        first_term_end = timezone.localdate() + timedelta(days=1)
        self._init_current_term(starts_on=timezone.localdate() - timedelta(days=10), ends_on=first_term_end)
        second_term = _make_term(
            self.school, code="t2", sequence=2,
            starts_on=first_term_end + timedelta(days=1), ends_on=first_term_end + timedelta(days=60),
            session=AcademicTerm.objects.get(code="t1").session,
        )
        self._make_period_due_and_metered()
        run_subscription_cycle(self.subscription.id)  # bills term 1 and advances
        self.subscription.refresh_from_db()

        expected_start, expected_end = _term_bounds(second_term)
        self.assertEqual(self.subscription.current_period_start, expected_start)
        self.assertEqual(self.subscription.current_period_end, expected_end)

    def test_advancing_with_no_next_term_configured_waits_rather_than_inventing_one(self):
        self._init_current_term(starts_on=timezone.localdate() - timedelta(days=10), ends_on=timezone.localdate() + timedelta(days=1))
        self._make_period_due_and_metered()
        run_subscription_cycle(self.subscription.id)  # bills term 1; no term 2 exists yet
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.current_period_start)
        self.assertIsNone(self.subscription.current_period_end)
        # Term 1's invoice is still open, so the next run reports it outstanding rather than
        # either inventing a next term or silently re-initializing term 1's own period again.
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_outstanding")
