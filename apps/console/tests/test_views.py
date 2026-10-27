from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.billing.models import OrganizationSubscription, Plan, SubscriptionEvent, SubscriptionStatus
from apps.organizations.models import Organization
from apps.schools.models import School

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class ConsoleAccessTests(TestCase):
    """The console is for SkulGen's own staff, gated by Django's own is_staff -
    never by a school/organization role."""

    def setUp(self):
        self.ordinary_user = User.objects.create_user("owner@school.ng", PASSWORD)
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        response = self.client.get(reverse("console:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_a_real_schoolos_account_that_is_not_platform_staff_is_refused(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.get(reverse("console:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_platform_staff_can_open_the_dashboard(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("console:dashboard"))
        self.assertEqual(response.status_code, 200)


class ConsoleDashboardTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)
        self.plan = Plan.objects.create(code="standard-console-test", name="Test plan")

    def _organization(self, name, slug, status):
        organization = Organization.objects.create(name=name, slug=slug)
        OrganizationSubscription.objects.create(organization=organization, plan=self.plan, status=status)
        return organization

    def test_the_dashboard_counts_every_organization_by_its_real_status(self):
        self._organization("Active Co", "active-co", SubscriptionStatus.ACTIVE)
        self._organization("Restricted Co", "restricted-co", SubscriptionStatus.RESTRICTED)
        response = self.client.get(reverse("console:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_organizations"], 2)
        attention_names = [s.organization.name for s in response.context["needs_attention"]]
        self.assertEqual(attention_names, ["Restricted Co"])

    def test_the_organization_list_can_be_searched_and_filtered_by_status(self):
        self._organization("BrightGate Group", "brightgate-group", SubscriptionStatus.ACTIVE)
        self._organization("Suspended Group", "suspended-group", SubscriptionStatus.SUSPENDED)

        all_results = self.client.get(reverse("console:organizations"))
        self.assertEqual(len(all_results.context["organizations"]), 2)

        searched = self.client.get(reverse("console:organizations"), {"q": "Bright"})
        self.assertEqual([o.name for o in searched.context["organizations"]], ["BrightGate Group"])

        filtered = self.client.get(reverse("console:organizations"), {"status": SubscriptionStatus.SUSPENDED})
        self.assertEqual([o.name for o in filtered.context["organizations"]], ["Suspended Group"])


class ConsoleOrganizationDetailTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)
        self.plan = Plan.objects.create(code="standard-detail-test", name="Standard")
        self.premium_plan = Plan.objects.create(code="premium-detail-test", name="Premium")
        self.organization = Organization.objects.create(name="BrightGate Group", slug="brightgate-group-detail")
        self.subscription = OrganizationSubscription.objects.create(
            organization=self.organization, plan=self.plan, status=SubscriptionStatus.ACTIVE
        )
        School.objects.create(name="BrightGate Academy", slug="brightgate-academy-detail", organization=self.organization)

    def _url(self):
        return reverse("console:organization_detail", args=[self.organization.id])

    def test_shows_the_real_subscription_schools_and_access_mode(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["access_mode"], "full")
        self.assertEqual(len(response.context["schools"]), 1)

    def test_setting_status_changes_it_and_is_audited(self):
        response = self.client.post(self._url(), {"action": "set_status", "status": SubscriptionStatus.SUSPENDED})
        self.assertRedirects(response, self._url())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.SUSPENDED)
        event = SubscriptionEvent.objects.filter(subscription=self.subscription, event="admin_status_changed").get()
        self.assertEqual(event.to_status, SubscriptionStatus.SUSPENDED)
        self.assertEqual(event.detail["adminEmail"], "ops@skulgen.com")
        self.assertEqual(event.detail["source"], "console")

    def test_setting_an_unreal_status_is_refused_and_changes_nothing(self):
        with self.assertRaises(Exception):
            self.client.post(self._url(), {"action": "set_status", "status": "not_a_real_status"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, SubscriptionStatus.ACTIVE)

    def test_setting_plan_changes_it_and_is_audited(self):
        response = self.client.post(self._url(), {"action": "set_plan", "plan_id": str(self.premium_plan.id)})
        self.assertRedirects(response, self._url())
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.plan_id, self.premium_plan.id)
        event = SubscriptionEvent.objects.filter(subscription=self.subscription, event="admin_plan_changed").get()
        self.assertEqual(event.detail["toPlanId"], str(self.premium_plan.id))

    def test_changing_status_through_the_console_actually_enforces_in_the_real_api(self):
        self.client.post(self._url(), {"action": "set_status", "status": SubscriptionStatus.SUSPENDED})
        school = self.organization.schools.get()
        teacher = User.objects.create_user("teacher@brightgate.ng", PASSWORD)
        from apps.schools.models import Membership, Role
        from rest_framework.test import APIClient

        Membership.objects.create(user=teacher, school=school, role=Role.TEACHER)

        api_client = APIClient()
        api_client.force_authenticate(teacher)
        # access/me/ always works regardless (bypass_subscription_check) - sync is what's blocked.
        response = api_client.get(f"/api/v1/schools/{school.id}/access/me/")
        self.assertEqual(response.status_code, 200)

        pull = api_client.get("/api/v1/sync/pull/", {"school": str(school.id), "since": 0})
        self.assertEqual(pull.status_code, 403)
