"""Stage 4, Task 2 probe (tenant isolation): does a billing-only OrganizationMembership
(no school Membership at all) get refused by a real, non-billing, school-operational API -
not just hidden by the Flutter UI? Written to prove the boundary with direct API calls, not
to assert anything about the Flutter layer."""

from rest_framework.test import APIClient

from .base import BillingTestCase


class BillingAdminCannotReadSchoolOperationalDataTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user = self.billing_administrator()

    def _client_as(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_cannot_list_students(self):
        client = self._client_as(self.admin_user)
        response = client.get(f"/api/v1/schools/{self.school.id}/students/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_cannot_read_roster_summary(self):
        client = self._client_as(self.admin_user)
        response = client.get(f"/api/v1/schools/{self.school.id}/roster/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_cannot_read_admissions(self):
        client = self._client_as(self.admin_user)
        response = client.get(f"/api/v1/schools/{self.school.id}/admissions/")
        self.assertEqual(response.status_code, 403, response.content)

    def test_a_real_school_administrator_can_list_students_for_contrast(self):
        # Sanity check that the view itself works and the 403s above are really about the
        # billing-only role, not a broken URL/fixture.
        client = self._client_as(self.owner)
        result = client.get(f"/api/v1/schools/{self.school.id}/students/")
        self.assertEqual(result.status_code, 200, result.content)
