"""Organization creation and school provisioning: who may do it, what it writes, and that one
organization's accounts and schools are never reachable through another's."""

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APITestCase

from apps.billing.models import OrganizationSubscription, SubscriptionStatus
from apps.schools.models import Membership, Role, School, SchoolType

from ..models import Organization, OrganizationAuditEvent, OrganizationMembership, OrganizationRole
from ..services import create_organization, provision_school

User = get_user_model()
PASSWORD = "a-long-test-password-1"


def _verified_user(email, **extra):
    """A real person who has already verified their email - the common case, and the one that does
    not trip the one-school/one-org-before-verification guard this suite is not testing here."""
    user = User.objects.create_user(email, PASSWORD, **extra)
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    return user


class OrganizationsTestCase(APITestCase):
    def setUp(self):
        self.user = _verified_user("owner@example.ng", first_name="Amina")

    def url(self, tail: str) -> str:
        return f"/api/v1/{tail}"

    def post(self, tail, data=None, *, who=None):
        self.client.force_authenticate((who or self.user))
        return self.client.post(self.url(tail), data or {}, format="json")

    def get(self, tail, *, who=None):
        self.client.force_authenticate((who or self.user))
        return self.client.get(self.url(tail))

    def make_org(self, name="BrightGate Group", *, owner=None):
        owner = owner or self.user
        organization, membership = create_organization(actor=owner, name=name)
        return organization, membership


class OrganizationCreationTests(OrganizationsTestCase):
    def test_creating_an_organization_makes_the_actor_its_first_owner_with_a_real_subscription(self):
        organization, membership = self.make_org()
        self.assertEqual((membership.user, membership.role), (self.user, OrganizationRole.OWNER))
        self.assertTrue(organization.is_active)
        subscription = OrganizationSubscription.objects.get(organization=organization)
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)
        self.assertIsNotNone(subscription.plan)

    def test_creation_is_audited(self):
        organization, membership = self.make_org()
        event = OrganizationAuditEvent.objects.get(organization=organization, action="organization_created")
        self.assertEqual((event.actor, event.target_type, event.target_id), (self.user, "organization", str(organization.id)))

    def test_a_blank_or_overlong_name_is_refused(self):
        for bad_name in ("", " ", "x" * 201):
            with self.assertRaises(ValidationError):
                create_organization(actor=self.user, name=bad_name)

    def test_two_organizations_with_the_same_chosen_name_still_get_distinct_slugs(self):
        a, _ = create_organization(actor=self.user, name="BrightGate")
        other = User.objects.create_user("second@example.ng", PASSWORD)
        other.email_verified_at = None
        b, _ = create_organization(actor=other, name="BrightGate")
        self.assertNotEqual(a.slug, b.slug)

    def test_the_api_creates_an_organization_for_the_signed_in_user(self):
        response = self.post("organizations/", {"name": "Example Schools Group"})
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual(body["organization"]["name"], "Example Schools Group")
        self.assertEqual(body["membership"]["role"], OrganizationRole.OWNER)

    def test_the_api_lists_only_this_users_own_memberships(self):
        self.make_org("Mine")
        stranger = User.objects.create_user("stranger@example.ng", PASSWORD)
        create_organization(actor=stranger, name="Someone Else's")
        body = self.get("organizations/").json()
        self.assertEqual([o["organizationName"] for o in body["organizations"]], ["Mine"])


