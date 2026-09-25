"""Who may open Alumni Management: Proprietor, Administrator and Principal - no one else."""

from apps.staff.tests.helpers import StaffTestCase


class AlumniManagementAccessTests(StaffTestCase):
    def _get_management(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/management/")

    def test_proprietor_administrator_and_principal_can_all_open_alumni_management(self):
        for role in ("proprietor", "administrator", "principal"):
            response = self._get_management(self.members[role])
            self.assertEqual(response.status_code, 200, (role, response.json()))
            self.assertIn("profiles", response.json())

    def test_a_teacher_cannot_open_alumni_management(self):
        response = self._get_management(self.members["teacher"])
        self.assertEqual(response.status_code, 403)
