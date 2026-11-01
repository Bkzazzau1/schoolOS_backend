"""The school-scoped entitlement API (Phase D, Task 1/2): a secure, read-only view of one
school's own subscription and premium-feature availability - never the organization-wide
accessMode, never a sibling school's payment. Any active member of the school may read
entitlement availability and why; sensitive commercial detail (invoice id/number, due date,
exact amounts) is included only for someone with organization-level billing authority."""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.billing.models import BillingInvoice, BillingInvoiceLine, InvoiceStatus, UsageSnapshot
from apps.billing.premium_access import CBT_EXAM_PUBLISH, REPORT_CARD_GENERATION
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import Membership, Role, School, SchoolType

from .base import BillingTestCase, verified_user

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class SchoolEntitlementViewTestCase(BillingTestCase):
    def entitlement_url(self, school):
        return f"schools/{school.id}/billing/entitlements/"

    def teacher(self, school=None):
        user = User.objects.create_user(f"teacher-{uuid.uuid4().hex[:8]}@example.ng", PASSWORD)
        return Membership.objects.create(user=user, school=school or self.school, role=Role.TEACHER)

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

    def _current_period(self):
        return timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=89)


class AccessControlTests(SchoolEntitlementViewTestCase):
    def test_an_unauthenticated_request_is_refused(self):
        response = self.client.get(self.url(self.entitlement_url(self.school)))
        self.assertEqual(response.status_code, 401)

    def test_a_person_with_no_membership_at_this_school_is_refused(self):
        stranger = verified_user("stranger-entitlement@example.ng")
        response = self.get(self.entitlement_url(self.school), who=stranger)
        self.assertEqual(response.status_code, 403)

    def test_a_teacher_membership_can_read_entitlement_status(self):
        teacher = self.teacher()
        response = self.get(self.entitlement_url(self.school), who=teacher.user)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["viewerRole"], Role.TEACHER)

    def test_the_proprietor_membership_can_read_entitlement_status(self):
        response = self.get(self.entitlement_url(self.school), who=self.owner)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["viewerRole"], Role.PROPRIETOR)

    def test_cross_school_access_is_refused(self):
        other_school, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Other Campus",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        other_teacher = self.teacher(other_school)
        response = self.get(self.entitlement_url(self.school), who=other_teacher.user)
        self.assertEqual(response.status_code, 403)

    def test_cross_organization_access_is_refused(self):
        other_owner = verified_user("other-org-owner@example.ng")
        other_org, _ = create_organization(actor=other_owner, name="Unrelated Org")
        other_school, _ = provision_school(
            actor=other_owner, organization_id=other_org.id, name="Unrelated School",
            school_type=SchoolType.SECONDARY, location="Lagos",
        )
        other_teacher = self.teacher(other_school)
        response = self.get(self.entitlement_url(self.school), who=other_teacher.user)
        self.assertEqual(response.status_code, 403)
        # And the reverse: this organization's own owner has no membership at the
        # unrelated school, so cannot read its entitlement status either.
        response = self.get(self.entitlement_url(other_school), who=self.owner)
        self.assertEqual(response.status_code, 403)


