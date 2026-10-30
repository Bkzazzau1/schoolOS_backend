"""Premium-feature enforcement (Phase C): CBT exam publishing and report-card generation are
gated through the real sync mutation endpoint, behind PREMIUM_ENFORCEMENT_ENABLED - off by
default, so installing this code changes nothing for any real school until it is deliberately
switched on. Each test distinguishes "blocked for premium reasons" from "passed the premium
gate and reached ordinary validation" by the rejection message, since both currently surface
as the same disposition/status code - a minimal, intentionally-invalid payload is enough to
prove where in the pipeline a request was actually stopped, without needing a full class/term
fixture chain that has nothing to do with what this phase changed."""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.billing.models import BillingInvoice, InvoiceStatus, SubscriptionStatus, UsageSnapshot
from apps.billing.services import ensure_organization_subscription
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School
from apps.sync.access_tiers import premium_code_for

User = get_user_model()
PASSWORD = "a-long-test-password-1"

PREMIUM_MESSAGE = "current term subscription"


class PremiumCodeForClassificationTests(TestCase):
    def test_cbt_publish_is_the_only_premium_cbt_action(self):
        self.assertIsNotNone(premium_code_for("academic_cbt_test", {"action": "publish"}))
        self.assertIsNone(premium_code_for("academic_cbt_test", {"action": "saveDraft"}))
        self.assertIsNone(premium_code_for("academic_cbt_test", {"action": "close"}))

    def test_a_students_cbt_attempt_is_never_premium_gated(self):
        self.assertIsNone(premium_code_for("academic_cbt_attempt", {"action": "submit"}))
        self.assertIsNone(premium_code_for("academic_cbt_attempt", {"action": "start"}))
        self.assertIsNone(premium_code_for("academic_cbt_attempt", {"action": "answer"}))

    def test_report_card_batch_compile_is_premium(self):
        self.assertIsNotNone(premium_code_for("academic_report_card_batch", {"action": "compile"}))

    def test_reviewing_an_existing_report_card_is_never_premium_gated(self):
        self.assertIsNone(premium_code_for("academic_report_card", {"action": "principalReview"}))
        self.assertIsNone(premium_code_for("academic_report_card", {"action": "release"}))

    def test_an_unclassified_entity_type_is_never_premium_gated(self):
        self.assertIsNone(premium_code_for("not_a_real_entity", {"action": "publish"}))


class _PremiumPushTestCase(APITestCase):
    def _school(self, *, status=SubscriptionStatus.ACTIVE):
        organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-premium-{uuid.uuid4().hex[:8]}")
        school = School.objects.create(name="BrightGate Academy", slug=f"bg-academy-premium-{uuid.uuid4().hex[:8]}", organization=organization)
        subscription = ensure_organization_subscription(organization)
        subscription.status = status
        subscription.save(update_fields=["status"])
        return school, subscription

    def _pay_current_term(self, school, subscription):
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=89)
        snapshot = UsageSnapshot.objects.create(
            organization=school.organization, subscription=subscription,
            period_start=start, period_end=end, active_school_count=1,
            billable_student_count=50, source="test",
        )
        BillingInvoice.objects.create(
            organization=school.organization, subscription=subscription, school=school,
            usage_snapshot=snapshot, number=f"TEST-{uuid.uuid4().hex[:10]}", currency="NGN",
            base_amount_minor=4_000_000, amount_due_minor=4_000_000, amount_paid_minor=4_000_000,
            status=InvoiceStatus.PAID, period_start=start, period_end=end,
        )

    def push(self, school, membership, *, entity_type, entity_id, payload, operation="create"):
        self.client.force_authenticate(membership.user)
        return self.client.post(
            "/api/v1/sync/push/",
            {
                "id": f"mut-{uuid.uuid4().hex}",
                "tenantId": str(school.id),
                "membershipId": str(membership.id),
                "entityType": entity_type,
                "entityId": entity_id,
                "operation": operation,
                "payload": payload,
            },
            format="json",
        )


