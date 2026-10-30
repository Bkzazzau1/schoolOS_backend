"""The reusable premium-entitlement foundation (Phase C freemium policy): a premium feature
needs BOTH the organization's commercial plan to have the feature enabled at all, and this
specific school's own current billing period to be paid - independent of the organization's
overall lifecycle status, and independent of any sibling school's own payment. Nothing here
is wired into any live feature yet - these tests prove the foundation itself across paid,
unpaid, restricted, expired, renewed and mixed-paid-and-unpaid multi-school states."""

import uuid
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.organizations.services import provision_school
from apps.schools.models import School, SchoolType

from ..models import BillingInvoice, BillingInvoiceLine, InvoiceStatus, SubscriptionStatus, UsageSnapshot
from ..premium_access import CBT_EXAM_PUBLISH, require_premium_entitlement, school_premium_access
from ..services import admin_set_status
from .base import BillingTestCase, verified_user


class PremiumAccessTestCase(BillingTestCase):
    def _invoice(self, school, *, period_start, period_end, status, fee_minor=4_000_000):
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=period_start, period_end=period_end,
            active_school_count=1, billable_student_count=50, source="test",
        )
        invoice = BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=school, usage_snapshot=snapshot,
            number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=fee_minor, amount_due_minor=fee_minor,
            amount_paid_minor=fee_minor if status == InvoiceStatus.PAID else 0,
            status=status, period_start=period_start, period_end=period_end,
        )
        BillingInvoiceLine.objects.create(
            invoice=invoice, school=school, billable_student_count=50, currency="NGN",
            fixed_fee_minor=fee_minor, pricing_source="bracket", bracket_min_students=1,
            bracket_max_students=100, gross_fee_minor=fee_minor,
            period_start=period_start, period_end=period_end,
        )
        return invoice

    def _midpoint(self, start, end):
        return start + (end - start) / 2


class SchoolPremiumAccessTests(PremiumAccessTestCase):
    def test_no_invoice_at_all_is_locked(self):
        self.assertFalse(school_premium_access(self.school))

    def test_an_open_unpaid_invoice_for_the_current_period_is_locked(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        self.assertFalse(school_premium_access(self.school, as_of=self._midpoint(start, end)))

    def test_a_paid_invoice_for_the_current_period_is_unlocked(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        self.assertTrue(school_premium_access(self.school, as_of=self._midpoint(start, end)))

    def test_a_paid_invoice_never_unlocks_a_different_schools_period(self):
        other_owner = verified_user("other-premium@example.ng")
        from apps.organizations.services import create_organization

        other_org, _ = create_organization(actor=other_owner, name="Unrelated Premium Org")
        other_school, _ = provision_school(actor=other_owner, organization_id=other_org.id, name="Unrelated School", school_type=SchoolType.SECONDARY, location="Lagos")
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        self.assertFalse(school_premium_access(other_school, as_of=self._midpoint(start, end)))

    def test_mixed_paid_and_unpaid_schools_in_one_organization_are_fully_independent(self):
        school_b, _ = provision_school(actor=self.owner, organization_id=self.organization.id, name="Campus B", school_type=SchoolType.PRIMARY, location="Kano")
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID, fee_minor=4_000_000)
        self._invoice(school_b, period_start=start, period_end=end, status=InvoiceStatus.OPEN, fee_minor=12_000_000)

        as_of = self._midpoint(start, end)
        self.assertTrue(school_premium_access(self.school, as_of=as_of))
        self.assertFalse(school_premium_access(school_b, as_of=as_of))

        # Paying school B's own invoice afterwards never touches school A's, and vice versa.
        school_b_invoice = BillingInvoice.objects.get(school=school_b)
        school_b_invoice.status = InvoiceStatus.PAID
        school_b_invoice.amount_paid_minor = school_b_invoice.amount_due_minor
        school_b_invoice.save(update_fields=["status", "amount_paid_minor"])
        self.assertTrue(school_premium_access(school_b, as_of=as_of))
        self.assertTrue(school_premium_access(self.school, as_of=as_of))

    def test_an_earlier_terms_payment_does_not_unlock_a_later_unpaid_term(self):
        term1_start = timezone.now() - timedelta(days=100)
        term1_end = timezone.now() - timedelta(days=10)
        term2_start = term1_end
        term2_end = timezone.now() + timedelta(days=80)
        self._invoice(self.school, period_start=term1_start, period_end=term1_end, status=InvoiceStatus.PAID)
        self._invoice(self.school, period_start=term2_start, period_end=term2_end, status=InvoiceStatus.OPEN)

        self.assertTrue(school_premium_access(self.school, as_of=self._midpoint(term1_start, term1_end)))
        self.assertFalse(school_premium_access(self.school, as_of=self._midpoint(term2_start, term2_end)))

    def test_renewal_unlocks_once_the_new_terms_invoice_is_paid(self):
        term1_start = timezone.now() - timedelta(days=100)
        term1_end = timezone.now() - timedelta(days=10)
        term2_start = term1_end
        term2_end = timezone.now() + timedelta(days=80)
        self._invoice(self.school, period_start=term1_start, period_end=term1_end, status=InvoiceStatus.PAID)
        term2_invoice = self._invoice(self.school, period_start=term2_start, period_end=term2_end, status=InvoiceStatus.OPEN)

        term2_invoice.status = InvoiceStatus.PAID
        term2_invoice.amount_paid_minor = term2_invoice.amount_due_minor
        term2_invoice.save(update_fields=["status", "amount_paid_minor"])

        self.assertTrue(school_premium_access(self.school, as_of=self._midpoint(term2_start, term2_end)))


class RequirePremiumEntitlementTests(PremiumAccessTestCase):
    def test_a_school_with_no_organization_is_never_blocked(self):
        standalone = School.objects.create(name="Independent School", slug="independent-premium-test")
        require_premium_entitlement(standalone, code=CBT_EXAM_PUBLISH)  # does not raise

    def test_a_restricted_organization_never_blocks_a_schools_own_paid_premium_access(self):
        """Independent per-school billing means the organization's overall lifecycle status -
        which can be dragged down by a sibling school's unpaid invoice - must never block a
        *different*, already-paid school's premium access. This is the brief's own explicit
        "do not rely solely on the organization-wide subscription status" requirement."""
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        operator = verified_user("ops-premium@skulgen.app")
        admin_set_status(self.subscription, SubscriptionStatus.RESTRICTED, actor_user=operator, source="test")
        require_premium_entitlement(self.school, code=CBT_EXAM_PUBLISH)  # does not raise

    def test_full_access_but_unpaid_this_term_is_refused_with_the_premium_specific_message(self):
        with self.assertRaises(PermissionDenied) as caught:
            require_premium_entitlement(self.school, code=CBT_EXAM_PUBLISH)
        self.assertIn("current term subscription", str(caught.exception))

    def test_full_access_and_paid_this_term_does_not_raise(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        require_premium_entitlement(self.school, code=CBT_EXAM_PUBLISH)  # does not raise

    def test_a_disabled_entitlement_blocks_even_when_the_school_is_paid(self):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        self.give_entitlement(CBT_EXAM_PUBLISH, enabled=False)
        with self.assertRaises(PermissionDenied):
            require_premium_entitlement(self.school, code=CBT_EXAM_PUBLISH)
