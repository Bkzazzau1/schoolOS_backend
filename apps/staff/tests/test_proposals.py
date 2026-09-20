from apps.notifications.models import Notification
from apps.staff.models import IdentityClaim
from apps.sync.models import SyncRecord

from .helpers import StaffTestCase, fresh_nin, fresh_phone


class WhoCanProposeTests(StaffTestCase):
    def test_the_owner_principal_administrator_and_finance_officer_can_propose(self):
        for role in ["proprietor", "principal", "administrator", "accountant"]:
            self.ok(self.propose(who=self.members[role]))

    def test_nobody_else_can(self):
        for role in ["teacher", "staff", "parent", "student"]:
            self.rejected(self.propose(who=self.members[role]), "role")
        self.assertEqual(SyncRecord.objects.count(), 0)
        self.assertEqual(IdentityClaim.objects.count(), 0)

    def test_the_server_sets_who_proposed_and_the_status_whatever_the_app_sends(self):
        self.ok(self.propose(proposal_id="P1", status="approved", proposedByMembershipId="someone-else",
                             proposedByRole="proprietor", proposedAt="2001-01-01", createdStaffId="STAFF-X",
                             injected="x"))
        p = self.stored("staff_proposal", "P1").payload
        self.assertEqual(p["status"], "pending")
        self.assertEqual((p["proposedByMembershipId"], p["proposedByRole"]), (str(self.members["principal"].id), "principal"))
        self.assertNotEqual(p["proposedAt"], "2001-01-01")
        self.assertNotIn("createdStaffId", p)
        self.assertNotIn("injected", p)

    def test_a_proposal_creates_no_staff_no_salary_and_no_profile(self):
        self.ok(self.propose())
        types = set(SyncRecord.objects.values_list("entity_type", flat=True))
        self.assertEqual(types, {"staff_proposal"})

    def test_a_proposal_cannot_be_edited_or_deleted_through_sync(self):
        self.ok(self.propose(proposal_id="P1"))
        edit = self.push("staff_proposal", "P1", {**self.proposal_payload(), "status": "approved"},
                         operation="update", who=self.members["principal"])
        self.rejected(edit, "cannot be changed")
        self.rejected(self.push("staff_proposal", "P1", operation="delete", who=self.members["principal"]), "deleted")
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "pending")

    def test_the_owner_is_told_about_a_new_proposal(self):
        self.ok(self.propose(name="Musa Ibrahim"))
        message = Notification.objects.get(recipient=self.owner)
        self.assertEqual((message.kind, message.title), ("staff_proposal", "New staff proposal"))
        self.assertIn("Musa Ibrahim", message.message)
        self.assertNotIn("200000", message.message)  # money is not put in a notification
        self.assertEqual(Notification.objects.exclude(recipient=self.owner).count(), 0)


class ProposalValidationTests(StaffTestCase):
    def bad(self, contains, **over):
        self.rejected(self.propose(**over), contains)
        self.assertEqual(SyncRecord.objects.count(), 0)
        self.assertEqual(IdentityClaim.objects.count(), 0)

    def test_phone_and_nin_are_required_and_checked(self):
        self.bad("phone is required", phone="")
        self.bad("phone number", phone="12345")
        self.bad("phone number", phone="0603 123 4567")
        self.bad("nin is required", nin="")
        self.bad("NIN", nin="123")
        self.bad("NIN", nin="abcdefghijk")

    def test_the_email_is_required_because_the_invitation_goes_there(self):
        self.bad("email", email="")
        self.bad("email", email="not-an-email")

    def test_the_salary_must_make_sense(self):
        self.bad("gross", gross=0)
        self.bad("gross", gross=-5)
        self.bad("gross", gross="200000")
        self.bad("gross", gross=True)
        self.bad("gross", gross=1_000_000_001)
        self.bad("deductions", gross=100, deductions=101)
        self.bad("deductions", deductions=-1)

    def test_the_role_must_be_one_a_person_can_be_appointed_to(self):
        for role in ["proprietor", "parent", "student", "emperor", "", "Teacher"]:
            self.bad("systemRole", systemRole=role)
        for role in ["teacher", "staff", "accountant", "administrator", "principal"]:
            self.ok(self.propose(systemRole=role))

    def test_name_title_and_area_are_required(self):
        self.bad("name", name=" ")
        self.bad("roleTitle", roleTitle="")
        self.bad("workArea", workArea="")

    def test_numbers_are_stored_in_one_standard_form(self):
        self.ok(self.propose(proposal_id="P1", phone="+234 803 999 8888", nin="123 4567 8901", email="Musa@School.NG"))
        p = self.stored("staff_proposal", "P1").payload
        self.assertEqual((p["phone"], p["nin"], p["email"]), ("08039998888", "12345678901", "musa@school.ng"))


class ProposalIdentityTests(StaffTestCase):
    def test_the_same_person_cannot_be_proposed_twice_however_the_number_is_typed(self):
        self.ok(self.propose(name="Musa Ibrahim", phone="0803 123 4567", nin="11111111111"))
        for phone in ["08031234567", "+234 803 123 4567", "2348031234567", "(0803) 123-4567"]:
            self.rejected(self.propose(phone=phone), "already used by Musa Ibrahim")
        self.rejected(self.propose(nin="111-1111-1111"), "NIN is already used by Musa Ibrahim")
        self.assertEqual(SyncRecord.objects.filter(entity_type="staff_proposal").count(), 1)

    def test_the_message_says_it_is_a_pending_proposal(self):
        self.ok(self.propose(phone="08031234567"))
        response = self.propose(phone="08031234567")
        self.rejected(response, "a pending proposal")

    def test_both_clashes_are_reported_at_once(self):
        self.ok(self.propose(phone="08031234567", nin="11111111111"))
        response = self.propose(phone="08031234567", nin="11111111111")
        message = response.json()["message"]
        self.assertIn("phone number", message)
        self.assertIn("NIN", message)

    def test_a_person_can_hold_their_number_only_once_even_across_different_names(self):
        self.ok(self.propose(name="A", phone="08031234567"))
        self.rejected(self.propose(name="B", phone="08031234567"), "already used by A")

    def test_a_rejected_proposal_frees_the_numbers(self):
        self.ok(self.propose(proposal_id="P1", phone="08031234567", nin="11111111111"))
        self.assertEqual(self.reject(self.owner, "P1", "No vacancy").status_code, 200)
        self.ok(self.propose(phone="08031234567", nin="11111111111"))

    def test_another_school_can_use_the_same_numbers(self):
        self.ok(self.propose(phone="08031234567", nin="11111111111"))
        other = self.push("staff_proposal", "P9", self.proposal_payload(phone="08031234567", nin="11111111111"),
                          who=self.other_principal, school=self.other_school)
        self.ok(other)

    def test_a_proposal_holds_exactly_the_numbers_it_was_made_with(self):
        self.ok(self.propose(proposal_id="P1", phone="08031234567", nin="11111111111"))
        claims = {(c.kind, c.value, c.holder_type, c.holder_id) for c in IdentityClaim.objects.all()}
        self.assertEqual(claims, {("phone", "08031234567", "proposal", "P1"), ("nin", "11111111111", "proposal", "P1")})

    def test_different_numbers_are_fine(self):
        self.ok(self.propose(phone=fresh_phone(), nin=fresh_nin()))
        self.ok(self.propose(phone=fresh_phone(), nin=fresh_nin()))
