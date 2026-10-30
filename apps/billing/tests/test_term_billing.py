"""Term-based billing (Phase C, priority three, corrected under the Phase C correction brief):
each school is billed from its OWN academic calendar, independently of every sibling school's
calendar and invoice history (apps.billing.cycle._run_term_billing_cycle), and as soon as an
authoritative roster census exists for its current term - not only once the term has ended
(the correction brief's "allow payment during the term"). These tests use real
AcademicSession/AcademicTerm records, the same ones the school's own academic-affairs staff
would create, and real SchoolBillingMeterSnapshot rows, the same kind a trusted roster source
publishes."""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.billing.cycle import publish_school_billing_meter, run_subscription_cycle
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


def _census(school, count, *, at, key):
    """An authoritative roster census stamped at a specific moment - the real
    publish_school_billing_meter(), not the base test helper, so tests can place the
    observation precisely inside (or outside) a particular term's own window. Uses
    source="canonical_student_roster": apps.students.signals.canonical_roster_owns_automatic_student_meter
    forces every other source to authoritative=False once apps.students exists, the same
    protection real production data gets."""
    return publish_school_billing_meter(
        school=school, billable_student_count=count, source="canonical_student_roster",
        source_version=f"test:{key}", measured_at=at,
    )


class TermBillingReadinessTests(BillingTestCase):
    def test_with_no_term_configured_the_school_is_skipped_not_invented(self):
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "scheduled")
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "no_active_term"}])
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_a_configured_term_with_only_the_zero_student_bootstrap_meter_waits_rather_than_guessing(self):
        """Provisioning a school always publishes one authoritative, zero-student bootstrap
        meter (apps.students.services.bootstrap_school_roster_meter) - so "no census yet" in
        practice means "no real, non-zero census yet", surfaced as pricing_undefined rather
        than a missing meter."""
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        today = timezone.localdate()
        _make_term(self.school, code="t1", sequence=1, starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))

        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "scheduled")
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "pricing_undefined"}])
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())


class TermInvoicingTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        self.today = timezone.localdate()

    def test_the_term_is_invoiced_as_soon_as_a_census_is_published_even_mid_term(self):
        """A school must have a real invoice and payment path available early in its term, not
        only once it ends (Phase C correction brief, item 4: "allow payment during the term")."""
        term = _make_term(self.school, code="t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=50))
        _census(self.school, 20, at=timezone.now(), key="mid-term")

        result = run_subscription_cycle(self.subscription.id)

        self.assertEqual(result["result"], "invoice_issued")
        self.assertEqual(result["schoolCount"], 1)
        invoice = BillingInvoice.objects.get(school=self.school)
        self.assertEqual(invoice.amount_due_minor, 4_000_000)  # 20 students: the 1-100 bracket
        self.assertEqual(invoice.status, InvoiceStatus.OPEN)
        self.assertEqual((invoice.period_start, invoice.period_end), _term_bounds(term))

    def test_a_school_is_never_charged_twice_for_the_same_term(self):
        _make_term(self.school, code="t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=50))
        _census(self.school, 20, at=timezone.now(), key="first")
        run_subscription_cycle(self.subscription.id)
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

        # A fresh census, still inside the same term, re-run - never a second invoice for the
        # same (subscription, school, period).
        _census(self.school, 25, at=timezone.now(), key="second")
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "scheduled")
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

    def test_once_the_next_term_begins_its_own_separate_invoice_can_be_issued(self):
        first_term_end = self.today + timedelta(days=1)
        first_term = _make_term(self.school, code="t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=first_term_end)
        second_term = _make_term(
            self.school, code="t2", sequence=2,
            starts_on=first_term_end + timedelta(days=1), ends_on=first_term_end + timedelta(days=60),
            session=first_term.session,
        )
        _census(self.school, 20, at=timezone.now(), key="term1")
        run_subscription_cycle(self.subscription.id)  # bills term 1
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)

        into_term_2 = timezone.make_aware(datetime.combine(second_term.starts_on + timedelta(days=5), time.min))
        _census(self.school, 30, at=into_term_2, key="term2")
        result = run_subscription_cycle(self.subscription.id, now=into_term_2)

        self.assertEqual(result["result"], "invoice_issued")
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 2)
        start, end = _term_bounds(second_term)
        term2_invoice = BillingInvoice.objects.get(subscription=self.subscription, school=self.school, period_start=start, period_end=end)
        self.assertEqual(term2_invoice.amount_due_minor, 4_000_000)  # 30 students: still the 1-100 bracket

    def test_no_active_term_right_now_waits_rather_than_inventing_one(self):
        first_term_end = self.today + timedelta(days=1)
        _make_term(self.school, code="t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=first_term_end)
        _census(self.school, 20, at=timezone.now(), key="term1")
        run_subscription_cycle(self.subscription.id)  # bills term 1; no term 2 is configured

        later = timezone.make_aware(datetime.combine(first_term_end + timedelta(days=5), time.min))
        result = run_subscription_cycle(self.subscription.id, now=later)

        self.assertEqual(result["result"], "scheduled")
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "no_active_term"}])
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription).count(), 1)