class CbtExamPublishEnforcementTests(_PremiumPushTestCase):
    def _publish_payload(self, entity_id):
        # classSubjectId/termId are deliberately made-up: the premium check (when it fires)
        # runs before this payload is ever validated against real records, so a bogus id is
        # enough to tell "blocked for premium reasons" apart from "reached real validation".
        return {
            "id": entity_id,
            "action": "publish",
            "classSubjectId": str(uuid.uuid4()),
            "termId": str(uuid.uuid4()),
            "title": "Mid-term Mathematics",
            "durationMinutes": 60,
            "resultMode": "score_only",
            "questions": [],
        }

    def _teacher(self, school):
        user = User.objects.create_user(f"teacher-{uuid.uuid4().hex[:8]}@brightgate.ng", PASSWORD)
        return Membership.objects.create(user=user, school=school, role=Role.TEACHER)

    def test_publish_is_blocked_with_the_premium_message_when_enforcement_is_on_and_unpaid(self):
        school, _ = self._school()
        membership = self._teacher(school)
        entity_id = str(uuid.uuid4())
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(school, membership, entity_type="academic_cbt_test", entity_id=entity_id, payload=self._publish_payload(entity_id))
        self.assertEqual(response.status_code, 422)
        self.assertIn(PREMIUM_MESSAGE, response.json()["message"])

    def test_publish_reaches_ordinary_validation_when_enforcement_is_off_by_default(self):
        school, _ = self._school()
        membership = self._teacher(school)
        entity_id = str(uuid.uuid4())
        response = self.push(school, membership, entity_type="academic_cbt_test", entity_id=entity_id, payload=self._publish_payload(entity_id))
        self.assertNotIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertIn("Class subject does not exist", response.json()["message"])

    def test_publish_reaches_ordinary_validation_when_enforcement_is_on_and_school_is_paid(self):
        school, subscription = self._school()
        self._pay_current_term(school, subscription)
        membership = self._teacher(school)
        entity_id = str(uuid.uuid4())
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(school, membership, entity_type="academic_cbt_test", entity_id=entity_id, payload=self._publish_payload(entity_id))
        self.assertNotIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertIn("Class subject does not exist", response.json()["message"])

    def test_saving_a_draft_is_never_premium_gated_even_when_enforcement_is_on(self):
        school, _ = self._school()
        membership = self._teacher(school)
        entity_id = str(uuid.uuid4())
        payload = self._publish_payload(entity_id)
        payload["action"] = "saveDraft"
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(school, membership, entity_type="academic_cbt_test", entity_id=entity_id, payload=payload)
        self.assertNotIn(PREMIUM_MESSAGE, response.json()["message"])

    def test_a_students_attempt_submission_is_never_premium_gated_even_when_enforcement_is_on(self):
        school, _ = self._school()
        user = User.objects.create_user(f"student-{uuid.uuid4().hex[:8]}@brightgate.ng", PASSWORD)
        membership = Membership.objects.create(user=user, school=school, role=Role.STUDENT)
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(
                school, membership, entity_type="academic_cbt_attempt", entity_id=str(uuid.uuid4()),
                payload={"action": "submit", "answers": {}}, operation="update",
            )
        self.assertNotIn(PREMIUM_MESSAGE, response.json().get("message", ""))


class ReportCardGenerationEnforcementTests(_PremiumPushTestCase):
    def _compile_payload(self, entity_id):
        return {"id": entity_id, "action": "compile", "classId": str(uuid.uuid4()), "termId": str(uuid.uuid4())}

    def _administrator(self, school):
        user = User.objects.create_user(f"admin-{uuid.uuid4().hex[:8]}@brightgate.ng", PASSWORD)
        return Membership.objects.create(user=user, school=school, role=Role.ADMINISTRATOR)

    def test_compile_is_blocked_with_the_premium_message_when_enforcement_is_on_and_unpaid(self):
        school, _ = self._school()
        membership = self._administrator(school)
        entity_id = str(uuid.uuid4())
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(school, membership, entity_type="academic_report_card_batch", entity_id=entity_id, payload=self._compile_payload(entity_id))
        self.assertEqual(response.status_code, 422)
        self.assertIn(PREMIUM_MESSAGE, response.json()["message"])

    def test_compile_reaches_ordinary_validation_when_enforcement_is_off_by_default(self):
        school, _ = self._school()
        membership = self._administrator(school)
        entity_id = str(uuid.uuid4())
        response = self.push(school, membership, entity_type="academic_report_card_batch", entity_id=entity_id, payload=self._compile_payload(entity_id))
        self.assertNotIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertIn("Class does not exist", response.json()["message"])

    def test_compile_reaches_ordinary_validation_when_enforcement_is_on_and_school_is_paid(self):
        school, subscription = self._school()
        self._pay_current_term(school, subscription)
        membership = self._administrator(school)
        entity_id = str(uuid.uuid4())
        with override_settings(PREMIUM_ENFORCEMENT_ENABLED=True):
            response = self.push(school, membership, entity_type="academic_report_card_batch", entity_id=entity_id, payload=self._compile_payload(entity_id))
        self.assertNotIn(PREMIUM_MESSAGE, response.json()["message"])
        self.assertIn("Class does not exist", response.json()["message"])
