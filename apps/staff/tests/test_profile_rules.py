from apps.notifications.models import Notification

from .helpers import StaffTestCase


def complete_details(**over):
    details = {
        "phone": "0803 987 6543", "nin": "98765432109", "email": "musa@school.ng",
        "address": "5 Ahmadu Bello Way, Kaduna", "dateOfBirth": "1990-05-14", "gender": "Male",
        "stateOfOrigin": "Kaduna", "nextOfKinName": "Hauwa Ibrahim", "nextOfKinPhone": "08055556666",
    }
    details.update(over)
    return details


BANK = {"bankName": "GTBank", "accountName": "Musa Ibrahim", "accountNumber": "0123456789"}


class EditorTests(StaffTestCase):
    """The owner and the principal edit a staff record, but not its bank details."""

    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff(name="Musa Ibrahim")
        self.principal = self.members["principal"]

    def test_the_owner_and_principal_edit_personal_details_academics_credentials_and_documents(self):
        for who in (self.owner, self.principal):
            response = self.edit_profile(
                self.staff_id, who,
                personal=complete_details(),
                academics=[{"level": "Master degree", "institution": "BUK", "course": "Physics", "year": 2018, "grade": "2:1"}],
                credentials=[{"title": "TRCN licence", "issuer": "TRCN", "number": "T-1", "expiry": "2030-01-01",
                              "verified": True, "documentRef": "Cabinet 2"}],
            )
            self.ok(response)
        p = self.profile(self.staff_id)
        self.assertEqual(p["personal"]["phone"], "08039876543")          # stored in one standard form
        self.assertEqual(p["academics"][0]["level"], "Master degree")
        self.assertTrue(p["credentials"][0]["verified"])
        self.assertEqual(p["updatedByMembershipId"], str(self.principal.id))

    def test_documents_can_be_updated_by_editors(self):
        docs = self.profile(self.staff_id)["documents"]
        docs[0].update(status="verified", reference="Folder 4")
        self.ok(self.edit_profile(self.staff_id, self.owner, documents=docs))
        self.assertEqual(self.profile(self.staff_id)["documents"][0]["status"], "verified")

    def test_nobody_but_the_staff_member_changes_bank_details(self):
        for who in (self.owner, self.principal, self.members["administrator"]):
            self.rejected(self.edit_profile(self.staff_id, who, payment=BANK), "own bank details")
        self.assertEqual(self.profile(self.staff_id)["payment"]["accountNumber"], "")

    def test_unchanged_saves_are_accepted_and_an_editor_can_re_save_bank_details_as_they_are(self):
        self.ok(self.edit_profile(self.staff_id, self.owner))
        self.link(self.staff_id, self.members["staff"])
        self.ok(self.edit_profile(self.staff_id, self.members["staff"], payment=BANK, personal=complete_details()))
        # An editor's app echoes the bank details back unchanged when saving something else.
        self.ok(self.edit_profile(self.staff_id, self.owner, personal={"address": "New address"}))
        self.assertEqual(self.profile(self.staff_id)["payment"], BANK)

    def test_the_server_owned_fields_cannot_be_set_by_the_app(self):
        self.ok(self.edit_profile(self.staff_id, self.owner, linkedMembershipId=str(self.members["parent"].id),
                                  systemRole="principal", submittedAt="2001", injected="x"))
        p = self.profile(self.staff_id)
        self.assertEqual((p["linkedMembershipId"], p["systemRole"]), ("", "teacher"))
        self.assertNotIn("injected", p)
        self.assertNotIn("submittedAt", p)

    def test_the_link_is_kept_when_editors_save(self):
        self.link(self.staff_id, self.members["staff"])
        self.ok(self.edit_profile(self.staff_id, self.owner, linkedMembershipId=""))
        self.assertEqual(self.profile(self.staff_id)["linkedMembershipId"], str(self.members["staff"].id))

    def test_bad_values_are_refused(self):
        bad = [
            ("personal", complete_details(phone="12345")),
            ("personal", complete_details(nin="123")),
            ("personal", complete_details(email="nope")),
            ("personal", complete_details(dateOfBirth="14/05/1990")),
            ("personal", complete_details(nextOfKinPhone="1")),
            ("academics", [{"level": "Wizard", "institution": "x", "course": "y", "year": 2000}]),
            ("academics", [{"level": "HND", "institution": "x", "course": "y", "year": 2999}]),
            ("academics", [{"level": "HND", "institution": "", "course": "y", "year": 2000}]),
            ("credentials", [{"title": "", "issuer": "x"}]),
            ("credentials", [{"title": "x", "issuer": "y", "expiry": "soon"}]),
            ("documents", [{"name": "CV", "status": "requested"}, {"name": "CV", "status": "received"}]),
            ("documents", [{"name": "CV", "status": "lost"}]),
        ]
        for section, value in bad:
            response = self.edit_profile(self.staff_id, self.owner, **{section: value})
            self.assertEqual(response.status_code, 422, (section, value))

    def test_the_profile_needs_a_staff_member_to_exist(self):
        response = self.push("owner_staff_profile", "STAFF-NOPE", {"staffId": "STAFF-NOPE"}, who=self.owner)
        self.rejected(response, "Add the staff member first")

    def test_a_profile_cannot_be_deleted(self):
        self.rejected(self.push("owner_staff_profile", self.staff_id, operation="delete", who=self.owner), "deleted")

    def test_the_staff_id_must_match_the_record(self):
        payload = self.profile(self.staff_id)
        payload["staffId"] = "STAFF-OTHER"
        self.rejected(self.push("owner_staff_profile", self.staff_id, payload, operation="update", who=self.owner), "staffId")


class WhoElseTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff()

    def test_the_administrator_cannot_edit_a_staff_record(self):
        admin = self.members["administrator"]
        self.rejected(self.edit_profile(self.staff_id, admin, personal=complete_details()), "cannot change")
        self.rejected(self.edit_profile(self.staff_id, admin, credentials=[{"title": "x", "issuer": "y"}]), "owner or principal")

    def test_everyone_else_is_refused_outright(self):
        for role in ["accountant", "teacher", "parent", "student"]:
            self.rejected(self.edit_profile(self.staff_id, self.members[role], personal=complete_details()), "role")
        # Even a staff-role login that is not linked to this record.
        self.rejected(self.edit_profile(self.staff_id, self.members["staff"], personal=complete_details()), "role")

    def test_a_login_linked_to_someone_else_cannot_edit_this_record(self):
        other = self.make_staff()
        self.link(other, self.members["staff"])
        self.rejected(self.edit_profile(self.staff_id, self.members["staff"], personal=complete_details()), "role")

    def test_another_schools_owner_cannot_touch_it(self):
        payload = self.profile(self.staff_id)
        response = self.push("owner_staff_profile", self.staff_id, payload, operation="update",
                             who=self.other_owner, school=self.other_school)
        self.rejected(response)  # there is no such record in their school


class ReviewTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff()
        self.principal = self.members["principal"]

    def review(self, who, reviews):
        return self.edit_profile(self.staff_id, who, reviews=reviews)

    def test_the_owner_and_principal_add_reviews_and_the_server_says_who_and_when(self):
        r1 = {"period": "Term 1", "rating": 4, "notes": "Strong", "reviewerRole": "proprietor",
              "reviewerMembershipId": "fake", "at": "2001-01-01"}
        self.ok(self.review(self.principal, [r1]))
        stored = self.profile(self.staff_id)["reviews"][0]
        self.assertEqual((stored["reviewerRole"], stored["reviewerMembershipId"]), ("principal", str(self.principal.id)))
        self.assertNotEqual(stored["at"], "2001-01-01")
        self.ok(self.review(self.owner, self.profile(self.staff_id)["reviews"] + [{"period": "Term 2", "rating": 5, "notes": ""}]))
        second = self.profile(self.staff_id)["reviews"][1]
        self.assertEqual(second["reviewerRole"], "proprietor")
        self.assertEqual(self.profile(self.staff_id)["reviews"][0]["reviewerRole"], "principal")  # unchanged

    def test_reviews_only_ever_grow(self):
        self.ok(self.review(self.principal, [{"period": "T1", "rating": 4, "notes": "a"}]))
        stored = self.profile(self.staff_id)["reviews"]
        rewritten = [{**stored[0], "rating": 1}]
        self.rejected(self.review(self.owner, rewritten), "only added")
        self.rejected(self.review(self.owner, []), "only added")
        self.rejected(self.review(self.owner, [{**stored[0], "notes": "changed"}]), "only added")
        self.assertEqual(self.profile(self.staff_id)["reviews"][0]["rating"], 4)

    def test_only_the_owner_and_principal_may_add_them(self):
        review = [{"period": "T1", "rating": 3, "notes": ""}]
        self.rejected(self.review(self.members["administrator"], review), "owner or principal")
        self.link(self.staff_id, self.members["staff"])
        self.rejected(self.review(self.members["staff"], review), "owner or principal")   # not their own review
        self.assertEqual(self.profile(self.staff_id)["reviews"], [])

    def test_ratings_and_periods_are_checked(self):
        for review in [{"period": "T1", "rating": 0}, {"period": "T1", "rating": 6}, {"period": "T1", "rating": "5"},
                       {"period": "T1", "rating": True}, {"period": "", "rating": 3}]:
            self.assertEqual(self.review(self.owner, [review]).status_code, 422, review)


class IdentityUniquenessTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_staff(name="Aisha Bello", phone="08031111111", nin="11111111111")
        self.b = self.make_staff(name="Bala Sani", phone="08032222222", nin="22222222222")

    def test_a_phone_or_nin_cannot_be_taken_from_someone_else_however_it_is_typed(self):
        for phone in ["08031111111", "0803 111 1111", "+234 803 111 1111", "2348031111111"]:
            self.rejected(self.edit_profile(self.b, self.owner, personal={"phone": phone}), "already used by Aisha Bello")
        self.rejected(self.edit_profile(self.b, self.owner, personal={"nin": "111-1111-1111"}), "NIN is already used by Aisha Bello")
        self.assertEqual(self.profile(self.b)["personal"]["phone"], "08032222222")

    def test_changing_your_own_number_releases_the_old_one(self):
        self.ok(self.edit_profile(self.a, self.owner, personal={"phone": "08039999999"}))
        self.ok(self.edit_profile(self.b, self.owner, personal={"phone": "08031111111"}))   # Aisha's old number is free
        self.rejected(self.edit_profile(self.a, self.owner, personal={"phone": "08031111111"}), "already used by Bala Sani")

    def test_keeping_your_own_number_is_never_a_clash(self):
        self.ok(self.edit_profile(self.a, self.owner, personal={"phone": "0803 111 1111", "address": "x"}))

    def test_the_staff_member_cannot_take_someone_elses_number_either(self):
        self.link(self.b, self.members["staff"])
        self.rejected(self.edit_profile(self.b, self.members["staff"], personal={"phone": "08031111111"}), "already used by Aisha Bello")

    def test_clearing_a_number_frees_it(self):
        self.ok(self.edit_profile(self.a, self.owner, personal={"phone": ""}))
        self.ok(self.edit_profile(self.b, self.owner, personal={"phone": "08031111111"}))

    def test_a_new_proposal_cannot_reuse_a_current_staff_number(self):
        self.rejected(self.propose(phone="08031111111"), "already used by Aisha Bello")

    def test_a_race_between_two_devices_undoes_the_loser_completely(self):
        """Both devices pass the check before either has saved. The database still
        allows only one holder, and the loser's whole write is rolled back."""
        from unittest import mock

        from apps.staff.models import IdentityClaim

        version = self.stored("owner_staff_profile", self.b).version
        with mock.patch("apps.staff.profiles.handler.identity.check_available"):
            # Someone else claims the number between the check and the save.
            IdentityClaim.objects.filter(kind="phone", value="08031111111").update(holder_id="STAFF-RACE", holder_name="Racer")
            response = self.edit_profile(self.b, self.owner, personal={"phone": "08031111111"})
        self.rejected(response, "already used by Racer")
        after = self.stored("owner_staff_profile", self.b)
        self.assertEqual(after.version, version)
        self.assertEqual(after.payload["personal"]["phone"], "08032222222")
        self.assertEqual(IdentityClaim.objects.get(kind="phone", holder_id=self.b).value, "08032222222")

    def test_the_database_itself_refuses_two_holders(self):
        from django.db import IntegrityError, transaction

        from apps.staff.models import IdentityClaim

        with self.assertRaises(IntegrityError), transaction.atomic():
            IdentityClaim.objects.create(school=self.school, kind="phone", value="08031111111",
                                         holder_type="staff", holder_id="STAFF-EVIL", holder_name="Evil")
        # The same number in another school is fine.
        IdentityClaim.objects.create(school=self.other_school, kind="phone", value="08031111111",
                                     holder_type="staff", holder_id="STAFF-1", holder_name="Someone")

    def test_staff_who_were_never_given_numbers_do_not_collide_with_each_other(self):
        self.ok(self.edit_profile(self.a, self.owner, personal={"phone": "", "nin": ""}))
        self.ok(self.edit_profile(self.b, self.owner, personal={"phone": "", "nin": ""}))


class NotificationTests(StaffTestCase):
    def test_owner_and_principal_hear_when_a_staff_member_submits(self):
        staff_id = self.make_staff(name="Musa Ibrahim")
        self.link(staff_id, self.members["staff"])
        Notification.objects.all().delete()
        self.ok(self.edit_profile(staff_id, self.members["staff"], personal=complete_details(), payment=BANK,
                                  onboardingStatus="submitted"))
        told = set(Notification.objects.filter(kind="staff_registration_submitted").values_list("recipient__role", flat=True))
        self.assertEqual(told, {"proprietor", "principal"})
        self.assertIn("Musa Ibrahim", Notification.objects.filter(recipient=self.owner).first().message)