class MultiSchoolTermBillingTests(BillingTestCase):
    """Previously: a multi-school organization's TERM billing was refused outright
    (single_active_school()). Fixed under the Phase C correction brief, item 3: each school is
    now billed from its own calendar, independently of its siblings."""

    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        self.today = timezone.localdate()
        self.school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )

    def test_two_schools_with_different_calendars_are_each_billed_from_their_own_term(self):
        _make_term(self.school, code="a-t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=50))
        _make_term(self.school_b, code="b-t1", sequence=1, starts_on=self.today - timedelta(days=40), ends_on=self.today + timedelta(days=20))
        _census(self.school, 20, at=timezone.now(), key="a")
        _census(self.school_b, 450, at=timezone.now(), key="b")

        result = run_subscription_cycle(self.subscription.id)

        self.assertEqual(result["result"], "invoice_issued")
        self.assertEqual(result["schoolCount"], 2)
        invoice_a = BillingInvoice.objects.get(school=self.school)
        invoice_b = BillingInvoice.objects.get(school=self.school_b)
        self.assertEqual(invoice_a.amount_due_minor, 4_000_000)   # 20 students: 1-100 bracket
        self.assertEqual(invoice_b.amount_due_minor, 12_000_000)  # 450 students: its own bracket
        self.assertNotEqual((invoice_a.period_start, invoice_a.period_end), (invoice_b.period_start, invoice_b.period_end))

    def test_one_schools_missing_calendar_never_blocks_a_siblings_own_invoice(self):
        # Only school A has a term configured at all; school B has none.
        _make_term(self.school, code="a-t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=50))
        _census(self.school, 20, at=timezone.now(), key="a")

        result = run_subscription_cycle(self.subscription.id)

        self.assertEqual(result["result"], "invoice_issued")
        self.assertEqual(result["schoolCount"], 1)
        self.assertTrue(BillingInvoice.objects.filter(school=self.school).exists())
        self.assertFalse(BillingInvoice.objects.filter(school=self.school_b).exists())
        self.assertIn({"schoolId": str(self.school_b.id), "reason": "no_active_term"}, result["skipped"])

    def test_one_schools_unpaid_invoice_never_blocks_a_siblings_next_terms_invoice(self):
        """The central correctness fix (Phase C correction brief, item 3): school B's own
        unpaid invoice must never prevent school A from getting its own next term's invoice,
        even though they share one organization/subscription."""
        first_term_end = self.today + timedelta(days=1)
        session_a = AcademicSession.objects.create(school=self.school, code="session-a", name="Session A", starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=200))
        term_a1 = AcademicTerm.objects.create(session=session_a, code="a-t1", name="Term 1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=first_term_end)
        term_a2 = AcademicTerm.objects.create(session=session_a, code="a-t2", name="Term 2", sequence=2, starts_on=first_term_end + timedelta(days=1), ends_on=first_term_end + timedelta(days=60))
        # School B's own long-running term - left unpaid for the whole test on purpose.
        _make_term(self.school_b, code="b-t1", sequence=1, starts_on=self.today - timedelta(days=10), ends_on=self.today + timedelta(days=200))

        _census(self.school, 20, at=timezone.now(), key="a-term1")
        _census(self.school_b, 450, at=timezone.now(), key="b")
        run_subscription_cycle(self.subscription.id)  # bills school A's term 1 and school B's own term
        self.assertEqual(BillingInvoice.objects.filter(school=self.school_b, status=InvoiceStatus.OPEN).count(), 1)

        into_term_2 = timezone.make_aware(datetime.combine(term_a2.starts_on + timedelta(days=5), time.min))
        _census(self.school, 30, at=into_term_2, key="a-term2")
        result = run_subscription_cycle(self.subscription.id, now=into_term_2)

        self.assertEqual(result["result"], "invoice_issued")
        start, end = _term_bounds(term_a2)
        self.assertTrue(BillingInvoice.objects.filter(school=self.school, period_start=start, period_end=end).exists())
        # School B's own earlier debt is untouched - never retried, never duplicated.
        self.assertEqual(BillingInvoice.objects.filter(school=self.school_b).count(), 1)
