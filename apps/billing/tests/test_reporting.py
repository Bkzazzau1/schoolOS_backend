"""Platform-wide subscription reporting (Phase D, Stage 3, Task 8): every number read
directly from BillingInvoice/SchoolEnrollmentCensus/EnterpriseAgreement, never approximated,
never counting an open invoice as revenue, never touching a school's own fee-collection
ledger."""

import uuid
from datetime import timedelta

from django.utils import timezone

from apps.billing.census import flag_pricing_exception
from apps.billing.models import (
    BillingInvoice,
    InvoiceStatus,
    OrganizationSubscription,
    Plan,
    SubscriptionEvent,
    SubscriptionStatus,
    UsageSnapshot,
)
from apps.billing.pricing import create_enterprise_agreement
from apps.billing.reporting import (
    platform_subscription_report,
    revenue_by_plan,
    weekly_cancellations,
    weekly_confirmed_revenue,
)
from apps.organizations.models import Organization
from apps.organizations.services import provision_school
from apps.schools.models import School, SchoolType

from .base import BillingTestCase


class PlatformSubscriptionReportTests(BillingTestCase):
    def _invoice(self, school, *, status, amount_due_minor=4_000_000, amount_paid_minor=None, period_end=None):
        now = timezone.now()
        period_start = now - timedelta(days=1)
        period_end = period_end or (now + timedelta(days=89))
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=period_start, period_end=period_end,
            active_school_count=1, billable_student_count=50, source="test",
        )
        return BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=school,
            usage_snapshot=snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=amount_due_minor, amount_due_minor=amount_due_minor,
            amount_paid_minor=amount_paid_minor if amount_paid_minor is not None else (amount_due_minor if status == InvoiceStatus.PAID else 0),
            status=status, period_start=period_start, period_end=period_end,
        )

    def test_counts_schools_by_payment_status(self):
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        self._invoice(self.school, status=InvoiceStatus.PAID)
        self._invoice(school_b, status=InvoiceStatus.OPEN)

        report = platform_subscription_report()
        self.assertEqual(report["totalSubscribedSchools"], 2)
        self.assertEqual(report["schoolsWithActivePaidSubscriptions"], 1)
        self.assertEqual(report["schoolsAwaitingPayment"], 1)

    def test_a_legacy_school_with_no_organization_is_never_counted_as_subscribed(self):
        School.objects.create(name="Legacy Academy", slug=f"legacy-{uuid.uuid4().hex[:8]}", organization=None)
        report = platform_subscription_report()
        self.assertEqual(report["totalSubscribedSchools"], 1)  # only self.school, from BillingTestCase.setUp

    def test_revenue_counts_only_paid_invoices_never_an_open_one(self):
        self._invoice(self.school, status=InvoiceStatus.PAID, amount_due_minor=4_000_000)
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        self._invoice(school_b, status=InvoiceStatus.OPEN, amount_due_minor=99_999_999)

        report = platform_subscription_report()
        revenue = {row["currency"]: row["amountMinor"] for row in report["confirmedRevenueByCurrency"]}
        self.assertEqual(revenue["NGN"], 4_000_000)
        self.assertEqual(report["unpaidInvoiceCount"], 1)

    def test_a_school_expiring_within_the_window_is_counted_a_later_one_is_not(self):
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        self._invoice(self.school, status=InvoiceStatus.PAID, period_end=timezone.now() + timedelta(days=10))
        self._invoice(school_b, status=InvoiceStatus.PAID, period_end=timezone.now() + timedelta(days=200))

        report = platform_subscription_report(expiring_within_days=30)
        self.assertEqual(report["schoolsExpiringSoon"], 1)

    def test_a_school_on_an_active_enterprise_agreement_is_counted_an_expired_one_is_not(self):
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        create_enterprise_agreement(
            school=self.school, fixed_fee_minor=40_000_000,
            effective_from=timezone.now() - timedelta(days=10), contract_reference="ACTIVE-1",
        )
        create_enterprise_agreement(
            school=school_b, fixed_fee_minor=40_000_000, contract_reference="EXPIRED-1",
            effective_from=timezone.now() - timedelta(days=100),
            effective_until=timezone.now() - timedelta(days=1),
        )

        report = platform_subscription_report()
        self.assertEqual(report["schoolsOnEnterpriseAgreements"], 1)

    def test_schools_needing_census_review_are_counted_once_each(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        flag_pricing_exception(self.school, period_start=start, period_end=end, reason="Zero billable students.")
        flag_pricing_exception(self.school, period_start=start, period_end=end, reason="Still zero on replay.")

        report = platform_subscription_report()
        self.assertEqual(report["schoolsAwaitingCensusReview"], 1)


class WeeklyConfirmedRevenueTests(BillingTestCase):
    def _invoice(self, *, status, amount_paid_minor=0, paid_at=None, currency="NGN", period_offset_days=0):
        now = timezone.now()
        period_start = now - timedelta(days=1) + timedelta(days=period_offset_days)
        period_end = now + timedelta(days=89) + timedelta(days=period_offset_days)
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=period_start, period_end=period_end,
            active_school_count=1, billable_student_count=50, source="test",
        )
        return BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=self.school,
            usage_snapshot=snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency=currency,
            base_amount_minor=4_000_000, amount_due_minor=4_000_000, amount_paid_minor=amount_paid_minor,
            status=status, period_start=period_start, period_end=period_end, paid_at=paid_at,
        )

    def test_revenue_is_bucketed_into_the_week_it_was_actually_paid(self):
        now = timezone.now()
        self._invoice(status=InvoiceStatus.PAID, amount_paid_minor=1_000_000, paid_at=now)
        self._invoice(
            status=InvoiceStatus.PAID, amount_paid_minor=2_000_000,
            paid_at=now - timedelta(weeks=3), period_offset_days=100,
        )

        report = weekly_confirmed_revenue()
        buckets = report["NGN"]
        self.assertEqual(len(buckets), 12)
        self.assertEqual(buckets[-1]["amount_minor"], 1_000_000)
        self.assertEqual(buckets[-4]["amount_minor"], 2_000_000)
        self.assertEqual(buckets[-4]["height_pct"], 100)
        self.assertEqual(buckets[-1]["height_pct"], 50)

    def test_an_open_invoice_is_never_counted_as_revenue(self):
        self._invoice(status=InvoiceStatus.OPEN, amount_paid_minor=0, paid_at=None)
        self._invoice(
            status=InvoiceStatus.PAID, amount_paid_minor=500_000,
            paid_at=timezone.now(), period_offset_days=100,
        )

        report = weekly_confirmed_revenue()
        self.assertEqual(sum(b["amount_minor"] for b in report["NGN"]), 500_000)

    def test_no_paid_invoices_ever_gives_an_empty_report(self):
        self.assertEqual(weekly_confirmed_revenue(), {})


