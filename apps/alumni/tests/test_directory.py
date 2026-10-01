"""Alumni Directory: every real, verified, directory-visible alumnus - nothing else, and never a
private field (admission number, original student reference, email)."""

from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase

from ..models import AlumniProfile, AlumniVerificationStatus

User = get_user_model()


class AlumniDirectoryTests(StaffTestCase):
    def _alumnus(self, email, school=None):
        user = User.objects.create_user(email, "a-long-test-password-1")
        return Membership.objects.create(user=user, school=school or self.school, role=Role.ALUMNI)

    def _profile(self, membership, **over):
        defaults = dict(
            membership=membership,
            school=membership.school,
            verification_status=AlumniVerificationStatus.VERIFIED,
            directory_visible=True,
            graduation_year=2015,
            graduation_set="Class of 2015",
            profession="Engineer",
            organisation="Acme Co",
            location_text="Lagos",
            bio="Hello.",
        )
        defaults.update(over)
        return AlumniProfile.objects.create(**defaults)

    def _get(self, who, **params):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/directory/", params)

    def test_a_verified_directory_visible_alumnus_appears_without_private_fields(self):
        self._profile(self.members["alumni"])
        response = self._get(self.members["alumni"])
        self.assertEqual(response.status_code, 200, response.json())
        entries = response.json()["entries"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["profession"], "Engineer")
        self.assertEqual(entry["graduationYear"], 2015)
        for private_field in ("admissionNumber", "admission_number", "email", "originalStudentReference"):
            self.assertNotIn(private_field, entry)

    def test_an_unverified_profile_is_excluded(self):
        unverified = self._alumnus("unverified@school.ng")
        self._profile(unverified, verification_status=AlumniVerificationStatus.PENDING, directory_visible=False)
        response = self._get(self.members["alumni"])
        self.assertEqual(response.json()["entries"], [])

    def test_a_verified_but_not_directory_visible_profile_is_excluded(self):
        hidden = self._alumnus("hidden@school.ng")
        self._profile(hidden, directory_visible=False)
        response = self._get(self.members["alumni"])
        self.assertEqual(response.json()["entries"], [])

    def test_cross_school_alumni_never_appear(self):
        other_alumnus = self._alumnus("other-alumnus@school.ng", school=self.other_school)
        self._profile(other_alumnus)
        response = self._get(self.members["alumni"])
        self.assertEqual(response.json()["entries"], [])

    def test_a_non_alumni_role_cannot_browse_the_directory(self):
        self._profile(self.members["alumni"])
        for role in ("teacher", "parent", "proprietor", "administrator", "principal"):
            response = self._get(self.members[role])
            self.assertEqual(response.status_code, 403, role)

    def test_search_and_graduation_year_filters(self):
        self._profile(self.members["alumni"], graduation_year=2015, profession="Engineer")
        second = self._alumnus("second-alumnus@school.ng")
        self._profile(second, graduation_year=2020, profession="Doctor", organisation="City Hospital")

        by_year = self._get(self.members["alumni"], graduationYear="2020")
        self.assertEqual(len(by_year.json()["entries"]), 1)
        self.assertEqual(by_year.json()["entries"][0]["profession"], "Doctor")

        by_query = self._get(self.members["alumni"], q="hospital")
        self.assertEqual(len(by_query.json()["entries"]), 1)
        self.assertEqual(by_query.json()["entries"][0]["organisation"], "City Hospital")
