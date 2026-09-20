from .helpers import StaffTestCase
from .test_profile_rules import BANK, complete_details


class StaffMemberSelfServiceTests(StaffTestCase):
    """What the staff member can do to their own record once their login is linked."""

    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff(name="Musa Ibrahim", email="musa@school.ng")
        self.me = self.members["staff"]
        self.link(self.staff_id, self.me)

    def submit(self, **sections):
        base = dict(personal=complete_details(), payment=BANK, onboardingStatus="submitted")
        base.update(sections)
        return self.edit_profile(self.staff_id, self.me, **base)

    # -- the registration form ---------------------------------------------------------

    def test_completing_the_form_submits_it_for_review(self):
        docs = self.profile(self.staff_id)["documents"]
        docs[0].update(status="received", reference="passport.jpg emailed to admin")
        docs[4].update(status="received", reference="Handed to the office")
        self.ok(self.submit(documents=docs))
        p = self.profile(self.staff_id)
        self.assertEqual(p["onboardingStatus"], "submitted")
        self.assertIn("submittedAt", p)
        self.assertEqual(p["payment"], BANK)
        self.assertEqual((p["personal"]["phone"], p["personal"]["nin"]), ("08039876543", "98765432109"))
        self.assertEqual([d["status"] for d in p["documents"]], ["received", "requested", "requested", "requested", "received", "requested"])
        self.assertEqual(p["linkedMembershipId"], str(self.me.id))   # unchanged

    def test_an_incomplete_form_cannot_be_submitted(self):
        for missing in ["phone", "nin", "address", "dateOfBirth", "nextOfKinName", "nextOfKinPhone"]:
            response = self.submit(personal=complete_details(**{missing: ""}))
            self.rejected(response, "Enter your phone, NIN, address")
        for bank in [{}, {**BANK, "accountNumber": ""}, {**BANK, "bankName": ""}, {**BANK, "accountName": ""}]:
            self.rejected(self.submit(payment=bank), "bank")
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")

    def test_bank_details_are_checked(self):
        for number in ["123", "12345678901", "abcdefghij", "0123 45678"]:
            self.rejected(self.submit(payment={**BANK, "accountNumber": number}), "10-digit")
        # Spaces in an otherwise valid number are tidied away.
        self.ok(self.submit(payment={**BANK, "accountNumber": "0123 456 789"}))
        self.assertEqual(self.profile(self.staff_id)["payment"]["accountNumber"], "0123456789")

    def test_it_cannot_be_submitted_when_no_request_is_open(self):
        for status in ["none", "reviewed"]:
            record = self.stored("owner_staff_profile", self.staff_id)
            record.payload = {**record.payload, "onboardingStatus": status}
            record.save()
            self.rejected(self.submit(), "no open registration request")

    def test_sending_it_again_changes_nothing_and_does_not_notify_twice(self):
        from apps.notifications.models import Notification

        self.ok(self.submit())
        submitted_at = self.profile(self.staff_id)["submittedAt"]
        self.ok(self.submit())   # a retry after a lost response
        self.assertEqual(self.profile(self.staff_id)["submittedAt"], submitted_at)
        self.assertEqual(Notification.objects.filter(kind="staff_registration_submitted", recipient=self.owner).count(), 1)
        # The person can still keep their own details current afterwards.
        self.ok(self.edit_profile(self.staff_id, self.me, personal={"address": "New address"}, onboardingStatus="submitted"))

    def test_they_can_keep_their_bank_details_and_contact_details_up_to_date(self):
        self.ok(self.submit())
        self.ok(self.edit_profile(self.staff_id, self.me, payment={**BANK, "bankName": "Zenith"}, onboardingStatus="submitted"))
        self.assertEqual(self.profile(self.staff_id)["payment"]["bankName"], "Zenith")

    # -- what they cannot do -----------------------------------------------------------

    def test_the_school_controls_their_employment_terms(self):
        for change in [{"employmentDate": "2020-01-01"}, {"employmentType": "Full-time"}]:
            self.rejected(self.edit_profile(self.staff_id, self.me, personal=change), "set by the school")

    def test_they_cannot_touch_academics_credentials_or_reviews(self):
        self.rejected(self.edit_profile(self.staff_id, self.me, academics=[
            {"level": "Doctorate (PhD)", "institution": "x", "course": "y", "year": 2000}]), "owner or principal")
        self.rejected(self.edit_profile(self.staff_id, self.me, credentials=[
            {"title": "TRCN", "issuer": "TRCN", "verified": True}]), "owner or principal")
        self.rejected(self.edit_profile(self.staff_id, self.me, reviews=[{"period": "T1", "rating": 5}]), "owner or principal")

    def test_they_can_only_mark_requested_documents_as_provided(self):
        docs = self.profile(self.staff_id)["documents"]
        # Verifying their own document, or providing one without saying how:
        for change in [dict(status="verified", reference="x"), dict(status="received", reference="")]:
            mutated = [dict(d) for d in docs]
            mutated[0].update(change)
            self.rejected(self.edit_profile(self.staff_id, self.me, documents=mutated), "mark a requested document")
        # Adding or removing documents:
        self.rejected(self.edit_profile(self.staff_id, self.me, documents=docs + [{"name": "Extra", "status": "requested", "reference": ""}]), "add or remove")
        self.rejected(self.edit_profile(self.staff_id, self.me, documents=docs[:-1]), "add or remove")
        # Undoing something the school already verified:
        verified = [dict(d) for d in docs]
        verified[0].update(status="verified", reference="Folder 1")
        self.ok(self.edit_profile(self.staff_id, self.owner, documents=verified))
        downgrade = [dict(d) for d in verified]
        downgrade[0].update(status="requested", reference="")
        self.rejected(self.edit_profile(self.staff_id, self.me, documents=downgrade), "mark a requested document")

    def test_they_cannot_mark_their_own_registration_reviewed_or_change_the_request(self):
        self.rejected(self.edit_profile(self.staff_id, self.me, onboardingStatus="reviewed"), "owner or principal")
        self.rejected(self.edit_profile(self.staff_id, self.me, onboardingEmail="other@x.ng"), "cannot send")
        self.rejected(self.edit_profile(self.staff_id, self.me, onboardingStatus="none"), "not allowed")
        self.rejected(self.edit_profile(self.staff_id, self.me, onboardingStatus="invitePending", onboardingEmail="x@y.ng"), "cannot send")

    def test_someone_who_is_not_linked_cannot_submit_for_them(self):
        for who in (self.owner, self.members["principal"], self.members["administrator"], self.members["teacher"]):
            response = self.edit_profile(self.staff_id, who, personal=complete_details(), payment=BANK, onboardingStatus="submitted")
            self.assertEqual(response.status_code, 422, who.role)
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "invitePending")


