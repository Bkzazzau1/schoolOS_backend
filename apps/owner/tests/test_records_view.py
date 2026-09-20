from django.contrib.auth import get_user_model

from apps.schools.models import Membership, Role, School

from .helpers import PASSWORD, OwnerTestCase
from .test_salary import salary


class OwnerRecordsViewTests(OwnerTestCase):
    entity_type = "owner_payroll_profile"

    def url(self, entity_type="owner_payroll_profile", school=None):
        return f"/api/v1/owner/schools/{(school or self.school).id}/records/{entity_type}/"

    def get(self, who=None, **kwargs):
        self.client.force_authenticate(who.user if who else None)
        return self.client.get(self.url(**kwargs))

    def test_the_owner_loads_their_records_of_one_kind(self):
        self.push("STAFF-001", salary())
        self.push("STAFF-002", {**salary(), "staffId": "STAFF-002", "name": "Mr. Ahmad Sani"})
        self.push("J1", {"unrelated": 1}, entity_type="owner_job_assignment")  # refused, so absent
        response = self.get(self.owner)
        self.assertEqual(response.status_code, 200)
        records = response.json()["records"]
        self.assertEqual([r["entityId"] for r in records], ["STAFF-001", "STAFF-002"])
        first = records[0]
        self.assertEqual((first["version"], first["deleted"]), (1, False))
        self.assertEqual(first["payload"]["gross"], 250000)
        self.assertIn("updatedAt", first)

    def test_an_empty_list_is_fine(self):
        self.assertEqual(self.get(self.owner).json(), {"records": []})

    def test_nobody_but_the_owner_may_read_them(self):
        self.push("STAFF-001", salary())
        for role, member in self.members.items():
            if role != "proprietor":
                self.assertEqual(self.get(member).status_code, 403, role)

    def test_signing_in_is_required(self):
        self.assertEqual(self.get(None).status_code, 401)

    def test_an_owner_cannot_read_another_schools_records(self):
        self.push("STAFF-001", salary())
        other = School.objects.create(name="Other", slug="other")
        user = get_user_model().objects.create_user("owner@other.ng", PASSWORD)
        other_owner = Membership.objects.create(user=user, school=other, role=Role.PROPRIETOR)
        self.assertEqual(self.get(other_owner).status_code, 403)
        self.assertEqual(self.get(other_owner, school=other).json(), {"records": []})

    def test_an_inactive_owner_membership_is_refused(self):
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.get(self.owner).status_code, 403)

    def test_only_owner_record_types_can_be_read_this_way(self):
        self.assertEqual(self.get(self.owner, entity_type="school_event").status_code, 404)
        self.assertEqual(self.get(self.owner, entity_type="administrator_staff_directory").status_code, 404)

    def test_each_kind_is_listed_separately(self):
        self.push("STAFF-001", salary())
        self.assertEqual(self.get(self.owner, entity_type="owner_payroll_authorizer").json(), {"records": []})
        self.assertEqual(self.get(self.owner, entity_type="owner_job_assignment").json(), {"records": []})
