from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School

User = get_user_model()
PULL = "/api/v1/sync/pull/"
PUSH = "/api/v1/sync/push/"


class SyncSubscriptionAccessTests(APITestCase):
    """End to end, through the real HTTP views: a restricted/suspended organization's
    subscription actually stops its schools' sync, not just account growth."""

    def setUp(self):
        self.organization = Organization.objects.create(name="BrightGate Group", slug="brightgate-group")
        plan = Plan.objects.create(code="standard-test", name="Test plan")
        self.school = School.objects.create(name="BrightGate Academy", slug="brightgate", organization=self.organization)
        self.user = User.objects.create_user("teacher@brightgate.ng", "a-long-test-password-1")
        Membership.objects.create(user=self.user, school=self.school, role=Role.TEACHER)
        self.client.force_authenticate(self.user)
        self.subscription = OrganizationSubscription.objects.create(
            organization=self.organization, plan=plan, status=SubscriptionStatus.ACTIVE
        )

    def _set_status(self, status):
        self.subscription.status = status
        self.subscription.save(update_fields=["status"])

    def push_body(self):
        return {
            "id": "m1",
            "tenantId": str(self.school.id),
            "membershipId": str(Membership.objects.get(user=self.user, school=self.school).id),
            "operation": "create",
            "entityType": "some_future_thing",
            "entityId": "e1",
            "payload": {"title": "Assembly"},
        }

    def test_an_active_subscription_leaves_sync_working_normally(self):
        self.assertEqual(self.client.get(PULL, {"school": str(self.school.id), "since": 0}).status_code, 200)
        self.assertEqual(self.client.post(PUSH, self.push_body(), format="json").status_code, 200)

    def test_restricted_still_allows_pulling_existing_data_but_refuses_new_pushes(self):
        self._set_status(SubscriptionStatus.RESTRICTED)
        pull = self.client.get(PULL, {"school": str(self.school.id), "since": 0})
        self.assertEqual(pull.status_code, 200)
        push = self.client.post(PUSH, self.push_body(), format="json")
        self.assertEqual(push.status_code, 403)
        self.assertIn("needs attention", push.json()["detail"])

    def test_suspended_refuses_both_pull_and_push(self):
        self._set_status(SubscriptionStatus.SUSPENDED)
        pull = self.client.get(PULL, {"school": str(self.school.id), "since": 0})
        self.assertEqual(pull.status_code, 403)
        self.assertIn("suspended", pull.json()["detail"])
        push = self.client.post(PUSH, self.push_body(), format="json")
        self.assertEqual(push.status_code, 403)

    def test_access_me_always_keeps_working_so_the_app_can_explain_the_block(self):
        self._set_status(SubscriptionStatus.SUSPENDED)
        response = self.client.get(f"/api/v1/schools/{self.school.id}/access/me/")
        self.assertEqual(response.status_code, 200)

    def test_recovering_restores_sync_immediately_with_no_extra_step(self):
        self._set_status(SubscriptionStatus.SUSPENDED)
        self.assertEqual(self.client.post(PUSH, self.push_body(), format="json").status_code, 403)
        self._set_status(SubscriptionStatus.ACTIVE)
        self.assertEqual(self.client.post(PUSH, self.push_body(), format="json").status_code, 200)