class SensitiveDetailGatingTests(SchoolEntitlementViewTestCase):
    def test_a_teacher_never_sees_billing_detail_even_when_an_invoice_exists(self):
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        teacher = self.teacher()
        payload = self.get(self.entitlement_url(self.school), who=teacher.user).json()
        self.assertFalse(payload["canManageBilling"])
        self.assertIsNone(payload["billing"])
        # But the non-sensitive facts teachers need are still present.
        self.assertEqual(payload["paymentStatus"], "unpaid")
        self.assertIn(CBT_EXAM_PUBLISH, payload["entitlements"])

    def test_a_billing_administrator_sees_full_billing_detail(self):
        start, end = self._current_period()
        invoice = self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        admin = self.billing_administrator()
        payload = self.get(self.entitlement_url(self.school), who=admin).json()
        self.assertTrue(payload["canManageBilling"])
        self.assertIsNotNone(payload["billing"])
        self.assertEqual(payload["billing"]["invoiceId"], str(invoice.id))
        self.assertEqual(payload["billing"]["amountDueMinor"], invoice.amount_due_minor)
        self.assertTrue(payload["billing"]["payable"])

    def test_the_proprietor_owner_role_also_manages_billing(self):
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertTrue(payload["canManageBilling"])
        self.assertIsNotNone(payload["billing"])
        self.assertEqual(payload["billing"]["status"], InvoiceStatus.PAID)

    def test_billing_detail_surfaces_the_invoices_own_latest_payment_attempt(self):
        """Stage 1.5, Task 2: a Flutter client reopening the subscription screen must be able
        to recover a pending checkout from the server's own record, never only its own
        in-memory state - and never by starting a second attempt just to check on the first."""
        from apps.billing.models import PaymentAttempt, PaymentAttemptStatus

        start, end = self._current_period()
        invoice = self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        older = PaymentAttempt.objects.create(
            invoice=invoice, provider="paystack", reference=f"ref-older-{uuid.uuid4().hex[:8]}",
            amount_minor=invoice.amount_due_minor, currency="NGN", status=PaymentAttemptStatus.FAILED,
        )
        older.created_at = timezone.now() - timedelta(minutes=10)
        older.save(update_fields=["created_at"])
        newer = PaymentAttempt.objects.create(
            invoice=invoice, provider="paystack", reference=f"ref-newer-{uuid.uuid4().hex[:8]}",
            amount_minor=invoice.amount_due_minor, currency="NGN", status=PaymentAttemptStatus.PENDING,
            checkout_url="https://checkout.paystack.com/abc123",
        )
        admin = self.billing_administrator()
        payload = self.get(self.entitlement_url(self.school), who=admin).json()
        latest = payload["billing"]["latestPaymentAttempt"]
        self.assertEqual(latest["reference"], newer.reference)
        self.assertEqual(latest["status"], PaymentAttemptStatus.PENDING)
        self.assertEqual(latest["checkoutUrl"], newer.checkout_url)

    def test_billing_detail_has_no_payment_attempt_when_none_has_ever_been_made(self):
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        admin = self.billing_administrator()
        payload = self.get(self.entitlement_url(self.school), who=admin).json()
        self.assertIsNone(payload["billing"]["latestPaymentAttempt"])


class PaymentStatusAndEntitlementTests(SchoolEntitlementViewTestCase):
    def test_an_unpaid_school_shows_both_premium_capabilities_unavailable_with_a_reason(self):
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "no_invoice_yet")
        for code in (CBT_EXAM_PUBLISH, REPORT_CARD_GENERATION):
            self.assertFalse(payload["entitlements"][code]["available"])
            self.assertTrue(payload["entitlements"][code]["reason"])

    def test_a_paid_school_shows_both_premium_capabilities_available(self):
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "paid")
        for code in (CBT_EXAM_PUBLISH, REPORT_CARD_GENERATION):
            self.assertTrue(payload["entitlements"][code]["available"])
            self.assertEqual(payload["entitlements"][code]["reason"], "")

    def test_an_open_unpaid_invoice_for_the_current_period_is_reported_as_unpaid(self):
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.OPEN)
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "unpaid")
        self.assertFalse(payload["entitlements"][CBT_EXAM_PUBLISH]["available"])

    def test_premium_enforcement_rollout_flag_is_reported_accurately(self):
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertFalse(payload["premiumEnforcementEnabled"])

    def test_plan_entitlement_and_school_payment_are_reported_as_separate_signals(self):
        """Task 1's own requirement: distinguish plan entitlement, payment eligibility and
        actual enforcement - never one collapsed boolean."""
        # Plan allows it, but this school has not paid: planEnabled true, schoolPaid false.
        unpaid = self.get(self.entitlement_url(self.school), who=self.owner).json()
        entry = unpaid["entitlements"][CBT_EXAM_PUBLISH]
        self.assertTrue(entry["planEnabled"])
        self.assertFalse(entry["schoolPaid"])
        self.assertFalse(entry["available"])

        # The plan itself does not include the feature at all - even once paid, it stays
        # unavailable, and planEnabled (not schoolPaid) is what reports why.
        self.give_entitlement(CBT_EXAM_PUBLISH, enabled=False)
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        disabled_plan = self.get(self.entitlement_url(self.school), who=self.owner).json()
        entry = disabled_plan["entitlements"][CBT_EXAM_PUBLISH]
        self.assertFalse(entry["planEnabled"])
        self.assertTrue(entry["schoolPaid"])
        self.assertFalse(entry["available"])


