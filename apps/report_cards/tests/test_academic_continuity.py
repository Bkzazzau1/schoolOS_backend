"""A real, compiled report card and its real review/submit/release workflow - proving
academic continuity (Phase C correction brief, item 2.B): compiling (generating) a *new* batch
of report cards is the premium action; submitting and releasing an already-generated report
card is continuity, and must keep working no matter the school's subscription state, even with
PREMIUM_ENFORCEMENT_ENABLED on. Judged by apps.sync.access_tiers.ACADEMIC_CONTINUITY_ENTITY_TYPES
("academic_report_card"), never by apps.billing.premium_access. Built from real academic/
enrollment records - the same models apps.report_cards.services itself reads."""

import uuid
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm, EnrollmentAcademicContext
from apps.billing.models import BillingInvoice, InvoiceStatus, SubscriptionStatus, UsageSnapshot
from apps.billing.services import ensure_organization_subscription
from apps.organizations.models import Organization
from apps.report_cards.models import ReportCard, ReportCardState
from apps.schools.models import Membership, Role, School
from apps.students.models import EnrollmentStatus, Student, StudentEnrollment

User = get_user_model()
PASSWORD = "a-long-test-password-1"
PREMIUM_MESSAGE = "current term subscription"


class ReportCardContinuityTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-rc-{uuid.uuid4().hex[:8]}")
        self.school = School.objects.create(
            name="BrightGate Academy", slug=f"bg-rc-academy-{uuid.uuid4().hex[:8]}", organization=self.organization
        )
        self.subscription = ensure_organization_subscription(self.organization)

        today = timezone.localdate()
        self.session = AcademicSession.objects.create(
            school=self.school, code="2026-session", name="2026/2027",
            starts_on=today - timedelta(days=30), ends_on=today + timedelta(days=300),
        )
        self.term = AcademicTerm.objects.create(
            session=self.session, code="t1", name="Term 1", sequence=1,
            starts_on=today - timedelta(days=10), ends_on=today + timedelta(days=50),
        )
        # A non-Secondary class skips the Principal-review step, keeping this fixture to the
        # Administrator-only submit/release path that's the point of these tests.
        self.academic_class = AcademicClass.objects.create(
            school=self.school, code="P5A", name="Primary 5A", section="Primary", level_order=1,
        )

        admin_user = User.objects.create_user("admin@brightgate.ng", PASSWORD)
        self.administrator = Membership.objects.create(user=admin_user, school=self.school, role=Role.ADMINISTRATOR)

        self.student_record = Student.objects.create(
            school=self.school, student_code="STU-0001", admission_number="ADM-0001",
            first_name="Amina", surname="Bello",
        )
        enrollment = StudentEnrollment.objects.create(
            school=self.school, student=self.student_record, academic_section="Primary", class_name="Primary 5A",
            status=EnrollmentStatus.ACTIVE, is_billable=True, started_at=timezone.now() - timedelta(days=10),
        )
        EnrollmentAcademicContext.objects.create(
            enrollment=enrollment, session=self.session, academic_class=self.academic_class,
            entry_term=self.term, source="test",
        )

    # -- helpers -----------------------------------------------------------------------------

    def _pay_current_term(self):
        start = timezone.make_aware(datetime.combine(self.term.starts_on, time.min))
        end = timezone.make_aware(datetime.combine(self.term.ends_on + timedelta(days=1), time.min))
        snapshot = UsageSnapshot.objects.create(
            organization=self.organization, subscription=self.subscription, period_start=start, period_end=end,
            active_school_count=1, billable_student_count=1, source="test",
        )
        BillingInvoice.objects.create(
            organization=self.organization, subscription=self.subscription, school=self.school,
            usage_snapshot=snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=4_000_000, amount_due_minor=4_000_000, amount_paid_minor=4_000_000,
            status=InvoiceStatus.PAID, period_start=start, period_end=end,
        )

    def _make_restricted(self):
        self.subscription.status = SubscriptionStatus.RESTRICTED
        self.subscription.save(update_fields=["status"])

    def push(self, who, *, entity_type, entity_id, payload, operation="create"):
        self.client.force_authenticate(who.user)
        return self.client.post(
            "/api/v1/sync/push/",
            {
                "id": f"mut-{uuid.uuid4().hex}", "tenantId": str(self.school.id), "membershipId": str(who.id),
                "entityType": entity_type, "entityId": entity_id, "operation": operation, "payload": payload,
            },
            format="json",
        )

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def _batch_id(self):
        return f"{self.academic_class.id}:{self.term.id}"

    def compile_real_batch(self):
        """Compiles a real report-card batch the way the brief insists on: real class/term/
        enrollment records, with enforcement ON and the school paid - so the only thing left
        for the continuity tests to prove is that submit/release keep working afterward."""
        self._pay_current_term()
        batch_id = self._batch_id()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.administrator, entity_type="academic_report_card_batch", entity_id=batch_id,
                payload={"id": batch_id, "action": "compile", "classId": str(self.academic_class.id), "termId": str(self.term.id)},
            ))
        card = ReportCard.objects.get(school=self.school, student=self.student_record, term=self.term)
        return card.external_id

    # -- compiling (generating) is the premium gate ------------------------------------------

    def test_compiling_a_new_batch_is_blocked_while_unpaid_with_enforcement_on(self):
        batch_id = self._batch_id()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(
                self.administrator, entity_type="academic_report_card_batch", entity_id=batch_id,
                payload={"id": batch_id, "action": "compile", "classId": str(self.academic_class.id), "termId": str(self.term.id)},
            )
        self.assertEqual(response.status_code, 422, response.json())
        self.assertIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertFalse(ReportCard.objects.filter(school=self.school).exists())

    # -- continuity: review/submit/release of an already-compiled card must keep working ----

    def test_an_already_compiled_card_can_be_submitted_and_released_with_enforcement_on(self):
        report_card_id = self.compile_real_batch()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "submit"}, operation="update",
            ))
            result = self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "release"}, operation="update",
            ))
        self.assertEqual(result["disposition"], "accepted")
        self.assertEqual(ReportCard.objects.get(external_id=report_card_id).state, ReportCardState.RELEASED)

    def test_submit_and_release_survive_the_schools_subscription_turning_restricted(self):
        report_card_id = self.compile_real_batch()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "submit"}, operation="update",
            ))
            # The school's subscription changes after compiling - restricted, not merely unpaid.
            self._make_restricted()
            result = self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "release"}, operation="update",
            ))
        self.assertEqual(result["disposition"], "accepted")
        self.assertEqual(ReportCard.objects.get(external_id=report_card_id).state, ReportCardState.RELEASED)

    def test_an_already_released_card_remains_readable_even_though_the_school_never_pays_again(self):
        report_card_id = self.compile_real_batch()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "submit"}, operation="update",
            ))
            self.ok(self.push(
                self.administrator, entity_type="academic_report_card", entity_id=report_card_id,
                payload={"id": report_card_id, "action": "release"}, operation="update",
            ))
        BillingInvoice.objects.filter(school=self.school).update(status=InvoiceStatus.OPEN, amount_paid_minor=0)
        self.client.force_authenticate(self.administrator.user)
        response = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)})
        self.assertEqual(response.status_code, 200)
        found = {(r["entityType"], r["entityId"]) for r in response.json()["records"]}
        self.assertIn(("academic_report_card", report_card_id), found)
