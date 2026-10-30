"""Essential school operations must keep working through the real sync mutation endpoint
even when the organization's SkulGen subscription is restricted or suspended (Phase C
freemium policy) - starting a term, admitting students, managing staff records. Everything
not explicitly classified in apps.sync.access_tiers keeps today's exact behaviour: blocked."""

import uuid

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class EssentialSyncAccessTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("admin@brightgate.ng", PASSWORD)

    def _restricted_school(self, status):
        from apps.organizations.models import Organization

        organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-essential-{status}")
        plan = Plan.objects.create(code=f"plan-essential-{status}", name="Test plan")
        school = School.objects.create(name="BrightGate Academy", slug=f"bg-academy-essential-{status}", organization=organization)
        membership = Membership.objects.create(user=self.user, school=school, role=Role.ADMINISTRATOR)
        OrganizationSubscription.objects.create(organization=organization, plan=plan, status=status)
        return school, membership

    def push(self, school, membership, *, entity_type, entity_id, payload, operation="create"):
        self.client.force_authenticate(self.user)
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

    def _session_payload(self, entity_id):
        return {
            "id": entity_id,
            "code": "2026-session",
            "name": "2026 Academic Session",
            "startsOn": "2026-09-01",
            "endsOn": "2027-07-31",
            "status": "planned",
        }

    def test_starting_a_new_academic_session_works_under_a_restricted_subscription(self):
        school, membership = self._restricted_school(SubscriptionStatus.RESTRICTED)
        entity_id = str(uuid.uuid4())
        response = self.push(school, membership, entity_type="academic_session", entity_id=entity_id, payload=self._session_payload(entity_id))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["disposition"], "accepted")

    def test_starting_a_new_academic_session_works_even_when_suspended(self):
        school, membership = self._restricted_school(SubscriptionStatus.SUSPENDED)
        entity_id = str(uuid.uuid4())
        response = self.push(school, membership, entity_type="academic_session", entity_id=entity_id, payload=self._session_payload(entity_id))
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["disposition"], "accepted")

    def test_an_unclassified_entity_type_is_still_blocked_under_restricted(self):
        school, membership = self._restricted_school(SubscriptionStatus.RESTRICTED)
        response = self.push(school, membership, entity_type="not_a_real_entity", entity_id="x", payload={}, operation="delete")
        self.assertEqual(response.status_code, 403)

    def test_an_unclassified_entity_type_still_works_when_fully_active(self):
        school, membership = self._restricted_school(SubscriptionStatus.ACTIVE)
        response = self.push(school, membership, entity_type="not_a_real_entity", entity_id="x", payload={}, operation="delete")
        self.assertNotEqual(response.status_code, 403)