class MultiSchoolIndependenceTests(SchoolEntitlementViewTestCase):
    def test_mixed_paid_and_unpaid_schools_in_one_organization_are_fully_independent(self):
        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        self._invoice(school_b, period_start=start, period_end=end, status=InvoiceStatus.OPEN)

        payload_a = self.get(self.entitlement_url(self.school), who=self.owner).json()
        payload_b = self.get(self.entitlement_url(school_b), who=self.owner).json()
        self.assertEqual(payload_a["paymentStatus"], "paid")
        self.assertEqual(payload_b["paymentStatus"], "unpaid")
        self.assertTrue(payload_a["entitlements"][CBT_EXAM_PUBLISH]["available"])
        self.assertFalse(payload_b["entitlements"][CBT_EXAM_PUBLISH]["available"])

    def test_a_restricted_organization_never_hides_a_schools_own_paid_status(self):
        from apps.billing.models import SubscriptionStatus

        start, end = self._current_period()
        self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID)
        self.subscription.status = SubscriptionStatus.RESTRICTED
        self.subscription.save(update_fields=["status"])
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "paid")
        self.assertTrue(payload["entitlements"][CBT_EXAM_PUBLISH]["available"])


class LegacySchoolTests(SchoolEntitlementViewTestCase):
    def test_a_school_with_no_organization_reads_as_not_applicable_and_fully_available(self):
        standalone = School.objects.create(name="Independent School", slug=f"independent-{uuid.uuid4().hex[:8]}")
        user = User.objects.create_user(f"owner-{uuid.uuid4().hex[:8]}@example.ng", PASSWORD)
        membership = Membership.objects.create(user=user, school=standalone, role=Role.PROPRIETOR)
        payload = self.get(self.entitlement_url(standalone), who=user).json()
        self.assertIsNone(payload["organizationId"])
        self.assertEqual(payload["paymentStatus"], "not_applicable")
        self.assertFalse(payload["canManageBilling"])
        self.assertIsNone(payload["billing"])
        self.assertTrue(payload["entitlements"][CBT_EXAM_PUBLISH]["available"])
        self.assertTrue(payload["entitlements"][REPORT_CARD_GENERATION]["available"])


class HistoricalInvoiceCompatibilityTests(SchoolEntitlementViewTestCase):
    def test_a_paid_historical_combined_invoice_still_reports_this_school_as_paid(self):
        start, end = self._current_period()
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription,
            period_start=start, period_end=end, active_school_count=1,
            billable_student_count=50, source="test",
        )
        historical = BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=None,
            usage_snapshot=snapshot, number=f"TEST-HIST-{uuid.uuid4().hex[:8]}", currency="NGN",
            base_amount_minor=4_000_000, amount_due_minor=4_000_000, amount_paid_minor=4_000_000,
            status=InvoiceStatus.PAID, period_start=start, period_end=end,
        )
        BillingInvoiceLine.objects.create(
            invoice=historical, school=self.school, billable_student_count=50, currency="NGN",
            fixed_fee_minor=4_000_000, pricing_source="bracket", bracket_min_students=1,
            bracket_max_students=100, gross_fee_minor=4_000_000,
            period_start=start, period_end=end,
        )
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "paid")
        self.assertTrue(payload["entitlements"][CBT_EXAM_PUBLISH]["available"])

        admin = self.billing_administrator()
        payload_admin = self.get(self.entitlement_url(self.school), who=admin).json()
        self.assertEqual(payload_admin["billing"]["invoiceId"], str(historical.id))


