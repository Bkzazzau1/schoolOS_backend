"""Alumni Mentorship: alumni mentoring alumni only. A mentor directory, a request the mentor
accepts or declines, and real contact email revealed only once accepted - never before."""

from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase

from ..models import AlumniMentorProfile, AlumniMentorshipRequest

User = get_user_model()


class AlumniMentorshipTests(StaffTestCase):
    def _other_alumnus(self, email="other-alumnus@school.ng"):
        user = User.objects.create_user(email, "a-long-test-password-1")
        return Membership.objects.create(user=user, school=self.school, role=Role.ALUMNI)

    def _become_mentor(self, who=None, **body):
        who = who or self.members["alumni"]
        self.client.force_authenticate(who.user)
        payload = {"expertise": "Software Engineering", "bio": "Happy to help with career advice."}
        payload.update(body)
        return self.client.put(f"/api/v1/alumni/schools/{self.school.id}/mentorship/me/", payload, format="json")

    def _my_profile(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/mentorship/me/")

    def _directory(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/mentorship/mentors/")

    def _list_requests(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/mentorship/requests/")

    def _request(self, who, mentor, **body):
        self.client.force_authenticate(who.user)
        payload = {"mentorMembershipId": str(mentor.id), "message": "Could you help me with my career?"}
        payload.update(body)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/mentorship/requests/", payload, format="json"
        )

    def _respond(self, who, request_id, resolution):
        self.client.force_authenticate(who.user)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/mentorship/requests/{request_id}/respond/",
            {"status": resolution},
            format="json",
        )

    def _withdraw(self, who, request_id):
        self.client.force_authenticate(who.user)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/mentorship/requests/{request_id}/withdraw/"
        )

    def test_only_real_alumni_can_opt_in_as_a_mentor(self):
        response = self._become_mentor()
        self.assertEqual(response.status_code, 200, response.json())
        for role in ("teacher", "parent", "proprietor", "administrator", "principal", "student"):
            self.assertEqual(self._become_mentor(self.members[role]).status_code, 403, role)

    def test_a_mentor_profile_requires_real_expertise_and_bio(self):
        self.assertEqual(self._become_mentor(expertise="").status_code, 400)
        self.assertEqual(self._become_mentor(bio="hi").status_code, 400)

    def test_my_mentor_profile_is_honestly_null_until_opted_in(self):
        response = self._my_profile(self.members["alumni"])
        self.assertIsNone(response.json()["mentorProfile"])
        self._become_mentor()
        response = self._my_profile(self.members["alumni"])
        self.assertEqual(response.json()["mentorProfile"]["expertise"], "Software Engineering")

    def test_only_active_mentors_appear_in_the_directory(self):
        self._become_mentor()
        other = self._other_alumnus()
        self._become_mentor(other, expertise="Finance", isActive=False)

        response = self._directory(self.members["alumni"])
        mentors = response.json()["mentors"]
        self.assertEqual(len(mentors), 1)
        self.assertEqual(mentors[0]["expertise"], "Software Engineering")

    def test_the_directory_never_carries_contact_info(self):
        self._become_mentor()
        response = self._directory(self.members["alumni"])
        mentor = response.json()["mentors"][0]
        self.assertNotIn("email", mentor)
        self.assertNotIn("contactInfo", mentor)

    def test_only_real_alumni_can_browse_or_request(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        for role in ("teacher", "parent", "proprietor", "administrator", "principal", "student"):
            self.assertEqual(self._directory(self.members[role]).status_code, 403, role)
            self.assertEqual(self._request(self.members[role], mentor).status_code, 403, role)

    def test_a_request_needs_a_real_currently_active_mentor(self):
        mentee = self._other_alumnus()
        ghost_mentor = self._other_alumnus("ghost-mentor@school.ng")
        response = self._request(mentee, ghost_mentor)
        self.assertEqual(response.status_code, 400, response.json())

    def test_an_alumnus_cannot_request_themselves(self):
        self._become_mentor()
        response = self._request(self.members["alumni"], self.members["alumni"])
        self.assertEqual(response.status_code, 400)

    def test_a_second_simultaneous_pending_request_is_refused_but_a_new_one_after_a_decline_is_allowed(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()

        first = self._request(mentee, mentor)
        self.assertEqual(first.status_code, 201, first.json())
        self.assertEqual(self._request(mentee, mentor).status_code, 400)

        request_id = first.json()["request"]["id"]
        self._respond(mentor, request_id, "declined")
        second = self._request(mentee, mentor)
        self.assertEqual(second.status_code, 201, second.json())

    def test_only_the_named_mentor_can_accept_or_decline(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        request_id = self._request(mentee, mentor).json()["request"]["id"]

        self.assertEqual(self._respond(mentee, request_id, "accepted").status_code, 404)
        unrelated = self._other_alumnus("unrelated@school.ng")
        self.assertEqual(self._respond(unrelated, request_id, "accepted").status_code, 404)

        response = self._respond(mentor, request_id, "accepted")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["request"]["status"], "accepted")

    def test_contact_email_is_absent_until_accepted_then_present_for_both_sides(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        request_id = self._request(mentee, mentor).json()["request"]["id"]

        pending = self._list_requests(mentor).json()["requests"][0]
        self.assertIsNone(pending["mentorEmail"])
        self.assertIsNone(pending["menteeEmail"])

        self._respond(mentor, request_id, "accepted")
        accepted = self._list_requests(mentee).json()["requests"][0]
        self.assertEqual(accepted["mentorEmail"], mentor.user.email)
        self.assertEqual(accepted["menteeEmail"], mentee.user.email)

    def test_declining_never_reveals_contact_email(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        request_id = self._request(mentee, mentor).json()["request"]["id"]
        self._respond(mentor, request_id, "declined")
        declined = self._list_requests(mentee).json()["requests"][0]
        self.assertIsNone(declined["mentorEmail"])
        self.assertIsNone(declined["menteeEmail"])

    def test_a_request_already_answered_cannot_be_answered_again(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        request_id = self._request(mentee, mentor).json()["request"]["id"]
        self._respond(mentor, request_id, "accepted")
        response = self._respond(mentor, request_id, "declined")
        self.assertEqual(response.status_code, 400)

    def test_only_the_mentee_can_withdraw_and_only_while_pending(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        request_id = self._request(mentee, mentor).json()["request"]["id"]

        self.assertEqual(self._withdraw(mentor, request_id).status_code, 404)

        response = self._withdraw(mentee, request_id)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["request"]["status"], "withdrawn")

        self.assertEqual(self._withdraw(mentee, request_id).status_code, 400)

    def test_a_forged_or_other_schools_request_id_is_refused(self):
        self._become_mentor()
        ghost_id = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self._respond(self.members["alumni"], ghost_id, "accepted").status_code, 404)
        self.assertEqual(self._withdraw(self.members["alumni"], ghost_id).status_code, 404)

        other_school_mentor_user = User.objects.create_user("mentor@other.ng", "a-long-test-password-1")
        other_school_mentor = Membership.objects.create(
            user=other_school_mentor_user, school=self.other_school, role=Role.ALUMNI
        )
        other_school_mentee_user = User.objects.create_user("mentee@other.ng", "a-long-test-password-1")
        other_school_mentee = Membership.objects.create(
            user=other_school_mentee_user, school=self.other_school, role=Role.ALUMNI
        )
        AlumniMentorProfile.objects.create(
            membership=other_school_mentor,
            school=self.other_school,
            expertise="Finance",
            bio="Happy to help.",
        )
        other_school_request = AlumniMentorshipRequest.objects.create(
            school=self.other_school,
            mentor=other_school_mentor,
            mentee=other_school_mentee,
            message="Hello.",
        )
        self.assertEqual(
            self._respond(self.members["alumni"], other_school_request.id, "accepted").status_code, 404
        )

    def test_mentorship_requests_never_appear_in_alumni_management(self):
        self._become_mentor()
        mentor = self.members["alumni"]
        mentee = self._other_alumnus()
        self._request(mentee, mentor)
        self.client.force_authenticate(self.members["proprietor"].user)
        response = self.client.get(f"/api/v1/alumni/schools/{self.school.id}/management/")
        self.assertNotIn("mentorshipRequests", response.json())
        self.assertNotIn("mentors", response.json())

    def test_cross_school_mentors_never_appear_in_the_directory(self):
        other_school_mentor_user = User.objects.create_user("mentor2@other.ng", "a-long-test-password-1")
        other_school_mentor = Membership.objects.create(
            user=other_school_mentor_user, school=self.other_school, role=Role.ALUMNI
        )
        AlumniMentorProfile.objects.create(
            membership=other_school_mentor,
            school=self.other_school,
            expertise="Finance",
            bio="Happy to help.",
        )
        self._become_mentor()
        response = self._directory(self.members["alumni"])
        self.assertEqual(len(response.json()["mentors"]), 1)