class SchoolProvisioningTests(OrganizationsTestCase):
    def setUp(self):
        super().setUp()
        self.organization, self.owner_membership = self.make_org()

    def provision(self, *, who=None, name="Example Academy", school_type=SchoolType.SECONDARY, location="Kaduna"):
        return provision_school(
            actor=who or self.user, organization_id=self.organization.id, name=name, school_type=school_type, location=location,
        )

    def test_provisioning_creates_an_isolated_school_and_proprietor_access(self):
        school, membership = self.provision()
        self.assertEqual(school.organization_id, self.organization.id)
        self.assertEqual((membership.user, membership.school, membership.role), (self.user, school, Role.PROPRIETOR))

    def test_a_brand_new_school_publishes_a_real_zero_student_meter_immediately(self):
        from apps.billing.models import SchoolBillingMeterSnapshot

        school, _ = self.provision()
        meter = SchoolBillingMeterSnapshot.objects.filter(school=school).order_by("-measured_at").first()
        self.assertIsNotNone(meter)
        self.assertEqual((meter.billable_student_count, meter.authoritative), (0, True))

    def test_provisioning_is_audited_with_the_subscription_and_plan_it_saw(self):
        school, membership = self.provision()
        event = OrganizationAuditEvent.objects.get(organization=self.organization, action="school_created")
        self.assertEqual(event.detail["schoolName"], school.name)
        self.assertEqual(event.detail["proprietorMembershipId"], str(membership.id))
        self.assertIsNotNone(event.detail["planCode"])

    def test_a_second_school_is_a_real_multi_school_organization(self):
        first, _ = self.provision(name="Campus One")
        second, _ = self.provision(name="Campus Two")
        self.assertEqual(
            set(School.objects.filter(organization=self.organization, is_active=True).values_list("name", flat=True)),
            {"Campus One", "Campus Two"},
        )
        self.assertNotEqual(first.id, second.id)

    def test_only_an_owner_or_administrator_of_the_organization_may_provision(self):
        billing_admin_user = User.objects.create_user("billing@example.ng", PASSWORD)
        OrganizationMembership.objects.create(user=billing_admin_user, organization=self.organization, role=OrganizationRole.BILLING_ADMINISTRATOR)
        with self.assertRaises(PermissionDenied):
            self.provision(who=billing_admin_user)

    def test_a_stranger_with_no_membership_at_all_is_refused(self):
        stranger = User.objects.create_user("stranger@example.ng", PASSWORD)
        with self.assertRaises(PermissionDenied):
            self.provision(who=stranger)

    def test_invalid_input_is_refused_before_anything_is_created(self):
        cases = [
            {"name": "x", "school_type": SchoolType.SECONDARY, "location": "Lagos"},
            {"name": "Valid Name", "school_type": "not_a_real_type", "location": "Lagos"},
            {"name": "Valid Name", "school_type": SchoolType.SECONDARY, "location": ""},
        ]
        for fields in cases:
            with self.assertRaises(ValidationError, msg=fields):
                provision_school(actor=self.user, organization_id=self.organization.id, **fields)
        self.assertFalse(School.objects.filter(organization=self.organization).exists())

    def test_the_api_provisions_a_school_for_the_organization(self):
        response = self.post(
            f"organizations/{self.organization.id}/schools/",
            {"name": "Example International", "schoolType": SchoolType.PRIMARY, "location": "Abuja"},
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["school"]["organizationId"], str(self.organization.id))

    def test_the_apis_school_list_is_scoped_to_the_organization_and_refuses_an_outsider(self):
        self.provision(name="Visible School")
        other_org, other_owner = self.make_org("Other Group", owner=User.objects.create_user("other@example.ng", PASSWORD))
        response = self.get(f"organizations/{other_org.id}/schools/")
        self.assertEqual(response.status_code, 403)
        mine = self.get(f"organizations/{self.organization.id}/schools/").json()
        self.assertEqual([s["name"] for s in mine["schools"]], ["Visible School"])


class CrossOrganizationIsolationTests(OrganizationsTestCase):
    def test_one_organizations_owner_cannot_provision_into_another_organizations_account(self):
        mine, _ = self.make_org("Mine")
        other_owner_user = User.objects.create_user("otherowner@example.ng", PASSWORD)
        theirs, _ = self.make_org("Theirs", owner=other_owner_user)
        with self.assertRaises(PermissionDenied):
            provision_school(actor=self.user, organization_id=theirs.id, name="Intrusion Academy", school_type=SchoolType.SECONDARY, location="Lagos")
        self.assertFalse(School.objects.filter(organization=theirs, name="Intrusion Academy").exists())

    def test_a_schools_students_and_billing_never_cross_into_a_sibling_organization(self):
        org_a, _ = self.make_org("Org A")
        school_a, _ = provision_school(actor=self.user, organization_id=org_a.id, name="School A", school_type=SchoolType.SECONDARY, location="Lagos")
        other_user = User.objects.create_user("b@example.ng", PASSWORD)
        org_b, _ = self.make_org("Org B", owner=other_user)
        school_b, _ = provision_school(actor=other_user, organization_id=org_b.id, name="School B", school_type=SchoolType.SECONDARY, location="Lagos")
        self.assertEqual(set(School.objects.filter(organization=org_a).values_list("id", flat=True)), {school_a.id})
        self.assertEqual(set(School.objects.filter(organization=org_b).values_list("id", flat=True)), {school_b.id})
