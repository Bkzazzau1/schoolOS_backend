"""A school whose organization's SkulGen subscription is restricted or suspended
must actually stop working for everyone at it, not just block growing the
account (apps.billing.services.require_entitlement already covered that part).
This is the enforcement itself: apps.core.permissions."""

from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionStatus
from apps.core.permissions import active_membership, membership_from_request, require_membership
from apps.organizations.models import Organization
from apps.schools.models import Membership, Role, School

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class _FakeRequest:
    """membership_from_request only reads .user and .query_params."""

    def __init__(self, user):
        self.user = user
        self.query_params = {}


class SubscriptionAccessTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner@school.ng", PASSWORD, first_name="Ibrahim")

    def _subscription(self, status):
        organization = Organization.objects.create(name="BrightGate Group", slug=f"brightgate-group-{status}")
        plan = Plan.objects.create(code=f"plan-{status}", name="Test plan")
        school = School.objects.create(name="BrightGate Academy", slug=f"brightgate-{status}", organization=organization)
        Membership.objects.create(user=self.user, school=school, role=Role.TEACHER)
        subscription = OrganizationSubscription.objects.create(organization=organization, plan=plan, status=status)
        return school, subscription

    def test_a_school_with_no_organization_is_never_affected(self):
        school = School.objects.create(name="Independent School", slug="independent")
        Membership.objects.create(user=self.user, school=school, role=Role.TEACHER)
        # Does not raise, regardless of there being no subscription anywhere.
        require_membership(self.user, school.id)

    def test_active_trial_past_due_and_grace_are_all_full_access(self):
        for status in (
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.GRACE,
        ):
            school, _ = self._subscription(status)
            require_membership(self.user, school.id)  # does not raise

    def test_restricted_blocks_require_membership_by_default(self):
        school, _ = self._subscription(SubscriptionStatus.RESTRICTED)
        with self.assertRaises(PermissionDenied) as caught:
            require_membership(self.user, school.id)
        self.assertIn("subscription needs attention", str(caught.exception))

    def test_restricted_allows_the_read_only_opt_out(self):
        school, _ = self._subscription(SubscriptionStatus.RESTRICTED)
        # Does not raise: sync pull and similar read-only call sites opt in to this.
        membership = require_membership(self.user, school.id, allow_when_restricted=True)
        self.assertEqual(membership.school_id, school.id)

    def test_suspended_blocks_even_the_read_only_opt_out(self):
        school, _ = self._subscription(SubscriptionStatus.SUSPENDED)
        with self.assertRaises(PermissionDenied) as caught:
            require_membership(self.user, school.id, allow_when_restricted=True)
        self.assertIn("subscription is suspended", str(caught.exception))

    def test_cancelled_is_treated_the_same_as_suspended(self):
        school, _ = self._subscription(SubscriptionStatus.CANCELLED)
        with self.assertRaises(PermissionDenied):
            require_membership(self.user, school.id)

    def test_bypass_always_works_regardless_of_status_so_a_blocked_person_can_still_learn_why(self):
        for status in (SubscriptionStatus.RESTRICTED, SubscriptionStatus.SUSPENDED):
            school, _ = self._subscription(status)
            request = _FakeRequest(self.user)
            membership = membership_from_request(request, school.id, bypass_subscription_check=True)
            self.assertEqual(membership.school_id, school.id)

    def test_active_membership_is_blocked_the_same_way_require_membership_is(self):
        school, _ = self._subscription(SubscriptionStatus.SUSPENDED)
        with self.assertRaises(PermissionDenied):
            active_membership(self.user, school.id)

    def test_active_membership_returns_none_for_a_real_stranger_before_any_subscription_check(self):
        school, _ = self._subscription(SubscriptionStatus.SUSPENDED)
        stranger = User.objects.create_user("stranger@school.ng", PASSWORD)
        self.assertIsNone(active_membership(stranger, school.id))

    def test_recovering_to_active_restores_access_immediately(self):
        school, subscription = self._subscription(SubscriptionStatus.SUSPENDED)
        with self.assertRaises(PermissionDenied):
            require_membership(self.user, school.id)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.save(update_fields=["status"])
        require_membership(self.user, school.id)  # does not raise, no code change needed
