from rest_framework.test import APITestCase

from apps.billing.tests.base import verified_user
from apps.organizations.services import create_organization, provision_school
from apps.schools.models import SchoolType

from ..services import create_announcement


class ActiveAnnouncementsViewTests(APITestCase):
    def setUp(self):
        self.owner = verified_user("owner@example.ng")
        self.organization, _ = create_organization(actor=self.owner, name="BrightGate Group")
        self.school, self.membership = provision_school(
            actor=self.owner, organization_id=self.organization.id, name="BrightGate Academy",
            school_type=SchoolType.SECONDARY, location="Kaduna",
        )
        self.other_owner = verified_user("other@example.ng")
        self.other_organization, _ = create_organization(actor=self.other_owner, name="Other Group")
        self.other_school, self.other_membership = provision_school(
            actor=self.other_owner, organization_id=self.other_organization.id, name="Other Academy",
            school_type=SchoolType.SECONDARY, location="Kano",
        )

    def _get(self, school_id, who):
        self.client.force_authenticate(who)
        return self.client.get(f"/api/v1/schools/{school_id}/announcements/active/")

    def test_a_platform_wide_announcement_shows_up_for_every_school(self):
        create_announcement(title="Platform notice", body="Scheduled maintenance Friday.")
        response = self._get(self.school.id, self.owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["announcements"]), 1)
        self.assertEqual(response.json()["announcements"][0]["title"], "Platform notice")

    def test_an_organization_targeted_announcement_never_reaches_another_organization(self):
        create_announcement(title="Just for you", body="Your renewal discount.", organization=self.organization)
        mine = self._get(self.school.id, self.owner).json()["announcements"]
        theirs = self._get(self.other_school.id, self.other_owner).json()["announcements"]
        self.assertEqual([a["title"] for a in mine], ["Just for you"])
        self.assertEqual(theirs, [])

    def test_an_unauthenticated_request_is_refused(self):
        create_announcement(title="Platform notice", body="x")
        response = self.client.get(f"/api/v1/schools/{self.school.id}/announcements/active/")
        self.assertEqual(response.status_code, 401)

    def test_someone_from_another_school_cannot_read_this_schools_announcements(self):
        response = self._get(self.school.id, self.other_owner)
        self.assertEqual(response.status_code, 403)
