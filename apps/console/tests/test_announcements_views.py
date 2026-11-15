"""The console's announcements page: platform operators write and target "pop messages"
shown to schools in the app - platform-staff-only, like every other console page."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.announcements.models import PlatformAnnouncement
from apps.announcements.services import create_announcement
from apps.billing.tests.base import BillingTestCase

User = get_user_model()
PASSWORD = "a-long-test-password-1"


class AnnouncementConsoleAccessTests(TestCase):
    def setUp(self):
        self.ordinary_user = User.objects.create_user("owner@school.ng", PASSWORD)
        self.staff_user = User.objects.create_user("ops@skulgen.com", PASSWORD, is_staff=True)

    def test_an_ordinary_account_cannot_reach_announcements(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.get(reverse("console:announcements"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_platform_staff_can_open_it(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("console:announcements"))
        self.assertEqual(response.status_code, 200)


class AnnouncementConsoleTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.staff_user = User.objects.create_user("ops-announce@skulgen.com", PASSWORD, is_staff=True)
        self.client.force_login(self.staff_user)

    def _url(self):
        return reverse("console:announcements")

    def test_sending_a_platform_wide_announcement(self):
        self.client.post(self._url(), {"action": "create", "title": "Maintenance", "body": "Downtime Friday night.", "organization_id": ""})
        announcement = PlatformAnnouncement.objects.get()
        self.assertEqual(announcement.title, "Maintenance")
        self.assertIsNone(announcement.organization_id)
        self.assertEqual(announcement.created_by, self.staff_user)

    def test_sending_an_organization_targeted_announcement(self):
        self.client.post(
            self._url(),
            {"action": "create", "title": "Your discount", "body": "10% off annual plans.", "organization_id": str(self.organization.id)},
        )
        announcement = PlatformAnnouncement.objects.get()
        self.assertEqual(announcement.organization_id, self.organization.id)

    def test_an_announcement_without_a_title_is_refused(self):
        self.client.post(self._url(), {"action": "create", "title": "", "body": "x", "organization_id": ""})
        self.assertFalse(PlatformAnnouncement.objects.exists())

    def test_ending_an_announcement_removes_it_from_the_active_list(self):
        announcement = create_announcement(title="Flash sale", body="Today only.")
        self.client.post(self._url(), {"action": "end", "announcement_id": str(announcement.id)})
        announcement.refresh_from_db()
        self.assertIsNotNone(announcement.effective_until)

        response = self.client.get(self._url())
        self.assertContains(response, "ended")
