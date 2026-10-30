"""Viewing mandates/payment status is a financial-continuity operation (Phase C freemium
policy): it stays reachable regardless of SkulGen subscription status. Starting or managing
a mandate is an active account action and keeps today's ordinary subscription gate."""

from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School

from ..permissions import NEED_MANAGE, NEED_VIEW, acting_membership

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class _FakeRequest:
    def __init__(self, user):
        self.user = user
        self.query_params = {}
        self.data = {}


class MandateFinancialContinuityTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner@brightgate.ng", PASSWORD)

    def _school(self, status):
        organization = Organization.objects.create(name="BrightGate Group", slug=f"bg-mandate-fc-{status}")
        plan = Plan.objects.create(code=f"plan-mandate-fc-{status}", name="Test plan")
        school = School.objects.create(name="BrightGate Academy", slug=f"bg-academy-mandate-fc-{status}", organization=organization)
        Membership.objects.create(user=self.user, school=school, role=Role.PROPRIETOR)
        OrganizationSubscription.objects.create(organization=organization, plan=plan, status=status)
        return school

    def test_viewing_mandates_works_when_restricted(self):
        school = self._school(SubscriptionStatus.RESTRICTED)
        membership = acting_membership(_FakeRequest(self.user), school.id, need=NEED_VIEW)
        self.assertEqual(membership.school_id, school.id)

    def test_viewing_mandates_works_even_when_suspended(self):
        school = self._school(SubscriptionStatus.SUSPENDED)
        membership = acting_membership(_FakeRequest(self.user), school.id, need=NEED_VIEW)
        self.assertEqual(membership.school_id, school.id)

    def test_managing_mandates_is_still_blocked_when_restricted(self):
        school = self._school(SubscriptionStatus.RESTRICTED)
        with self.assertRaises(PermissionDenied):
            acting_membership(_FakeRequest(self.user), school.id, need=NEED_MANAGE)