class AcademicCalendarTests(SchoolEntitlementViewTestCase):
    def test_the_schools_own_current_term_and_session_are_reported(self):
        from apps.academics.models import AcademicSession, AcademicTerm

        today = timezone.localdate()
        session = AcademicSession.objects.create(
            school=self.school, code="2026-session", name="2026/2027",
            starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=300),
        )
        term = AcademicTerm.objects.create(
            session=session, code="t1", name="Term 1", sequence=1,
            starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50),
        )
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["academicTerm"]["id"], str(term.id))
        self.assertEqual(payload["academicSession"]["id"], str(session.id))

    def test_two_schools_on_different_calendars_each_report_their_own_term(self):
        from apps.academics.models import AcademicSession, AcademicTerm

        school_b, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Campus B",
            school_type=SchoolType.PRIMARY, location="Kano",
        )
        today = timezone.localdate()
        session_a = AcademicSession.objects.create(school=self.school, code="a-session", name="A", starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=200))
        term_a = AcademicTerm.objects.create(session=session_a, code="a-t1", name="A Term 1", sequence=1, starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50))
        session_b = AcademicSession.objects.create(school=school_b, code="b-session", name="B", starts_on=today - timedelta(days=60), ends_on=today + timedelta(days=150))
        term_b = AcademicTerm.objects.create(session=session_b, code="b-t1", name="B Term 1", sequence=1, starts_on=today - timedelta(days=60), ends_on=today + timedelta(days=10))

        payload_a = self.get(self.entitlement_url(self.school), who=self.owner).json()
        payload_b = self.get(self.entitlement_url(school_b), who=self.owner).json()
        self.assertEqual(payload_a["academicTerm"]["id"], str(term_a.id))
        self.assertEqual(payload_b["academicTerm"]["id"], str(term_b.id))

    def test_no_configured_term_reports_null_rather_than_inventing_one(self):
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertIsNone(payload["academicTerm"])
        self.assertIsNone(payload["academicSession"])


class CensusDisplayTests(SchoolEntitlementViewTestCase):
    """Census status surfaced to the school's own authorized viewers (Phase D Stage 3, Task
    1 requirement 4) - built from apps.billing.census, the same model apps.console reads."""

    def _term(self):
        from apps.academics.models import AcademicSession, AcademicTerm

        today = timezone.localdate()
        session = AcademicSession.objects.create(
            school=self.school, code="2026-session", name="2026/2027",
            starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=300),
        )
        return AcademicTerm.objects.create(
            session=session, code="t1", name="Term 1", sequence=1,
            starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50),
        )

    def test_with_no_cutoff_policy_and_no_census_fired_yet_reports_null(self):
        self._term()
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertIsNone(payload["census"])

    def test_a_pricing_exception_is_reported_as_needs_review(self):
        from apps.billing.census import flag_pricing_exception
        from apps.billing.academic_periods import term_bounds

        term = self._term()
        start, end = term_bounds(term)
        flag_pricing_exception(self.school, period_start=start, period_end=end, reason="Zero billable students.")

        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["census"]["status"], "needs_review")
        self.assertEqual(payload["census"]["exceptionReason"], "Zero billable students.")

    def test_a_frozen_census_reports_its_count_and_timestamps(self):
        from apps.billing.census import freeze_school_census
        from apps.billing.academic_periods import term_bounds
        from apps.billing.cycle import publish_school_billing_meter

        term = self._term()
        start, end = term_bounds(term)
        # Published after the real zero-student bootstrap meter every provisioned school
        # already has, so it is genuinely the latest authoritative observation as of the
        # cutoff below, not shadowed by that older zero count.
        publish_school_billing_meter(
            school=self.school, billable_student_count=80, source="canonical_student_roster",
            source_version="test:1", measured_at=timezone.now(),
        )
        cutoff_at = timezone.now() + timedelta(minutes=1)
        freeze_school_census(
            self.school, period_start=start, period_end=end, cutoff_at=cutoff_at,
            now=timezone.now() + timedelta(minutes=2),
        )

        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["census"]["status"], "frozen")
        self.assertEqual(payload["census"]["billableStudentCount"], 80)
        self.assertIsNotNone(payload["census"]["frozenAt"])
        self.assertFalse(payload["census"]["isEarly"])

    def test_census_is_never_reported_for_monthly_billing_with_no_per_school_term(self):
        self.set_plan(automatic=False)  # default MONTHLY interval, no AcademicTerm created
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertIsNone(payload["census"])


