from django.contrib.auth import get_user_model

from apps.staff.tests.helpers import StaffTestCase

from ..models import (
    AssociationAdministrator,
    AssociationMembershipStatus,
    AssociationStatus,
    SchoolAssociationMembership,
    SchoolProprietorAssociation,
)

PASSWORD = "correct horse battery staple"
User = get_user_model()


class AssociationTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.association = SchoolProprietorAssociation.objects.create(
            name="Kaduna Private Schools Association", status=AssociationStatus.ACTIVE
        )
        self.unverified_association = SchoolProprietorAssociation.objects.create(
            name="New Association", status=AssociationStatus.PENDING_VERIFICATION
        )
        self.admin_user = User.objects.create_user("admin@association.ng", PASSWORD)
        AssociationAdministrator.objects.create(association=self.association, user=self.admin_user)

    def join(self, association=None, who=None, school=None):
        who = who or self.owner
        self.client.force_authenticate(who.user)
        association = association or self.association
        school = school or self.school
        return self.client.post(
            f"/api/v1/schools/{school.id}/transferverify/associations/{association.id}/join/"
        )

    def as_admin(self):
        self.client.force_authenticate(self.admin_user)


class CatalogTests(AssociationTestCase):
    def test_only_active_associations_are_in_the_public_catalog(self):
        self.client.force_authenticate(self.owner.user)
        response = self.client.get("/api/v1/transferverify/associations/")
        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["associations"]}
        self.assertIn(self.association.name, names)
        self.assertNotIn(self.unverified_association.name, names)


class JoiningTests(AssociationTestCase):
    def test_the_owner_requests_to_join_and_it_starts_pending(self):
        response = self.join()
        self.assertEqual(response.status_code, 201)
        body = response.json()["membership"]
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["schoolId"], str(self.school.id))

        listed = self.client.get(f"/api/v1/schools/{self.school.id}/transferverify/associations/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["memberships"]), 1)

    def test_only_the_owner_can_request_to_join(self):
        response = self.join(who=self.members["accountant"])
        self.assertEqual(response.status_code, 403)

    def test_joining_an_unverified_association_is_refused(self):
        response = self.join(association=self.unverified_association)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not currently accepting members", response.json()["message"])

    def test_requesting_twice_while_pending_is_refused(self):
        self.join()
        response = self.join()
        self.assertEqual(response.status_code, 400)
        self.assertIn("already has a request", response.json()["message"])

    def test_a_school_never_sees_another_schools_membership(self):
        self.join()
        self.client.force_authenticate(self.other_owner.user)
        listed = self.client.get(f"/api/v1/schools/{self.other_school.id}/transferverify/associations/")
        self.assertEqual(listed.json()["memberships"], [])


class AdministrationTests(AssociationTestCase):
    def _approve(self, membership_id, who=None):
        self.client.force_authenticate((who or self.admin_user))
        return self.client.post(
            f"/api/v1/transferverify/associations/{self.association.id}/members/{membership_id}/approve/"
        )

    def test_the_association_administrator_approves_a_pending_request(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        response = self.client.post(
            f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/approve/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["membership"]["status"], "active")
        row.refresh_from_db()
        self.assertEqual(row.status, AssociationMembershipStatus.ACTIVE)
        self.assertIsNotNone(row.decided_at)

    def test_someone_who_is_not_an_administrator_of_this_association_cannot_approve(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        response = self._approve(row.id, who=self.owner.user)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not an administrator", response.json()["message"])

    def test_approving_twice_is_refused(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/approve/")
        response = self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/approve/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Only a pending request", response.json()["message"])

    def test_rejecting_a_pending_request_ends_it_without_deleting_it(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        response = self.client.post(
            f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/reject/",
            {"note": "Registration reference could not be verified."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["membership"]["status"], "exited")
        self.assertTrue(SchoolAssociationMembership.objects.filter(id=row.id).exists())

    def test_suspending_requires_an_active_membership_first(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        too_early = self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/suspend/")
        self.assertEqual(too_early.status_code, 400)
        self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/approve/")
        suspended = self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/suspend/")
        self.assertEqual(suspended.status_code, 200)
        self.assertEqual(suspended.json()["membership"]["status"], "suspended")

    def test_a_membership_row_cannot_be_administered_through_the_wrong_associations_url(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        response = self.client.post(
            f"/api/v1/transferverify/associations/{self.unverified_association.id}/members/{row.id}/approve/"
        )
        self.assertEqual(response.status_code, 400)


class ExitTests(AssociationTestCase):
    def test_the_owner_exits_an_active_membership(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.as_admin()
        self.client.post(f"/api/v1/transferverify/associations/{self.association.id}/members/{row.id}/approve/")

        self.client.force_authenticate(self.owner.user)
        response = self.client.post(
            f"/api/v1/schools/{self.school.id}/transferverify/associations/memberships/{row.id}/exit/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["membership"]["status"], "exited")

    def test_a_school_cannot_exit_another_schools_membership(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.client.force_authenticate(self.other_owner.user)
        response = self.client.post(
            f"/api/v1/schools/{self.other_school.id}/transferverify/associations/memberships/{row.id}/exit/"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_school_can_re_request_after_exiting_and_it_reopens_the_same_row(self):
        self.join()
        row = SchoolAssociationMembership.objects.get(association=self.association, school=self.school)
        self.client.force_authenticate(self.owner.user)
        self.client.post(f"/api/v1/schools/{self.school.id}/transferverify/associations/memberships/{row.id}/exit/")

        response = self.join()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["membership"]["id"], str(row.id))
        self.assertEqual(
            SchoolAssociationMembership.objects.filter(association=self.association, school=self.school).count(), 1
        )
