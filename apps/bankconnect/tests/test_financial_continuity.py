"""Smart Money Collection viewing and reconciliation are financial-continuity operations
(Phase C freemium policy): a school's own fee collection must stay reachable regardless of
its SkulGen subscription status. Managing providers/policy/batches is an account-management
action and keeps today's ordinary subscription gate."""

from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School

from ..permissions import NEED_PROVIDER, NEED_RECONCILE, NEED_VIEW, acting_membership

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class _FakeRequest:
    def __init__(self, user):
        self.user = user
        self.query_params = {}
        self.data = {}


class FinancialContinuityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner@brightgate.ng", PASSWORD)

    def _school(self, status):
        organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-fc-{status}")
        plan = Plan.objects.create(code=f"plan-fc-{status}", name="Test plan")
        school = School.objects.create(name="BrightGate Academy", slug=f"bg-academy-fc-{status}", organization=organization)
        Membership.objects.create(user=self.user, school=school, role=Role.PROPRIETOR)
        OrganizationSubscription.objects.create(organization=organization, plan=plan, status=status)
        return school

    def test_viewing_collections_works_when_restricted(self):
        school = self._school(SubscriptionStatus.RESTRICTED)
        membership = acting_membership(_FakeRequest(self.user), school.id, need=NEED_VIEW)
        self.assertEqual(membership.school_id, school.id)

    def test_viewing_collections_works_even_when_suspended(self):
        school = self._school(SubscriptionStatus.SUSPENDED)
        membership = acting_membership(_FakeRequest(self.user), school.id, need=NEED_VIEW)
        self.assertEqual(membership.school_id, school.id)

    def test_deciding_reconciliation_works_when_restricted(self):
        school = self._school(SubscriptionStatus.RESTRICTED)
        membership = acting_membership(_FakeRequest(self.user), school.id, need=NEED_RECONCILE)
        self.assertEqual(membership.school_id, school.id)

    def test_managing_providers_is_still_blocked_when_restricted(self):
        school = self._school(SubscriptionStatus.RESTRICTED)
        with self.assertRaises(PermissionDenied):
            acting_membership(_FakeRequest(self.user), school.id, need=NEED_PROVIDER)
