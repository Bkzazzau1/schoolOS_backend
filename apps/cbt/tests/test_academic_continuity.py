"""A real, published CBT examination and a real student's attempt at it - proving academic
continuity (Phase C correction brief, item 2): once a teacher has published an exam, a
student's start/answer/submit flow must keep working no matter what happens to the school's
subscription afterward, even with PREMIUM_ENFORCEMENT_ENABLED on. Only publishing a *new* exam
is the premium action; everything downstream of an already-published one is academic
continuity (apps.sync.access_tiers.ACADEMIC_CONTINUITY_ENTITY_TYPES), never judged by
apps.billing.premium_access at all. Built from real academic/enrollment records - the same
models apps.cbt.services itself reads - not a dummy payload that would only ever reach an
unrelated validation failure."""

import uuid
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.academics.models import (
    AcademicClass,
    AcademicSession,
    AcademicTerm,
    ClassSubject,
    EnrollmentAcademicContext,
    Subject,
    TeachingAssignment,
)
from apps.billing.models import BillingInvoice, InvoiceStatus, SubscriptionStatus, UsageSnapshot
from apps.billing.services import ensure_organization_subscription
from apps.cbt.models import CbtAttempt
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School
from apps.students.models import EnrollmentStatus, Student, StudentEnrollment

User = get_user_model()
PASSWORD = "a-long-test-password-1"
PREMIUM_MESSAGE = "current term subscription"


class CbtExamContinuityTests(APITestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-cbt-{uuid.uuid4().hex[:8]}")
        self.school = School.objects.create(
            name="BrightGate Academy", slug=f"bg-cbt-academy-{uuid.uuid4().hex[:8]}", organization=self.organization
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
        self.academic_class = AcademicClass.objects.create(
            school=self.school, code="JSS2A", name="JSS 2A", section="Secondary", level_order=1,
        )
        self.subject = Subject.objects.create(school=self.school, code="MATH", name="Mathematics", section="Secondary")
        self.class_subject = ClassSubject.objects.create(
            session=self.session, academic_class=self.academic_class, subject=self.subject
        )

        teacher_user = User.objects.create_user("teacher@brightgate.ng", PASSWORD)
        self.teacher = Membership.objects.create(user=teacher_user, school=self.school, role=Role.TEACHER)
        TeachingAssignment.objects.create(
            school=self.school, external_id="TA-1", class_subject=self.class_subject,
            teacher_membership=self.teacher,
            started_at=datetime.combine(today - timedelta(days=10), time.min, tzinfo=dt_timezone.utc),
        )

        self.student_record = Student.objects.create(
            school=self.school, student_code="STU-0001", admission_number="ADM-0001",
            first_name="Amina", surname="Bello",
        )
        enrollment = StudentEnrollment.objects.create(
            school=self.school, student=self.student_record, academic_section="Secondary", class_name="JSS 2A",
            status=EnrollmentStatus.ACTIVE, is_billable=True, started_at=timezone.now() - timedelta(days=10),
        )
        EnrollmentAcademicContext.objects.create(
            enrollment=enrollment, session=self.session, academic_class=self.academic_class,
            entry_term=self.term, source="test",
        )
        student_user = User.objects.create_user("student@brightgate.ng", PASSWORD)
        self.student_record.account_user = student_user
        self.student_record.save(update_fields=["account_user"])
        self.student = Membership.objects.create(user=student_user, school=self.school, role=Role.STUDENT)

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

    def push(self, who, *, entity_type, entity_id, payload, operation="create", mutation_id=None):
        self.client.force_authenticate(who.user)
        return self.client.post(
            "/api/v1/sync/push/",
            {
                "id": mutation_id or f"mut-{uuid.uuid4().hex}", "tenantId": str(self.school.id), "membershipId": str(who.id),
                "entityType": entity_type, "entityId": entity_id, "operation": operation, "payload": payload,
            },
            format="json",
        )

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def _draft_payload(self, test_id):
        return {
            "id": test_id, "action": "saveDraft", "classSubjectId": str(self.class_subject.id), "termId": str(self.term.id),
            "title": "Mid-term Mathematics", "durationMinutes": 30, "resultMode": "score_only",
            "questions": [{"prompt": "2 + 2?", "options": ["3", "4"], "correctIndex": 1}],
        }

    def _publish_payload(self, test_id):
        payload = self._draft_payload(test_id)
        payload["action"] = "publish"
        return payload

    def save_real_draft(self, test_id):
        """A CBT test must exist as a draft - created while saveDraft is never premium-gated -
        before "publish" has anything to act on; this is never itself the premium action."""
        self.ok(self.push(self.teacher, entity_type="academic_cbt_test", entity_id=test_id, payload=self._draft_payload(test_id)))

    def publish_real_exam(self):
        """Publishes a real CBT exam the way the brief insists on: real class/term/teacher
        records, with enforcement ON and the school paid - so the only thing left for the
        attempt-flow tests to prove is continuity, never whether publishing can succeed."""
        test_id = str(uuid.uuid4())
        self.save_real_draft(test_id)
        self._pay_current_term()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.teacher, entity_type="academic_cbt_test", entity_id=test_id,
                payload=self._publish_payload(test_id), operation="update",
            ))
        attempt_id = f"cbt-attempt-{test_id}-{self.student_record.id}"
        return test_id, attempt_id

    # -- publishing itself is the premium gate ------------------------------------------------

    def test_publishing_a_new_exam_is_blocked_while_unpaid_with_enforcement_on(self):
        test_id = str(uuid.uuid4())
        self.save_real_draft(test_id)
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(
                self.teacher, entity_type="academic_cbt_test", entity_id=test_id,
                payload=self._publish_payload(test_id), operation="update",
            )
        self.assertEqual(response.status_code, 422, response.json())
        self.assertIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertFalse(CbtAttempt.objects.filter(school=self.school).exists())

    def test_saving_a_draft_itself_is_never_premium_gated_even_while_unpaid(self):
        test_id = str(uuid.uuid4())
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(self.teacher, entity_type="academic_cbt_test", entity_id=test_id, payload=self._draft_payload(test_id))
        self.assertEqual(response.status_code, 200, response.json())

    # -- continuity: everything downstream of a real publish must keep working ---------------

    def test_a_student_can_start_answer_and_submit_a_real_published_exam_with_enforcement_on(self):
        _test_id, attempt_id = self.publish_real_exam()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "start"}, operation="update",
            ))
            self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "answer", "questionIndex": 0, "optionIndex": 1}, operation="update",
            ))
            result = self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "submit"}, operation="update",
            ))
        self.assertEqual(result["disposition"], "accepted")
        attempt = CbtAttempt.objects.get(school=self.school, external_id=attempt_id)
        self.assertTrue(attempt.submitted)
        self.assertEqual(attempt.score, 1)  # answered the one real question correctly

    def test_an_attempt_already_in_progress_survives_the_schools_subscription_turning_restricted(self):
        _test_id, attempt_id = self.publish_real_exam()
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "start"}, operation="update",
            ))
            # The school's subscription changes mid-exam: not merely unpaid, but the shared
            # organization has escalated all the way to restricted.
            self._make_restricted()
            self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "answer", "questionIndex": 0, "optionIndex": 1}, operation="update",
            ))
            result = self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "submit"}, operation="update",
            ))
        self.assertEqual(result["disposition"], "accepted")
        self.assertTrue(CbtAttempt.objects.get(school=self.school, external_id=attempt_id).submitted)


