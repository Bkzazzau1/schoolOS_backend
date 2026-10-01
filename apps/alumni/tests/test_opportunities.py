"""Alumni Jobs & Opportunities: a real posting board - any real alumnus may post, every real
alumnus sees every real posting, and only the poster or school management may close one."""

from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase

from ..models import AlumniOpportunity

User = get_user_model()


class AlumniOpportunityTests(StaffTestCase):
    def _other_alumnus(self):
        user = User.objects.create_user("other-alumnus@school.ng", "a-long-test-password-1")
        return Membership.objects.create(user=user, school=self.school, role=Role.ALUMNI)

    def _list(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/opportunities/")

    def _post(self, who, **body):
        self.client.force_authenticate(who.user)
        payload = {
            "title": "Software Engineer",
            "organisation": "Acme Co",
            "opportunityType": "full_time",
            "locationText": "Lagos",
            "description": "We are hiring a software engineer for our growing team.",
            "contactInfo": "jobs@acme.example",
        }
        payload.update(body)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/opportunities/", payload, format="json"
        )

    def _close(self, who, opportunity_id):
        self.client.force_authenticate(who.user)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/opportunities/{opportunity_id}/close/"
        )

    def _management(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/management/")

    def test_only_real_alumni_can_list_or_post(self):
        response = self._post(self.members["alumni"])
        self.assertEqual(response.status_code, 201, response.json())
        for role in ("teacher", "parent", "proprietor", "administrator", "principal", "student"):
            self.assertEqual(self._list(self.members[role]).status_code, 403, role)
            self.assertEqual(self._post(self.members[role]).status_code, 403, role)

    def test_a_posting_is_refused_without_a_real_title_organisation_type_or_description(self):
        self.assertEqual(self._post(self.members["alumni"], title="").status_code, 400)
        self.assertEqual(self._post(self.members["alumni"], organisation="").status_code, 400)
        self.assertEqual(self._post(self.members["alumni"], opportunityType="volunteer").status_code, 400)
        self.assertEqual(self._post(self.members["alumni"], description="hi").status_code, 400)

    def test_a_posting_needs_a_real_way_to_follow_up(self):
        response = self._post(
            self.members["alumni"],
            contactInfo="",
            description="Short desc.",
        )
        self.assertEqual(response.status_code, 400)

        response = self._post(
            self.members["alumni"],
            contactInfo="",
            description="A long enough description that stands on its own without contact info.",
        )
        self.assertEqual(response.status_code, 201, response.json())

    def test_every_real_alumnus_sees_every_real_posting_not_just_their_own(self):
        self._post(self.members["alumni"])
        other = self._other_alumnus()
        self._post(other, title="Marketing Intern")

        mine = self._list(self.members["alumni"]).json()["opportunities"]
        theirs = self._list(other).json()["opportunities"]
        self.assertEqual(len(mine), 2)
        self.assertEqual(len(theirs), 2)

    def test_a_new_posting_starts_open_with_the_real_poster_name(self):
        response = self._post(self.members["alumni"])
        opportunity = response.json()["opportunity"]
        self.assertEqual(opportunity["status"], "open")
        self.assertEqual(opportunity["postedByMembershipId"], str(self.members["alumni"].id))
        self.assertTrue(opportunity["postedByName"])

    def test_the_poster_can_close_their_own_posting(self):
        opportunity_id = self._post(self.members["alumni"]).json()["opportunity"]["id"]
        response = self._close(self.members["alumni"], opportunity_id)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["opportunity"]["status"], "closed")

    def test_an_unrelated_alumnus_cannot_close_someone_elses_posting(self):
        opportunity_id = self._post(self.members["alumni"]).json()["opportunity"]["id"]
        other = self._other_alumnus()
        self.assertEqual(self._close(other, opportunity_id).status_code, 403)

    def test_management_can_close_any_real_posting_for_moderation(self):
        opportunity_id = self._post(self.members["alumni"]).json()["opportunity"]["id"]
        for role in ("proprietor", "administrator", "principal"):
            response = self._post(self.members["alumni"], title=f"Role for {role}")
            opp_id = response.json()["opportunity"]["id"]
            close_response = self._close(self.members[role], opp_id)
            self.assertEqual(close_response.status_code, 200, (role, close_response.json()))
        # Unrelated staff can't moderate.
        self.assertEqual(self._close(self.members["teacher"], opportunity_id).status_code, 403)

    def test_closing_an_already_closed_posting_is_a_harmless_no_op(self):
        opportunity_id = self._post(self.members["alumni"]).json()["opportunity"]["id"]
        self._close(self.members["alumni"], opportunity_id)
        response = self._close(self.members["alumni"], opportunity_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["opportunity"]["status"], "closed")

    def test_a_forged_or_other_schools_opportunity_id_is_refused(self):
        ghost_id = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self._close(self.members["alumni"], ghost_id).status_code, 404)

        other_school_alumnus_user = User.objects.create_user("alumnus@other.ng", "a-long-test-password-1")
        other_school_alumnus = Membership.objects.create(
            user=other_school_alumnus_user, school=self.other_school, role=Role.ALUMNI
        )
        other_school_opportunity = AlumniOpportunity.objects.create(
            school=self.other_school,
            posted_by=other_school_alumnus,
            title="Role at other school",
            organisation="Other Co",
            opportunity_type="full_time",
            description="A posting for the other school's own alumni network.",
            contact_info="jobs@other.example",
        )
        self.assertEqual(self._close(self.members["alumni"], other_school_opportunity.id).status_code, 404)

    def test_alumni_management_carries_every_real_posting_with_the_real_poster_name(self):
        self._post(self.members["alumni"])
        response = self._management(self.members["proprietor"])
        self.assertEqual(response.status_code, 200, response.json())
        opportunities = response.json()["opportunities"]
        self.assertEqual(len(opportunities), 1)
        self.assertEqual(opportunities[0]["postedByMembershipId"], str(self.members["alumni"].id))
        self.assertTrue(opportunities[0]["postedByName"])

    def test_cross_school_opportunities_never_appear(self):
        other_school_alumnus_user = User.objects.create_user("alumnus2@other.ng", "a-long-test-password-1")
        other_school_alumnus = Membership.objects.create(
            user=other_school_alumnus_user, school=self.other_school, role=Role.ALUMNI
        )
        AlumniOpportunity.objects.create(
            school=self.other_school,
            posted_by=other_school_alumnus,
            title="Role at other school",
            organisation="Other Co",
            opportunity_type="full_time",
            description="A posting for the other school's own alumni network.",
            contact_info="jobs@other.example",
        )
        self._post(self.members["alumni"])
        response = self._list(self.members["alumni"])
        self.assertEqual(len(response.json()["opportunities"]), 1)
