"""Enrollment census policy (Phase D, Stage 3, Task 1): apps.billing.census and its wiring
into apps.billing.cycle.term_billing. Two behaviors, tested separately because only one of
them is commercially approved:

- Pricing-exception flagging (zero enrollment, or above every bracket with no enterprise
  agreement) is active unconditionally - no BillingCyclePolicy.census_cutoff_days needed.
- The deliberate cutoff freeze only ever changes anything once census_cutoff_days is set;
  with it left at the shipped default (None), every real cycle outcome here must be
  byte-for-byte identical to what apps.billing.tests.test_term_billing already proves.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm
from apps.billing.census import correct_school_census, current_census, flag_pricing_exception
from apps.billing.cycle import publish_school_billing_meter, run_subscription_cycle
from apps.billing.models import (
    BillingInterval,
    BillingInvoice,
    CensusStatus,
    InvoiceStatus,
    SchoolEnrollmentCensus,
)

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


def _term_bounds(term):
    start = timezone.make_aware(datetime.combine(term.starts_on, time.min))
    end = timezone.make_aware(datetime.combine(term.ends_on + timedelta(days=1), time.min))
    return start, end


def _census_meter(school, count, *, at, key):
    return publish_school_billing_meter(
        school=school, billable_student_count=count, source="canonical_student_roster",
        source_version=f"test:{key}", measured_at=at,
    )


class NoCutoffConfiguredTests(BillingTestCase):
    """With census_cutoff_days left at the shipped default (None), nothing about this module
    changes real invoicing - the one exception is pricing-exception flagging, which is
    deliberately unconditional (Task 6)."""

    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000)
        today = timezone.localdate()
        self.term = _make_term(self.school, code="t1", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))
        self.start, self.end = _term_bounds(self.term)

    def test_a_real_census_is_invoiced_exactly_as_before_with_no_census_row_created(self):
        _census_meter(self.school, 50, at=timezone.now(), key="a")
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["result"], "invoice_issued")
        self.assertIsNone(current_census(self.school, period_start=self.start, period_end=self.end))

    def test_the_zero_student_bootstrap_meter_is_flagged_for_review_even_with_no_cutoff_policy(self):
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "pricing_undefined"}])
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

        census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertIsNotNone(census)
        self.assertEqual(census.status, CensusStatus.NEEDS_REVIEW)
        self.assertEqual(census.billable_student_count, 0)
        self.assertTrue(census.exception_reason)

    def test_replaying_the_same_unresolved_exception_updates_rather_than_duplicates(self):
        run_subscription_cycle(self.subscription.id)
        run_subscription_cycle(self.subscription.id)
        self.assertEqual(
            SchoolEnrollmentCensus.objects.filter(school=self.school, period_start=self.start, period_end=self.end).count(),
            1,
        )

    def test_an_enrollment_above_every_bracket_with_no_agreement_is_also_flagged(self):
        _census_meter(self.school, 3_000, at=timezone.now(), key="huge")
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "pricing_undefined"}])
        census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(census.status, CensusStatus.NEEDS_REVIEW)
        self.assertEqual(census.billable_student_count, 3_000)


class CutoffConfiguredTests(BillingTestCase):
    """Only once census_cutoff_days is explicitly set does freezing/deferral change anything."""

    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000, census_cutoff_days=14)
        today = timezone.localdate()
        self.term = _make_term(self.school, code="t1", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))
        self.start, self.end = _term_bounds(self.term)

    def test_before_the_cutoff_the_cycle_waits_even_with_a_real_census_already_published(self):
        _census_meter(self.school, 50, at=timezone.now(), key="early")
        result = run_subscription_cycle(self.subscription.id)
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "awaiting_census_cutoff"}])
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

    def test_after_the_cutoff_the_census_freezes_and_the_school_is_invoiced_from_it(self):
        # Published after the real zero-student bootstrap meter every provisioned school
        # already has (set up before this test method runs), so it is genuinely the latest
        # authoritative observation, not shadowed by an older real count.
        _census_meter(self.school, 50, at=timezone.now(), key="on_time")
        past_cutoff = timezone.now() + timedelta(days=15)
        result = run_subscription_cycle(self.subscription.id, now=past_cutoff)
        self.assertEqual(result["result"], "invoice_issued")

        invoice = BillingInvoice.objects.get(subscription=self.subscription, school=self.school)
        self.assertEqual(invoice.billable_student_count, 50)

        census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(census.status, CensusStatus.FROZEN)
        self.assertEqual(census.billable_student_count, 50)
        self.assertEqual(census.invoice_id, invoice.id)

        # A second cycle run never re-freezes or re-prices the already-invoiced period -
        # "scheduled" here means "nothing new to issue," matching how an ordinary
        # already_invoiced term already behaves without any census cutoff involved.
        second = run_subscription_cycle(self.subscription.id, now=past_cutoff + timedelta(hours=1))
        self.assertEqual(second["result"], "scheduled")
        self.assertEqual(BillingInvoice.objects.filter(subscription=self.subscription, school=self.school).count(), 1)

    def test_a_census_still_reporting_zero_at_the_cutoff_needs_review_not_invention(self):
        past_cutoff = timezone.now() + timedelta(days=15)
        result = run_subscription_cycle(self.subscription.id, now=past_cutoff)
        self.assertEqual(result["skipped"], [{"schoolId": str(self.school.id), "reason": "census_needs_review"}])
        self.assertFalse(BillingInvoice.objects.filter(subscription=self.subscription).exists())

        census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(census.status, CensusStatus.NEEDS_REVIEW)

    def test_enrollment_published_after_the_cutoff_never_changes_the_frozen_count(self):
        _census_meter(self.school, 50, at=timezone.now(), key="before")
        past_cutoff = timezone.now() + timedelta(days=15)
        run_subscription_cycle(self.subscription.id, now=past_cutoff)

        # A late admission reported well after the cutoff must never retroactively reprice
        # the already-frozen, already-invoiced period.
        _census_meter(self.school, 90, at=past_cutoff + timedelta(days=1), key="late")
        census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(census.billable_student_count, 50)
        invoice = BillingInvoice.objects.get(subscription=self.subscription, school=self.school)
        self.assertEqual(invoice.billable_student_count, 50)


class CensusCorrectionTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.set_plan(interval=BillingInterval.TERM, base_amount_minor=1_000_000, census_cutoff_days=14)
        today = timezone.localdate()
        self.term = _make_term(self.school, code="t1", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))
        self.start, self.end = _term_bounds(self.term)

    def test_correcting_before_any_invoice_lets_the_next_cycle_run_use_the_corrected_count(self):
        _census_meter(self.school, 40, at=timezone.now(), key="wrong")
        past_cutoff = timezone.now() + timedelta(days=15)
        run_subscription_cycle(self.subscription.id, now=past_cutoff)
        wrong_census = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(wrong_census.billable_student_count, 40)
        self.assertIsNotNone(wrong_census.invoice_id)

        # A real correction, after the fact, still never rewrites the invoice already issued
        # from the erroneous count - that invoice is immutable.
        corrected = correct_school_census(
            wrong_census, actor=self.owner, reason="Roster export miscounted a withdrawn class.",
            billable_student_count=120,
        )
        wrong_census.refresh_from_db()
        self.assertEqual(wrong_census.status, CensusStatus.CORRECTED)
        self.assertEqual(wrong_census.billable_student_count, 40)  # untouched - the historical record
        invoice = BillingInvoice.objects.get(subscription=self.subscription, school=self.school)
        self.assertEqual(invoice.billable_student_count, 40)  # never silently rewritten

        self.assertEqual(corrected.status, CensusStatus.FROZEN)
        self.assertEqual(corrected.billable_student_count, 120)
        self.assertEqual(corrected.corrected_from_id, wrong_census.id)
        self.assertIsNone(corrected.invoice_id)  # the correction itself never issues a new invoice

    def test_a_correction_requires_a_reason(self):
        _census_meter(self.school, 40, at=timezone.now(), key="wrong")
        past_cutoff = timezone.now() + timedelta(days=15)
        run_subscription_cycle(self.subscription.id, now=past_cutoff)
        census = current_census(self.school, period_start=self.start, period_end=self.end)
        with self.assertRaises(ValueError):
            correct_school_census(census, actor=self.owner, reason="  ", billable_student_count=80)

    def test_correcting_a_needs_review_census_can_resolve_it_to_frozen(self):
        past_cutoff = timezone.now() + timedelta(days=15)
        run_subscription_cycle(self.subscription.id, now=past_cutoff)
        needs_review = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(needs_review.status, CensusStatus.NEEDS_REVIEW)

        corrected = correct_school_census(
            needs_review, actor=self.owner, reason="Manual headcount confirmed by the registrar.",
            billable_student_count=65,
        )
        self.assertEqual(corrected.status, CensusStatus.FROZEN)
        current = current_census(self.school, period_start=self.start, period_end=self.end)
        self.assertEqual(current.id, corrected.id)
