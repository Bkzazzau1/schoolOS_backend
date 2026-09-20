from apps.sync.models import SyncRecord

from .helpers import OwnerTestCase


def salary(gross=250000, deductions=50000, on_payroll=True, history=None, **extra):
    payload = {
        "staffId": "STAFF-001", "name": "Mrs. Amina Yusuf", "role": "Teacher",
        "gross": gross, "deductions": deductions, "onPayroll": on_payroll,
        "history": history if history is not None else [
            {"at": "client-time", "gross": gross, "deductions": deductions,
             "onPayroll": on_payroll, "byMembershipId": "someone-else"}
        ],
    }
    payload.update(extra)
    return payload


class SalaryProfileTests(OwnerTestCase):
    entity_type = "owner_payroll_profile"

    def test_the_owner_sets_a_salary_and_the_server_stamps_who_and_when(self):
        self.assertAccepted(self.push("STAFF-001", salary(injected="x")))
        stored = self.stored("STAFF-001").payload
        self.assertEqual((stored["gross"], stored["deductions"], stored["onPayroll"]), (250000, 50000, True))
        self.assertNotIn("injected", stored)
        entry = stored["history"][0]
        self.assertEqual(entry["byMembershipId"], str(self.owner.id))
        self.assertNotEqual(entry["at"], "client-time")
        self.assertEqual(stored["updatedAt"], entry["at"])

    def test_nobody_but_the_owner_may_set_salaries(self):
        for role, member in self.members.items():
            if role == "proprietor":
                continue
            self.assertRejected(self.push("STAFF-001", salary(), who=member), "role")
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_bad_figures_are_refused(self):
        bad = [
            salary(gross=-1, deductions=0),
            salary(gross="250000"),
            salary(gross=250000.5),
            salary(gross=True),
            salary(gross=1_000_000_001, deductions=0),
            salary(deductions=250001),
            salary(deductions=-5),
            salary(gross=0, deductions=0),  # on payroll with no salary
            salary(on_payroll="yes"),
        ]
        for payload in bad:
            self.assertRejected(self.push("STAFF-001", payload))
        self.assertEqual(SyncRecord.objects.count(), 0)

    def test_a_salary_of_zero_is_fine_when_not_on_payroll(self):
        self.assertAccepted(self.push("STAFF-001", salary(gross=0, deductions=0, on_payroll=False)))

    def test_identity_and_required_fields(self):
        self.assertRejected(self.push("STAFF-002", salary()), "staffId")
        self.assertRejected(self.push("STAFF-001", salary(name="")), "name")
        self.assertRejected(self.push("STAFF-001", {k: v for k, v in salary().items() if k != "staffId"}), "staffId")

    def test_history_must_be_present_and_end_at_the_current_salary(self):
        self.assertRejected(self.push("STAFF-001", salary(history=[])), "history")
        self.assertRejected(self.push("STAFF-001", {k: v for k, v in salary().items() if k != "history"}), "history")
        self.assertRejected(self.push("STAFF-001", salary(history="nope")), "history")
        self.assertRejected(self.push("STAFF-001", salary(history=["x"])), "objects")
        stale = [{"gross": 1, "deductions": 0, "onPayroll": True}]
        self.assertRejected(self.push("STAFF-001", salary(history=stale)), "newest")

    def test_history_only_ever_grows(self):
        self.push("STAFF-001", salary())
        original = self.stored("STAFF-001").payload["history"][0]
        raise_ = [
            {"gross": 250000, "deductions": 50000, "onPayroll": True},
            {"gross": 300000, "deductions": 60000, "onPayroll": True},
        ]
        self.assertAccepted(self.push("STAFF-001", salary(300000, 60000, history=raise_), operation="update"))
        history = self.stored("STAFF-001").payload["history"]
        self.assertEqual(len(history), 2)
        # The first entry is exactly as the server stored it, not what the app echoed.
        self.assertEqual(history[0], original)
        self.assertEqual(history[1]["byMembershipId"], str(self.owner.id))

    def test_the_past_cannot_be_rewritten_or_dropped(self):
        self.push("STAFF-001", salary())
        rewrite = [{"gross": 999, "deductions": 0, "onPayroll": True}]
        self.assertRejected(
            self.push("STAFF-001", salary(999, 0, history=rewrite), operation="update"), "history"
        )
        # Dropping the old entry and adding a new one is also a rewrite.
        replaced = [{"gross": 300000, "deductions": 0, "onPayroll": True}]
        self.assertRejected(
            self.push("STAFF-001", salary(300000, 0, history=replaced), operation="update"), "history"
        )
        stored = self.stored("STAFF-001").payload
        self.assertEqual((stored["gross"], len(stored["history"])), (250000, 1))

    def test_saving_again_without_a_change_is_fine(self):
        self.push("STAFF-001", salary())
        self.assertAccepted(self.push("STAFF-001", salary(), operation="update"))
        self.assertEqual(len(self.stored("STAFF-001").payload["history"]), 1)

    def test_a_salary_record_cannot_be_deleted(self):
        self.push("STAFF-001", salary())
        self.assertRejected(self.push("STAFF-001", operation="delete"), "deleted")
        self.assertFalse(self.stored("STAFF-001").deleted)

    def test_a_stale_edit_from_another_device_is_a_conflict(self):
        self.push("STAFF-001", salary())
        two = [
            {"gross": 250000, "deductions": 50000, "onPayroll": True},
            {"gross": 260000, "deductions": 50000, "onPayroll": True},
        ]
        self.push("STAFF-001", salary(260000, 50000, history=two), operation="update", base_version=1)
        response = self.push("STAFF-001", salary(270000, 50000), operation="update", base_version=1)
        self.assertEqual(response.status_code, 409)

    def test_the_same_staff_id_in_another_school_is_a_different_record(self):
        from apps.schools.models import Membership, Role, School
        from django.contrib.auth import get_user_model

        other = School.objects.create(name="Other", slug="other")
        user = get_user_model().objects.create_user("owner@other.ng", "a-long-test-password-1")
        other_owner = Membership.objects.create(user=user, school=other, role=Role.PROPRIETOR)
        self.push("STAFF-001", salary(250000))
        self.assertAccepted(self.push("STAFF-001", salary(111, 0), who=other_owner, school=other))
        self.assertEqual(SyncRecord.objects.get(school=self.school).payload["gross"], 250000)
        self.assertEqual(SyncRecord.objects.get(school=other).payload["gross"], 111)