class RegistrationRequestTests(StaffTestCase):
    """Sending, reviewing and re-sending the registration request."""

    def setUp(self):
        super().setUp()
        self.staff_id = self.make_staff(email="musa@school.ng")
        self.me = self.members["staff"]
        self.link(self.staff_id, self.me)

    def set_status(self, status, email=None):
        from apps.sync.models import SyncRecord

        record = self.stored("owner_staff_profile", self.staff_id)
        record.payload = {**record.payload, "onboardingStatus": status, **({} if email is None else {"onboardingEmail": email})}
        record.save()

    def test_the_owner_or_principal_mark_a_submitted_registration_reviewed(self):
        self.set_status("submitted")
        self.ok(self.edit_profile(self.staff_id, self.members["principal"], onboardingStatus="reviewed"))
        self.assertEqual(self.profile(self.staff_id)["onboardingStatus"], "reviewed")

    def test_reviewing_needs_an_editor_and_an_existing_request(self):
        self.set_status("submitted")
        self.rejected(self.edit_profile(self.staff_id, self.members["administrator"], onboardingStatus="reviewed"), "owner or principal")
        self.set_status("none")
        self.rejected(self.edit_profile(self.staff_id, self.owner, onboardingStatus="reviewed"), "No registration request")

    def test_owner_principal_and_administrator_can_send_or_resend_the_request(self):
        for who in (self.owner, self.members["principal"], self.members["administrator"]):
            self.set_status("reviewed")
            self.ok(self.edit_profile(self.staff_id, who, onboardingStatus="invitePending", onboardingEmail="New@School.NG"))
            p = self.profile(self.staff_id)
            self.assertEqual((p["onboardingStatus"], p["onboardingEmail"]), ("invitePending", "new@school.ng"))

    def test_a_request_needs_a_valid_email(self):
        self.set_status("reviewed")
        self.rejected(self.edit_profile(self.staff_id, self.owner, onboardingStatus="invitePending", onboardingEmail=""), "email")
        self.rejected(self.edit_profile(self.staff_id, self.owner, onboardingStatus="invitePending", onboardingEmail="nope"), "email")

    def test_a_request_may_only_add_the_standard_documents(self):
        admin = self.members["administrator"]
        self.set_status("reviewed")
        docs = self.profile(self.staff_id)["documents"]
        self.rejected(self.edit_profile(self.staff_id, admin, onboardingStatus="invitePending", onboardingEmail="a@b.ng",
                                        documents=docs + [{"name": "Secret file", "status": "requested", "reference": ""}]),
                      "standard required documents")
        weakened = [dict(d) for d in docs]
        weakened[0].update(status="verified", reference="x")
        self.rejected(self.edit_profile(self.staff_id, admin, onboardingStatus="invitePending", onboardingEmail="a@b.ng",
                                        documents=weakened), "standard required documents")

    def test_a_profile_for_an_owner_added_person_can_be_created_by_an_inviter(self):
        payload = {"id": "STAFF-200", "name": "Mr. Bala", "role": "Driver", "section": "Transport", "fileStatus": "Missing document"}
        self.ok(self.push("administrator_staff_directory", "STAFF-200", payload, who=self.owner))
        from apps.staff.constants import DEFAULT_DOCUMENTS

        profile = {
            "staffId": "STAFF-200", "personal": {}, "academics": [], "credentials": [], "reviews": [], "payment": {},
            "documents": [{"name": n, "status": "requested", "reference": ""} for n in DEFAULT_DOCUMENTS],
            "onboardingStatus": "invitePending", "onboardingEmail": "bala@school.ng",
        }
        self.ok(self.push("owner_staff_profile", "STAFF-200", profile, who=self.members["administrator"]))
        self.rejected(self.push("owner_staff_profile", "STAFF-200", profile, who=self.members["teacher"]), "role")
        stored = self.profile("STAFF-200")
        self.assertEqual((stored["linkedMembershipId"], stored["systemRole"]), ("", ""))
