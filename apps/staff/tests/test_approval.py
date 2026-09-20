from unittest import mock

from apps.notifications.models import Notification
from apps.owner.payroll.salary import SalaryProfileHandler
from apps.staff.models import IdentityClaim
from apps.staff.signals import staff_approved
from apps.sync import registry
from apps.sync.models import SyncRecord

from .helpers import StaffTestCase


class OwnerApprovalTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.propose(proposal_id="P1", name="Musa Ibrahim", roleTitle="Mathematics Teacher",
                             systemRole="teacher", workArea="Secondary", email="musa@school.ng",
                             phone="08031234567", nin="11111111111", gross=200000, deductions=20000))

    def approve_p1(self, who=None, body=None):
        return self.approve(who or self.owner, "P1", body)

    def test_approving_makes_them_staff_with_a_salary_and_a_registration_request(self):
        response = self.approve_p1()
        self.assertEqual(response.status_code, 200, response.json())
        staff_id = response.json()["staffId"]
        self.assertTrue(staff_id.startswith("STAFF-"))
        self.assertFalse(response.json()["alreadyApproved"])

        directory = self.stored("administrator_staff_directory", staff_id).payload
        self.assertEqual((directory["name"], directory["role"], directory["section"], directory["systemRole"]),
                         ("Musa Ibrahim", "Mathematics Teacher", "Secondary", "teacher"))
        salary = self.stored("owner_payroll_profile", staff_id).payload
        self.assertEqual((salary["gross"], salary["deductions"], salary["onPayroll"]), (200000, 20000, True))
        self.assertEqual(salary["history"][0]["byMembershipId"], str(self.owner.id))
        profile = self.stored("owner_staff_profile", staff_id).payload
        self.assertEqual((profile["onboardingStatus"], profile["onboardingEmail"], profile["systemRole"]),
                         ("invitePending", "musa@school.ng", "teacher"))
        self.assertEqual((profile["personal"]["phone"], profile["personal"]["nin"]), ("08031234567", "11111111111"))
        self.assertEqual(len(profile["documents"]), 6)
        self.assertEqual(profile["linkedMembershipId"], "")
        proposal = self.stored("staff_proposal", "P1").payload
        self.assertEqual((proposal["status"], proposal["createdStaffId"], proposal["decidedByMembershipId"]),
                         ("approved", staff_id, str(self.owner.id)))

    def test_the_records_the_server_writes_are_ones_the_app_can_later_edit_through_sync(self):
        staff_id = self.approve_p1().json()["staffId"]
        # The salary record passes the owner feature's own rules.
        record = self.stored("owner_payroll_profile", staff_id)
        ctx = registry.MutationContext(
            membership=self.owner, operation="update", entity_type="owner_payroll_profile", entity_id=staff_id,
            payload=record.payload, existing=record.payload, now="2026-09-21T00:00:00+00:00",
        )
        self.assertEqual(SalaryProfileHandler().clean(ctx)["gross"], 200000)
        # The profile can be saved back unchanged by an editor.
        self.ok(self.edit_profile(staff_id, self.owner))
        self.ok(self.edit_profile(staff_id, self.members["principal"]))

    def test_nothing_is_created_before_approval(self):
        types = set(SyncRecord.objects.values_list("entity_type", flat=True))
        self.assertEqual(types, {"staff_proposal"})

    def test_the_owner_may_change_the_salary_and_the_role(self):
        response = self.approve_p1(body={"gross": 300000, "deductions": 30000, "systemRole": "accountant"})
        staff_id = response.json()["staffId"]
        self.assertEqual(self.stored("owner_payroll_profile", staff_id).payload["gross"], 300000)
        self.assertEqual(self.stored("owner_staff_profile", staff_id).payload["systemRole"], "accountant")
        self.assertEqual(self.stored("staff_proposal", "P1").payload["approvedSystemRole"], "accountant")
        self.assertEqual(self.stored("staff_proposal", "P1").payload["systemRole"], "teacher")  # what was asked

    def test_bad_changes_are_refused_and_nothing_happens(self):
        for body in [{"gross": 0}, {"gross": 100, "deductions": 200}, {"systemRole": "proprietor"},
                     {"systemRole": "parent"}, {"gross": 2_000_000_000}]:
            response = self.approve_p1(body=body)
            self.assertEqual(response.status_code, 400, body)
            self.assertEqual(response.json()["code"], "rejected")
        self.assertEqual(SyncRecord.objects.filter(entity_type="owner_payroll_profile").count(), 0)
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "pending")

    def test_approving_twice_is_harmless_and_creates_no_duplicate(self):
        first = self.approve_p1().json()["staffId"]
        again = self.approve_p1()
        self.assertEqual(again.status_code, 200)
        self.assertEqual((again.json()["staffId"], again.json()["alreadyApproved"]), (first, True))
        self.assertEqual(SyncRecord.objects.filter(entity_type="administrator_staff_directory").count(), 1)
        self.assertEqual(self.stored("owner_payroll_profile", first).version, 1)

    def test_the_numbers_pass_from_the_proposal_to_the_staff_member(self):
        staff_id = self.approve_p1().json()["staffId"]
        claims = {(c.kind, c.value, c.holder_type, c.holder_id, c.holder_name) for c in IdentityClaim.objects.all()}
        self.assertEqual(claims, {("phone", "08031234567", "staff", staff_id, "Musa Ibrahim"),
                                  ("nin", "11111111111", "staff", staff_id, "Musa Ibrahim")})
        # So the same person cannot be proposed again now that they are staff.
        self.rejected(self.propose(phone="08031234567"), "already used by Musa Ibrahim")

    def test_the_proposer_is_told_and_the_invitation_feature_is_signalled(self):
        received = []
        staff_approved.connect(lambda sender, **kw: received.append(kw), weak=False, dispatch_uid="t1")
        self.addCleanup(staff_approved.disconnect, dispatch_uid="t1")
        staff_id = self.approve_p1().json()["staffId"]
        message = Notification.objects.get(recipient=self.members["principal"], kind="staff_proposal_decided")
        self.assertIn("Musa Ibrahim was approved as Teacher", message.message)
        self.assertEqual(len(received), 1)
        self.assertEqual(
            (received[0]["staff_id"], received[0]["email"], received[0]["system_role"], received[0]["proposal_id"]),
            (staff_id, "musa@school.ng", "teacher", "P1"),
        )

    def test_a_failure_halfway_leaves_nothing_changed(self):
        from apps.sync import records

        real, calls = records.write, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:  # after the directory and the salary are written
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        with mock.patch("apps.staff.proposals.service.records.write", flaky):
            with self.assertRaises(RuntimeError):
                self.approve_p1()
        self.assertEqual(set(SyncRecord.objects.values_list("entity_type", flat=True)), {"staff_proposal"})
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "pending")
        self.assertEqual({c.holder_type for c in IdentityClaim.objects.all()}, {"proposal"})
        self.assertEqual(Notification.objects.filter(kind="staff_proposal_decided").count(), 0)
        # And it can simply be tried again.
        self.assertEqual(self.approve_p1().status_code, 200)

    def test_the_result_appears_on_devices_as_new_versions(self):
        version = self.stored("staff_proposal", "P1").version
        self.approve_p1()
        self.assertEqual(self.stored("staff_proposal", "P1").version, version + 1)


class WhoMayDecideTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.propose(proposal_id="P1", systemRole="teacher"))

    def test_people_with_no_authority_get_a_403_and_nothing_changes(self):
        for role in ["principal", "administrator", "accountant", "teacher", "staff", "parent", "student"]:
            self.assertEqual(self.approve(self.members[role], "P1").status_code, 403, role)
            self.assertEqual(self.reject(self.members[role], "P1").status_code, 403, role)
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "pending")

    def test_signing_in_is_required_and_other_schools_owners_have_no_power(self):
        self.assertEqual(self.approve(None, "P1").status_code, 401)
        self.assertEqual(self.decide("approve", self.other_owner, "P1", school=self.school).status_code, 403)
        self.assertEqual(self.decide("approve", self.other_owner, "P1", school=self.other_school).status_code, 400)  # not found there

    def test_an_assigned_approver_can_approve_teachers_and_staff(self):
        approver = self.members["accountant"]
        self.assign_approver(approver)
        self.assertEqual(self.approve(approver, "P1").status_code, 200)
        self.ok(self.propose(proposal_id="P2", systemRole="staff"))
        self.assertEqual(self.approve(approver, "P2").status_code, 200)

    def test_an_assigned_approver_cannot_approve_roles_that_reach_money_or_records(self):
        approver = self.members["teacher"]
        self.assign_approver(approver)
        for i, role in enumerate(["accountant", "administrator", "principal"]):
            self.ok(self.propose(proposal_id=f"R{i}", systemRole=role))
            response = self.approve(approver, f"R{i}")
            self.assertEqual(response.status_code, 400, role)
            self.assertIn("Only the owner can approve", response.json()["message"])
            self.assertEqual(self.stored("staff_proposal", f"R{i}").payload["status"], "pending")
        self.assertEqual(SyncRecord.objects.filter(entity_type="administrator_staff_directory").count(), 0)
        # The owner still can.
        self.assertEqual(self.approve(self.owner, "R0").status_code, 200)

    def test_an_assigned_approver_cannot_change_the_salary_or_the_role(self):
        approver = self.members["accountant"]
        self.assign_approver(approver)
        for body, fragment in [({"gross": 999999}, "salary"), ({"deductions": 1}, "salary"),
                               ({"systemRole": "staff"}, "role")]:
            response = self.approve(approver, "P1", body)
            self.assertEqual(response.status_code, 400, body)
            self.assertIn(fragment, response.json()["message"])
        # Sending the proposed figures unchanged is fine.
        self.assertEqual(self.approve(approver, "P1", {"gross": 200000, "deductions": 20000, "systemRole": "teacher"}).status_code, 200)

    def test_nobody_decides_on_their_own_proposal_except_the_owner(self):
        approver = self.members["principal"]
        self.assign_approver(approver)
        self.ok(self.propose(who=approver, proposal_id="MINE"))
        for action in ("approve", "reject"):
            response = self.decide(action, approver, "MINE")
            self.assertEqual(response.status_code, 400, action)
            self.assertIn("someone else", response.json()["message"])
        self.assertEqual(self.stored("staff_proposal", "MINE").payload["status"], "pending")
        # The owner may decide on their own proposal.
        self.ok(self.propose(who=self.owner, proposal_id="OWNERS"))
        self.assertEqual(self.approve(self.owner, "OWNERS").status_code, 200)

    def test_an_assignment_only_counts_when_active_linked_to_them_and_for_this_authority(self):
        approver, other = self.members["accountant"], self.members["administrator"]
        cases = [
            dict(status="pendingActivation"),                       # nobody has claimed it yet
            dict(status="revoked"),                                 # taken away
            dict(linked_to=other),                                  # linked to someone else
            dict(authorities=("approve", "pay", "view", "prepare")),  # payroll powers, not staff approval
        ]
        for i, case in enumerate(cases):
            SyncRecord.objects.filter(entity_type="owner_payroll_authorizer").delete()
            self.assign_approver(approver, **case)
            self.assertEqual(self.approve(approver, "P1").status_code, 403, case)
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "pending")

    def test_a_deleted_assignment_gives_no_power(self):
        approver = self.members["accountant"]
        self.assign_approver(approver)
        SyncRecord.objects.filter(entity_type="owner_payroll_authorizer").update(deleted=True)
        self.assertEqual(self.approve(approver, "P1").status_code, 403)

    def test_an_assignment_from_another_school_gives_no_power_here(self):
        approver = self.members["accountant"]
        SyncRecord.objects.create(
            school=self.other_school, entity_type="owner_payroll_authorizer", entity_id="X",
            payload={"status": "active", "membershipId": str(approver.id), "authorities": ["approveStaff"]},
        )
        self.assertEqual(self.approve(approver, "P1").status_code, 403)

    def test_a_person_with_two_roles_must_say_which_one_is_acting(self):
        from apps.schools.models import Membership

        both = Membership.objects.create(user=self.members["accountant"].user, school=self.school, role="parent")
        self.assign_approver(self.members["accountant"])
        response = self.approve(self.members["accountant"], "P1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "membership_required")
        self.client.force_authenticate(both.user)
        path = f"/api/v1/staff/schools/{self.school.id}/proposals/P1/approve/?membership={self.members['accountant'].id}"
        self.assertEqual(self.client.post(path, {}, format="json").status_code, 200)


class RejectionTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.propose(proposal_id="P1", name="Musa Ibrahim"))

    def test_rejecting_records_the_reason_tells_the_proposer_and_frees_the_numbers(self):
        self.assertEqual(self.reject(self.owner, "P1", "No vacancy").status_code, 200)
        p = self.stored("staff_proposal", "P1").payload
        self.assertEqual((p["status"], p["decisionNote"], p["decidedByMembershipId"]), ("rejected", "No vacancy", str(self.owner.id)))
        self.assertEqual(IdentityClaim.objects.count(), 0)
        message = Notification.objects.get(recipient=self.members["principal"], kind="staff_proposal_decided")
        self.assertIn("was not approved. Reason: No vacancy", message.message)
        self.assertEqual(SyncRecord.objects.filter(entity_type="administrator_staff_directory").count(), 0)

    def test_a_decided_proposal_cannot_be_decided_again(self):
        self.reject(self.owner, "P1")
        self.assertEqual(self.reject(self.owner, "P1").status_code, 400)
        self.assertEqual(self.approve(self.owner, "P1").status_code, 400)
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "rejected")

    def test_an_approved_proposal_cannot_be_rejected(self):
        self.approve(self.owner, "P1")
        response = self.reject(self.owner, "P1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored("staff_proposal", "P1").payload["status"], "approved")

    def test_an_assigned_approver_can_reject_others_proposals(self):
        approver = self.members["accountant"]
        self.assign_approver(approver)
        self.assertEqual(self.reject(approver, "P1", "Budget").status_code, 200)

    def test_missing_proposals_and_long_reasons_are_refused(self):
        self.assertEqual(self.reject(self.owner, "NOPE").status_code, 400)
        self.assertEqual(self.approve(self.owner, "NOPE").status_code, 400)
        self.assertEqual(self.decide("reject", self.owner, "P1", {"note": "x" * 201}).status_code, 400)
