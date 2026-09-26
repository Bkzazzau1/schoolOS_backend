from apps.owner.jobs.constants import DUTIES
from apps.schools.models import Membership, Role

from .. import permissions
from ..constants import BILLING_AUTHORITY_DUTY
from .base import ReceivablesTestCase

STAFF_SIDE = ("principal", "administrator", "teacher", "accountant", "staff", "driver")


class BillingAuthorityTests(ReceivablesTestCase):
    def test_the_owner_always_has_it(self):
        self.assertTrue(permissions.can_manage_billing(self.owner))
        self.assertTrue(permissions.can_operate_receivables(self.owner))

    def test_a_job_title_never_carries_it(self):
        for role in STAFF_SIDE:
            self.assertFalse(permissions.can_manage_billing(self.members[role]), role)

    def test_the_finance_office_can_work_the_ledger_but_not_decide_what_families_owe(self):
        finance = self.members["accountant"]
        self.assertTrue(permissions.can_operate_receivables(finance))
        self.assertFalse(permissions.can_manage_billing(finance))

    def test_the_owner_can_give_it_to_anyone_on_the_staff_side_and_only_them(self):
        for role in STAFF_SIDE:
            member = self.members[role]
            self.give_duty(member)
            self.assertTrue(permissions.can_manage_billing(member), role)
            self.assertTrue(permissions.can_operate_receivables(member), role)

    def test_a_grant_that_is_not_active_gives_nothing(self):
        member = self.members["principal"]
        for status in ("pendingActivation", "revoked"):
            self.give_duty(member, status=status)
            self.assertFalse(permissions.can_manage_billing(member), status)

    def test_a_different_duty_gives_nothing(self):
        member = self.members["administrator"]
        for duty in ("finance.concessions", "finance.fees", "finance.approvals", "finance.bank_connections"):
            self.give_duty(member, duty=duty)
            self.assertFalse(permissions.can_manage_billing(member), duty)

    def test_a_parent_student_or_alumnus_can_never_hold_it(self):
        for role in ("parent", "student", "alumni"):
            member = self.members[role]
            self.give_duty(member)
            self.assertFalse(permissions.can_manage_billing(member), role)
            self.assertFalse(permissions.can_operate_receivables(member), role)

    def test_someone_no_longer_at_the_school_loses_it(self):
        member = self.members["principal"]
        self.give_duty(member)
        Membership.objects.filter(id=member.id).update(is_active=False)
        member.refresh_from_db()
        self.assertFalse(permissions.can_manage_billing(member))

    def test_a_duty_given_at_one_school_does_nothing_at_another(self):
        stranger = Membership.objects.create(user=self.members["principal"].user, school=self.other_school, role=Role.PRINCIPAL)
        self.give_duty(self.members["principal"])  # at self.school
        self.assertTrue(permissions.can_manage_billing(self.members["principal"]))
        self.assertFalse(permissions.can_manage_billing(stranger))

    def test_the_other_schools_owner_has_nothing_here(self):
        self.assertTrue(permissions.can_manage_billing(self.other_owner))
        self.assertEqual([m.id for m in permissions.billing_authorities(self.school)], [self.owner.id])

    def test_everyone_who_may_decide_can_be_found_so_they_can_be_told(self):
        self.give_duty(self.members["principal"])
        self.give_duty(self.members["teacher"], status="revoked")
        found = {m.id for m in permissions.billing_authorities(self.school)}
        self.assertEqual(found, {self.owner.id, self.members["principal"].id})


class TheDutyIsAssignableTests(ReceivablesTestCase):
    def job(self, **over):
        payload = {
            "name": "Mrs. Hauwa Sule", "email": "", "title": "Bursar", "recipientType": "registered",
            "registeredStaffId": "STAFF-021", "role": "finance", "sectionId": "", "sectionName": "",
            "duties": [BILLING_AUTHORITY_DUTY], "status": "pendingActivation",
        }
        payload.update(over)
        return payload

    def test_it_is_one_of_the_duties_the_owner_can_assign(self):
        self.assertIn(BILLING_AUTHORITY_DUTY, DUTIES)

    def test_the_owner_assigns_it_through_the_ordinary_duty_system(self):
        self.ok(self.push("owner_job_assignment", "J1", self.job()))
        self.assertEqual(self.stored("owner_job_assignment", "J1").payload["duties"], [BILLING_AUTHORITY_DUTY])

    def test_only_the_owner_may_assign_it(self):
        for role in ("accountant", "principal", "administrator"):
            self.rejected(self.push("owner_job_assignment", "J1", self.job(), who=self.members[role]), "role")
