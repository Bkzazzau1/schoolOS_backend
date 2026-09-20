from apps.notifications.models import Notification

from .helpers import StaffTestCase
from .test_profile_rules import BANK, complete_details


class FromProposalToReviewedRegistrationTests(StaffTestCase):
    """The whole story, in order, the way the app will use it."""

    def test_the_full_journey(self):
        principal, owner, staff_login = self.members["principal"], self.owner, self.members["staff"]

        # 1. The principal proposes a teacher. Nothing exists yet, and the owner is told.
        self.ok(self.propose(who=principal, proposal_id="P1", name="Musa Ibrahim", roleTitle="Mathematics Teacher",
                             systemRole="teacher", phone="0803 123 4567", nin="12345678901",
                             email="musa@school.ng", gross=200000, deductions=20000))
        self.assertEqual(Notification.objects.filter(recipient=owner, kind="staff_proposal").count(), 1)
        # (The principal proposed it, but salaries are for the owner's eyes only.)
        self.client.force_authenticate(principal.user)
        self.assertEqual(self.client.get(f"/api/v1/owner/schools/{self.school.id}/records/owner_payroll_profile/").status_code, 403)

        # 2. Nobody can propose the same person again.
        self.rejected(self.propose(who=self.members["accountant"], phone="08031234567"), "already used by Musa Ibrahim")

        # 3. The owner approves. The salary is on payroll and the owner can read it back.
        staff_id = self.approve(owner, "P1").json()["staffId"]
        self.client.force_authenticate(owner.user)
        salary = self.client.get(f"/api/v1/owner/schools/{self.school.id}/records/owner_payroll_profile/").json()["records"]
        self.assertEqual([(r["entityId"], r["payload"]["gross"]) for r in salary], [(staff_id, 200000)])
        self.assertTrue(Notification.objects.filter(recipient=principal, kind="staff_proposal_decided").exists())

        # 4. Their login gets linked (what accepting the invitation will do). They register.
        self.link(staff_id, staff_login)
        self.ok(self.edit_profile(staff_id, staff_login, personal=complete_details(), payment=BANK, onboardingStatus="submitted"))

        # 5. The owner and principal are told, see everything, and mark it reviewed.
        self.assertEqual(Notification.objects.filter(kind="staff_registration_submitted").count(), 2)
        profile = self.profile(staff_id)
        self.assertEqual(profile["payment"]["accountNumber"], "0123456789")
        self.assertEqual(profile["personal"]["phone"], "08039876543")
        self.ok(self.edit_profile(staff_id, principal, onboardingStatus="reviewed"))
        self.assertEqual(self.profile(staff_id)["onboardingStatus"], "reviewed")

        # 6. Bank details stay the staff member's alone, and the person is still unique.
        self.rejected(self.edit_profile(staff_id, owner, payment={"bankName": "Other", "accountName": "X", "accountNumber": "9999999999"}),
                      "own bank details")
        self.rejected(self.propose(phone="08039876543"), "already used by Musa Ibrahim")

    def test_a_declined_person_can_be_proposed_again_later(self):
        self.ok(self.propose(proposal_id="P1", phone="08031234567", nin="12345678901"))
        self.reject(self.owner, "P1", "Not now")
        self.ok(self.propose(proposal_id="P2", phone="08031234567", nin="12345678901"))
        self.assertEqual(self.approve(self.owner, "P2").status_code, 200)