class EnrollmentPricingTests(SchoolEntitlementViewTestCase):
    def test_a_real_published_census_resolves_the_correct_bracket(self):
        self.add_billable_students(self.school, 20)
        self.publish_meter()
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["enrollment"]["billableStudentCount"], 20)
        self.assertEqual(payload["enrollment"]["bracketMinStudents"], 1)
        self.assertEqual(payload["enrollment"]["bracketMaxStudents"], 100)
        self.assertEqual(payload["enrollment"]["pricingSource"], "bracket")
        self.assertIsNone(payload["enrollment"]["undefinedReason"])

    def test_zero_enrollment_reports_a_reason_never_a_fabricated_amount(self):
        self.publish_meter()  # no students added - the authoritative count is 0
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["enrollment"]["billableStudentCount"], 0)
        self.assertIsNone(payload["enrollment"]["bracketMinStudents"])
        self.assertIsNotNone(payload["enrollment"]["undefinedReason"])

    def test_no_census_published_yet_reports_no_enrollment_block_at_all(self):
        standalone_org_school, _ = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="Brand New Campus",
            school_type=SchoolType.SECONDARY, location="Abuja",
        )
        # provision_school bootstraps a zero-count meter automatically - delete it to prove
        # the genuinely-never-measured case too (belt and suspenders with the zero-enrollment
        # case above, which covers the bootstrap-meter-exists path).
        from apps.billing.models import SchoolBillingMeterSnapshot

        SchoolBillingMeterSnapshot.objects.filter(school=standalone_org_school).delete()
        payload = self.get(self.entitlement_url(standalone_org_school), who=self.owner).json()
        self.assertIsNone(payload["enrollment"])

    def test_an_enterprise_agreement_is_reported_as_its_own_pricing_source(self):
        from apps.billing.pricing import create_enterprise_agreement

        create_enterprise_agreement(
            school=self.school, fixed_fee_minor=99_000_000, effective_from=timezone.now() - timedelta(days=1),
            contract_reference="TEST-CONTRACT",
        )
        self.add_billable_students(self.school, 5_000)  # far above every standard bracket
        self.publish_meter()
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["enrollment"]["pricingSource"], "enterprise")
        self.assertIsNone(payload["enrollment"]["undefinedReason"])


class SubscriptionRenewalTests(SchoolEntitlementViewTestCase):
    def test_an_earlier_terms_payment_does_not_report_a_later_unpaid_term_as_paid(self):
        term1_start = timezone.now() - timedelta(days=100)
        term1_end = timezone.now() - timedelta(days=10)
        term2_start = term1_end
        term2_end = timezone.now() + timedelta(days=80)
        self._invoice(self.school, period_start=term1_start, period_end=term1_end, status=InvoiceStatus.PAID)
        self._invoice(self.school, period_start=term2_start, period_end=term2_end, status=InvoiceStatus.OPEN)
        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "unpaid")

    def test_renewal_reports_paid_once_the_new_terms_invoice_is_paid(self):
        term1_start = timezone.now() - timedelta(days=100)
        term1_end = timezone.now() - timedelta(days=10)
        term2_start = term1_end
        term2_end = timezone.now() + timedelta(days=80)
        self._invoice(self.school, period_start=term1_start, period_end=term1_end, status=InvoiceStatus.PAID)
        term2_invoice = self._invoice(self.school, period_start=term2_start, period_end=term2_end, status=InvoiceStatus.OPEN)
        term2_invoice.status = InvoiceStatus.PAID
        term2_invoice.amount_paid_minor = term2_invoice.amount_due_minor
        term2_invoice.save(update_fields=["status", "amount_paid_minor"])

        payload = self.get(self.entitlement_url(self.school), who=self.owner).json()
        self.assertEqual(payload["paymentStatus"], "paid")
        self.assertTrue(payload["entitlements"][CBT_EXAM_PUBLISH]["available"])

    def test_an_annual_invoices_currency_and_period_are_reported_to_billing_authority(self):
        start = timezone.now() - timedelta(days=30)
        end = timezone.now() + timedelta(days=330)
        invoice = self._invoice(self.school, period_start=start, period_end=end, status=InvoiceStatus.PAID, fee_minor=36_000_000)
        admin = self.billing_administrator()
        payload = self.get(self.entitlement_url(self.school), who=admin).json()
        self.assertEqual(payload["billing"]["currency"], "NGN")
        self.assertEqual(payload["billing"]["amountPaidMinor"], invoice.amount_paid_minor)
        self.assertEqual(payload["currentPeriod"]["start"], invoice.period_start.isoformat())
        self.assertEqual(payload["currentPeriod"]["end"], invoice.period_end.isoformat())
