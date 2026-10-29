"""Section 10 of Phase C asks that restricted/suspended access be proven for proprietors, staff,
parents and students specifically, and at the server (HTTP) level, not only via direct calls into
apps.core.permissions (apps.core.tests.test_subscription_access already covers the mechanism itself
with one representative role). This exercises the real sync endpoints every one of those four roles
uses: GET /api/v1/sync/pull/ (read, explicitly allowed to continue under a restricted subscription so
existing academic/financial records stay reachable) and POST /api/v1/sync/push/ (write, never allowed
once billing access is not full)."""

import uuid

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"

ROLES_UNDER_TEST = [Role.PROPRIETOR, Role.STAFF, Role.PARENT, Role.STUDENT]


class SubscriptionAccessByRoleTests(APITestCase):
    def _member(self, role, status):
        user = User.objects.create_user(f"{role}-{status}@example.ng", PASSWORD)
        organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-{role}-{status}")
        plan = Plan.objects.create(code=f"plan-{role}-{status}", name="Test plan")
        school = School.objects.create(name="BrightGate Academy", slug=f"bg-school-{role}-{status}", organization=organization)
        membership = Membership.objects.create(user=user, school=school, role=role)
        subscription = OrganizationSubscription.objects.create(organization=organization, plan=plan, status=status)
        return user, school, membership, subscription

    def pull(self, user, school, membership):
        self.client.force_authenticate(user)
        return self.client.get(f"/api/v1/sync/pull/?school={school.id}&membership={membership.id}")

    def push(self, user, school, membership):
        self.client.force_authenticate(user)
        return self.client.post(
            "/api/v1/sync/push/",
            {
                "id": f"mut-{uuid.uuid4().hex}",
                "tenantId": str(school.id),
                "membershipId": str(membership.id),
                "entityType": "not_a_real_entity",
                "entityId": "x",
                "operation": "delete",
            },
            format="json",
        )

    def test_restricted_allows_reads_but_blocks_writes_for_every_role(self):
        for role in ROLES_UNDER_TEST:
            user, school, membership, _ = self._member(role, SubscriptionStatus.RESTRICTED)
            pull_response = self.pull(user, school, membership)
            push_response = self.push(user, school, membership)
            self.assertEqual(pull_response.status_code, 200, (role, pull_response.json()))
            self.assertEqual(push_response.status_code, 403, (role, push_response.json()))

    def test_suspended_blocks_both_reads_and_writes_for_every_role(self):
        for role in ROLES_UNDER_TEST:
            user, school, membership, _ = self._member(role, SubscriptionStatus.SUSPENDED)
            pull_response = self.pull(user, school, membership)
            push_response = self.push(user, school, membership)
            self.assertEqual(pull_response.status_code, 403, (role, pull_response.json()))
            self.assertEqual(push_response.status_code, 403, (role, push_response.json()))

    def test_recovering_to_active_restores_both_for_every_role(self):
        for role in ROLES_UNDER_TEST:
            user, school, membership, subscription = self._member(role, SubscriptionStatus.SUSPENDED)
            self.assertEqual(self.pull(user, school, membership).status_code, 403, role)
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.save(update_fields=["status"])
            self.assertEqual(self.pull(user, school, membership).status_code, 200, role)
            self.assertNotEqual(self.push(user, school, membership).status_code, 403, role)
