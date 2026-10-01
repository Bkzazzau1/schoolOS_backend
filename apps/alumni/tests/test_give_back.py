"""Alumni Give Back: a real, non-monetary offer of help from a real alumnus (volunteering,
mentoring, supplies, guest speaking) - one-sided, never a public board, never real money."""

from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role
from apps.staff.tests.helpers import StaffTestCase

from ..models import AlumniPledge

User = get_user_model()


class AlumniGiveBackTests(StaffTestCase):
    def _other_alumnus(self):
        user = User.objects.create_user("other-alumnus@school.ng", "a-long-test-password-1")
        return Membership.objects.create(user=user, school=self.school, role=Role.ALUMNI)

    def _list(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/give-back/")

    def _create(self, who, **body):
        self.client.force_authenticate(who.user)
        payload = {"category": "volunteering", "description": "I can help run the careers fair."}
        payload.update(body)
        return self.client.post(f"/api/v1/alumni/schools/{self.school.id}/give-back/", payload, format="json")

    def _withdraw(self, who, pledge_id):
        self.client.force_authenticate(who.user)
        return self.client.post(f"/api/v1/alumni/schools/{self.school.id}/give-back/{pledge_id}/withdraw/")

    def _set_status(self, who, pledge_id, **body):
        self.client.force_authenticate(who.user)
        payload = {"status": "acknowledged"}
        payload.update(body)
        return self.client.post(
            f"/api/v1/alumni/schools/{self.school.id}/give-back/{pledge_id}/status/", payload, format="json"
        )

    def _management(self, who):
        self.client.force_authenticate(who.user)
        return self.client.get(f"/api/v1/alumni/schools/{self.school.id}/management/")

    def test_only_real_alumni_can_list_or_create_a_pledge(self):
        response = self._create(self.members["alumni"])
        self.assertEqual(response.status_code, 201, response.json())
        for role in ("teacher", "parent", "proprietor", "administrator", "principal", "student"):
            self.assertEqual(self._list(self.members[role]).status_code, 403, role)
            self.assertEqual(self._create(self.members[role]).status_code, 403, role)

    def test_an_alumnus_only_ever_sees_their_own_pledges(self):
        self._create(self.members["alumni"])
        other = self._other_alumnus()
        self._create(other, description="I can donate textbooks for the library.")

        mine = self._list(self.members["alumni"]).json()["pledges"]
        theirs = self._list(other).json()["pledges"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 1)
        self.assertNotEqual(mine[0]["id"], theirs[0]["id"])

    def test_a_pledge_is_refused_without_a_real_category_or_description(self):
        self.assertEqual(self._create(self.members["alumni"], category="").status_code, 400)
        self.assertEqual(self._create(self.members["alumni"], category="money").status_code, 400)
        self.assertEqual(self._create(self.members["alumni"], description="hi").status_code, 400)

    def test_a_new_pledge_starts_offered_with_the_real_alumni_name(self):
        response = self._create(self.members["alumni"])
        pledge = response.json()["pledge"]
        self.assertEqual(pledge["status"], "offered")
        self.assertEqual(pledge["alumniMembershipId"], str(self.members["alumni"].id))
        self.assertTrue(pledge["alumniName"])

    def test_only_the_pledges_own_alumnus_can_withdraw_it(self):
        pledge_id = self._create(self.members["alumni"]).json()["pledge"]["id"]
        other = self._other_alumnus()
        self.assertEqual(self._withdraw(other, pledge_id).status_code, 404)
        response = self._withdraw(self.members["alumni"], pledge_id)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["pledge"]["status"], "withdrawn")

    def test_only_management_can_acknowledge_or_fulfil_never_withdraw(self):
        pledge_id = self._create(self.members["alumni"]).json()["pledge"]["id"]
        self.assertEqual(self._set_status(self.members["alumni"], pledge_id).status_code, 403)
        self.assertEqual(self._set_status(self.members["teacher"], pledge_id).status_code, 403)

        for role in ("proprietor", "administrator", "principal"):
            response = self._set_status(self.members[role], pledge_id, status="acknowledged", schoolNote=f"Seen by {role}.")
            self.assertEqual(response.status_code, 200, (role, response.json()))

        self.assertEqual(
            self._set_status(self.members["proprietor"], pledge_id, status="withdrawn").status_code, 400
        )

    def test_a_status_update_carries_the_real_school_note(self):
        pledge_id = self._create(self.members["alumni"]).json()["pledge"]["id"]
        self._set_status(self.members["proprietor"], pledge_id, status="acknowledged", schoolNote="Thank you!")
        response = self._set_status(self.members["proprietor"], pledge_id, status="fulfilled", schoolNote="Confirmed for careers day.")
        self.assertEqual(response.json()["pledge"]["schoolNote"], "Confirmed for careers day.")
        self.assertEqual(response.json()["pledge"]["status"], "fulfilled")

    def test_a_forged_or_other_schools_pledge_id_is_refused_on_every_endpoint(self):
        ghost_id = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self._withdraw(self.members["alumni"], ghost_id).status_code, 404)
        self.assertEqual(self._set_status(self.members["proprietor"], ghost_id).status_code, 404)

        other_school_alumnus_user = User.objects.create_user("alumnus@other.ng", "a-long-test-password-1")
        other_school_alumnus = Membership.objects.create(
            user=other_school_alumnus_user, school=self.other_school, role=Role.ALUMNI
        )
        other_school_pledge = AlumniPledge.objects.create(
            school=self.other_school,
            membership=other_school_alumnus,
            category="volunteering",
            description="Helping out at the other school.",
        )
        self.assertEqual(self._withdraw(self.members["alumni"], other_school_pledge.id).status_code, 404)
        self.assertEqual(self._set_status(self.members["proprietor"], other_school_pledge.id).status_code, 404)

    def test_alumni_management_carries_every_real_pledge_with_the_real_alumni_name(self):
        self._create(self.members["alumni"])
        response = self._management(self.members["proprietor"])
        self.assertEqual(response.status_code, 200, response.json())
        pledges = response.json()["pledges"]
        self.assertEqual(len(pledges), 1)
        self.assertEqual(pledges[0]["alumniMembershipId"], str(self.members["alumni"].id))
        self.assertTrue(pledges[0]["alumniName"])

    def test_cross_school_pledges_never_appear_in_management(self):
        other_school_alumnus_user = User.objects.create_user("alumnus2@other.ng", "a-long-test-password-1")
        other_school_alumnus = Membership.objects.create(
            user=other_school_alumnus_user, school=self.other_school, role=Role.ALUMNI
        )
        AlumniPledge.objects.create(
            school=self.other_school,
            membership=other_school_alumnus,
            category="volunteering",
            description="Helping out at the other school.",
        )
        self._create(self.members["alumni"])
        response = self._management(self.members["proprietor"])
        self.assertEqual(len(response.json()["pledges"]), 1)