class CbtPublishRetryAfterPaymentTests(CbtExamContinuityTests):
    """A premium refusal must never be memorized forever under its mutation id (Phase C
    correction brief, item 5) - once the school pays, retrying the exact same mutation id must
    succeed, and succeed exactly once. Only a payment-pending refusal gets this treatment;
    an intrinsically invalid payload must still be cached normally, unaffected."""

    def test_the_same_mutation_id_succeeds_once_payment_clears_it(self):
        from apps.cbt.models import CbtTest, CbtTestState
        from apps.sync.models import MutationLog

        test_id = str(uuid.uuid4())
        self.save_real_draft(test_id)
        mutation_id = f"retry-{uuid.uuid4().hex}"

        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            first = self.push(
                self.teacher, entity_type="academic_cbt_test", entity_id=test_id,
                payload=self._publish_payload(test_id), operation="update", mutation_id=mutation_id,
            )
            self.assertEqual(first.status_code, 422, first.json())
            self.assertIn(PREMIUM_MESSAGE, first.json()["message"])
            self.assertFalse(MutationLog.objects.filter(mutation_id=mutation_id).exists())

            self._pay_current_term()
            second = self.push(
                self.teacher, entity_type="academic_cbt_test", entity_id=test_id,
                payload=self._publish_payload(test_id), operation="update", mutation_id=mutation_id,
            )
        self.assertEqual(second.status_code, 200, second.json())
        self.assertEqual(second.json()["disposition"], "accepted")
        item = CbtTest.objects.get(school=self.school, external_id=test_id)
        self.assertEqual(item.state, CbtTestState.PUBLISHED)

        # A third push of the SAME now-successful mutation id replays the cached acceptance
        # (apply_mutation's ordinary idempotency, unaffected by this change) rather than
        # re-running the publish a second time.
        third = self.push(
            self.teacher, entity_type="academic_cbt_test", entity_id=test_id,
            payload=self._publish_payload(test_id), operation="update", mutation_id=mutation_id,
        )
        self.assertEqual(third.status_code, 200, third.json())
        self.assertEqual(third.json()["disposition"], "accepted")
        self.assertEqual(third.json()["serverVersion"], second.json()["serverVersion"])

    def test_an_ordinary_validation_rejection_is_still_cached_normally(self):
        from apps.sync.models import MutationLog

        bad_test_id = str(uuid.uuid4())
        mutation_id = f"retry-bad-{uuid.uuid4().hex}"
        payload = self._publish_payload(bad_test_id)
        payload["classSubjectId"] = str(uuid.uuid4())  # a class subject that does not exist

        first = self.push(self.teacher, entity_type="academic_cbt_test", entity_id=bad_test_id, payload=payload, mutation_id=mutation_id)
        self.assertEqual(first.status_code, 422, first.json())
        self.assertIn("Class subject does not exist", first.json()["message"])
        self.assertTrue(MutationLog.objects.filter(mutation_id=mutation_id).exists())

        second = self.push(self.teacher, entity_type="academic_cbt_test", entity_id=bad_test_id, payload=payload, mutation_id=mutation_id)
        self.assertEqual(second.json()["message"], first.json()["message"])

    def test_a_published_exams_attempts_work_even_after_its_invoice_lapses_to_unpaid(self):
        """Publishing is the one-time premium gate; once an exam exists, continuity never
        re-checks payment - a school whose invoice later lapses must still let students finish
        exams already published to them."""
        _test_id, attempt_id = self.publish_real_exam()
        BillingInvoice.objects.filter(school=self.school).update(status=InvoiceStatus.OPEN, amount_paid_minor=0)
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "start"}, operation="update",
            ))
            result = self.ok(self.push(
                self.student, entity_type="academic_cbt_attempt", entity_id=attempt_id,
                payload={"id": attempt_id, "action": "submit"}, operation="update",
            ))
        self.assertEqual(result["disposition"], "accepted")