class RevenueByPlanTests(BillingTestCase):
    def test_revenue_is_split_by_the_plan_that_actually_earned_it(self):
        now = timezone.now()
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=now - timedelta(days=1), period_end=now + timedelta(days=89),
            active_school_count=1, billable_student_count=50, source="test",
        )
        BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=self.school,
            usage_snapshot=snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=1_000_000, amount_due_minor=1_000_000, amount_paid_minor=1_000_000,
            status=InvoiceStatus.PAID, paid_at=now,
        )

        other_org = Organization.objects.create(name="Other Group", slug=f"other-{uuid.uuid4().hex[:8]}")
        other_plan = Plan.objects.create(code=f"premium-{uuid.uuid4().hex[:8]}", name="Premium")
        other_subscription = OrganizationSubscription.objects.create(
            organization=other_org, plan=other_plan, status=SubscriptionStatus.ACTIVE,
        )
        other_snapshot = UsageSnapshot.objects.create(
            organization=other_org, subscription=other_subscription,
            period_start=now - timedelta(days=1), period_end=now + timedelta(days=89),
            active_school_count=1, billable_student_count=10, source="test",
        )
        BillingInvoice.objects.create(
            organization=other_org, subscription=other_subscription, school=None,
            usage_snapshot=other_snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=3_000_000, amount_due_minor=3_000_000, amount_paid_minor=3_000_000,
            status=InvoiceStatus.PAID, paid_at=now,
        )

        rows = revenue_by_plan()["NGN"]
        self.assertEqual([r["plan_name"] for r in rows], ["Premium", self.subscription.plan.name])
        self.assertEqual(rows[0]["amount_minor"], 3_000_000)
        self.assertEqual(rows[0]["width_pct"], 100)
        self.assertEqual(rows[1]["amount_minor"], 1_000_000)

    def test_no_paid_invoices_ever_gives_an_empty_report(self):
        self.assertEqual(revenue_by_plan(), {})


class WeeklyCancellationsTests(BillingTestCase):
    def test_cancellations_are_counted_in_the_week_the_event_actually_happened(self):
        now = timezone.now()
        SubscriptionEvent.objects.create(
            subscription=self.subscription, event="status_changed",
            from_status=SubscriptionStatus.ACTIVE, to_status=SubscriptionStatus.CANCELLED,
        )
        SubscriptionEvent.objects.filter(
            subscription=self.subscription, to_status=SubscriptionStatus.CANCELLED,
        ).update(at=now - timedelta(weeks=5))

        buckets = weekly_cancellations()
        self.assertEqual(len(buckets), 12)
        self.assertEqual(buckets[-6]["count"], 1)
        self.assertEqual(sum(b["count"] for b in buckets), 1)

    def test_a_non_cancellation_event_is_never_counted(self):
        SubscriptionEvent.objects.create(
            subscription=self.subscription, event="status_changed",
            from_status=SubscriptionStatus.ACTIVE, to_status=SubscriptionStatus.PAST_DUE,
        )
        self.assertEqual(sum(b["count"] for b in weekly_cancellations()), 0)
