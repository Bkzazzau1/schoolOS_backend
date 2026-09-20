from apps.notifications.models import Notification
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord

BATCH = "payroll_batch"
SALARY = "owner_payroll_profile"
PERIOD = "2026-09"


class PayrollTestCase(StaffTestCase):
    """Two staff on payroll; a finance officer (prepares), and people the owner has authorized."""

    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]
        self.approver = self.members["principal"]
        self.payer = self.members["administrator"]
        self.salary("STAFF-A", "Aisha Bello", 300000, 30000)
        self.salary("STAFF-B", "Musa Ibrahim", 200000, 20000)
        self.assign_approver(self.approver, authorities=["approve"])
        self.assign_approver(self.payer, authorities=["pay"])

    def salary(self, staff_id, name, gross, deductions, on_payroll=True):
        record, _ = SyncRecord.objects.update_or_create(
            school=self.school, entity_type=SALARY, entity_id=staff_id,
            defaults={"payload": {"staffId": staff_id, "name": name, "role": "Teacher", "gross": gross,
                                  "deductions": deductions, "onPayroll": on_payroll, "history": []}},
        )
        return record

    def lines(self):
        return [{"staffId": "STAFF-A", "name": "Aisha Bello", "net": 270000},
                {"staffId": "STAFF-B", "name": "Musa Ibrahim", "net": 180000}]

    def body(self, status="prepared", **over):
        body = {"period": PERIOD, "status": status, "lines": self.lines()}
        body.update(over)
        return body

    def prepare(self, who=None, period=PERIOD, **over):
        """Prepare (or prepare again): the app creates a batch, then updates it."""
        exists = SyncRecord.objects.filter(school=self.school, entity_type=BATCH, entity_id=period).exists()
        return self.push(BATCH, period, self.body(period=period, **over), who=who or self.finance,
                         operation="update" if exists else "create")

    def move(self, who, status, **over):
        """Change the stored batch's status, as the app does: the stored record with a new status."""
        payload = {**self.stored(BATCH, PERIOD).payload, "status": status, **over}
        return self.push(BATCH, PERIOD, payload, operation="update", who=who)

    def batch(self):
        return self.stored(BATCH, PERIOD).payload


class PreparingTests(PayrollTestCase):
    def test_a_finance_officer_prepares_a_batch_and_the_server_stamps_it(self):
        self.ok(self.prepare())
        batch = self.batch()
        self.assertEqual((batch["status"], batch["total"]), ("prepared", 450000))
        self.assertEqual(batch["preparedByMembershipId"], str(self.finance.id))
        self.assertTrue(batch["preparedAt"])
        self.assertEqual([(e["action"], e["byMembershipId"]) for e in batch["trail"]], [("prepared", str(self.finance.id))])

    def test_the_owner_can_prepare_too(self):
        self.ok(self.prepare(who=self.owner))

    def test_only_people_with_prepare_authority_can(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.rejected(self.prepare(who=self.members[role]), "authorized")
        # Authority to approve or pay is not authority to prepare.
        self.rejected(self.prepare(who=self.approver), "prepare payroll batches")

    def test_the_names_and_amounts_come_from_the_salary_records(self):
        lines = self.lines()
        lines[0]["name"] = "Someone Else"
        self.ok(self.prepare(lines=lines))
        self.assertEqual(self.batch()["lines"][0]["name"], "Aisha Bello")

    def test_an_amount_that_does_not_match_the_salary_is_refused(self):
        for net in (271000, 0, -1, 270000.5, "270000", True):
            lines = self.lines()
            lines[0]["net"] = net
            self.rejected(self.prepare(lines=lines), "does not match")
        self.assertFalse(SyncRecord.objects.filter(entity_type=BATCH).exists())

    def test_only_staff_on_payroll_can_be_paid(self):
        self.salary("STAFF-C", "Off Payroll", 100000, 0, on_payroll=False)
        self.rejected(self.prepare(lines=[*self.lines(), {"staffId": "STAFF-C", "name": "x", "net": 100000}]), "not on payroll")
        self.rejected(self.prepare(lines=[{"staffId": "GHOST", "name": "Ghost", "net": 5}]), "not on payroll")

    def test_no_pay_no_duplicates_no_empty_batches(self):
        self.salary("STAFF-D", "No Net", 50000, 50000)
        self.rejected(self.prepare(lines=[{"staffId": "STAFF-D", "name": "x", "net": 0}]), "no pay")
        self.rejected(self.prepare(lines=[*self.lines(), self.lines()[0]]), "twice")
        self.rejected(self.prepare(lines=[]), "no staff")
        self.rejected(self.prepare(lines="nope"), "no staff")
        self.rejected(self.prepare(lines=[{"net": 1}]), "staffId")

    def test_the_period_must_be_a_real_month_and_match(self):
        for bad in ("2026-13", "2026-9", "September", "2026-09-01", "2026-00"):
            self.rejected(self.push(BATCH, bad, self.body(period=bad), who=self.finance), "month")
        self.rejected(self.push(BATCH, PERIOD, self.body(period="2026-10"), who=self.finance), "period must match")

    def test_a_new_batch_must_start_as_prepared_and_forged_fields_are_ignored(self):
        for status in ("approved", "rejected", "disbursementInstructed"):
            self.rejected(self.prepare(status=status), "starts as prepared")
        self.rejected(self.prepare(status="paid"), "status")
        self.ok(self.prepare(approvedByMembershipId=str(self.owner.id), instructedAt="2020-01-01", total=1,
                             preparedByMembershipId=str(self.owner.id), trail=[]))
        batch = self.batch()
        self.assertEqual((batch["total"], batch["preparedByMembershipId"]), (450000, str(self.finance.id)))
        self.assertNotIn("approvedByMembershipId", batch)
        self.assertNotIn("instructedAt", batch)

    def test_preparing_again_replaces_the_lines_and_the_preparer(self):
        self.ok(self.prepare())
        self.salary("STAFF-B", "Musa Ibrahim", 250000, 20000)
        lines = self.lines()
        lines[1]["net"] = 230000
        self.ok(self.prepare(who=self.owner, lines=lines))
        batch = self.batch()
        self.assertEqual((batch["total"], batch["preparedByMembershipId"]), (500000, str(self.owner.id)))
        self.assertEqual([e["action"] for e in batch["trail"]], ["prepared", "prepared"])

    def test_batches_are_kept_apart_by_school_and_month(self):
        self.ok(self.prepare())
        self.ok(self.prepare(period="2026-10"))
        self.assertEqual(SyncRecord.objects.filter(entity_type=BATCH).count(), 2)
        self.assertEqual(self.prepare(who=self.other_owner).status_code, 403)

    def test_a_batch_cannot_be_deleted(self):
        self.ok(self.prepare())
        self.rejected(self.push(BATCH, PERIOD, operation="delete", who=self.owner), "cannot be deleted")


class ApprovingTests(PayrollTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.prepare())

    def test_someone_with_approve_authority_approves_and_is_recorded(self):
        self.ok(self.move(self.approver, "approved"))
        batch = self.batch()
        self.assertEqual((batch["status"], batch["approvedByMembershipId"]), ("approved", str(self.approver.id)))
        self.assertEqual([e["action"] for e in batch["trail"]], ["prepared", "approved"])
        self.assertEqual(batch["preparedByMembershipId"], str(self.finance.id))

    def test_the_owner_may_approve_what_someone_else_prepared(self):
        self.ok(self.move(self.owner, "approved"))

    def test_nobody_approves_their_own_batch_not_even_the_owner(self):
        self.assign_approver(self.finance, authorities=["approve"])
        self.rejected(self.move(self.finance, "approved"), "different person")
        self.ok(self.prepare(who=self.owner))
        self.rejected(self.move(self.owner, "approved"), "different person")
        self.assertEqual(self.batch()["status"], "prepared")

    def test_finance_cannot_approve_without_the_authority(self):
        self.rejected(self.move(self.finance, "approved"), "approve payroll batches")

    def test_an_assignment_nobody_has_claimed_gives_no_power(self):
        other = self.members["teacher"]
        self.assign_approver(other, authorities=["approve"], status="pendingActivation")
        self.rejected(self.move(other, "approved"), "authorized")

    def test_authority_for_someone_else_does_not_carry_over(self):
        # A pay authority is not an approve authority.
        self.rejected(self.move(self.payer, "approved"), "approve payroll batches")

    def test_a_forged_approval_field_is_not_believed(self):
        self.ok(self.move(self.approver, "approved", approvedByMembershipId=str(self.owner.id), preparedByMembershipId=str(self.owner.id),
                          lines=[{"staffId": "STAFF-A", "name": "x", "net": 999999}], total=999999))
        batch = self.batch()
        self.assertEqual((batch["approvedByMembershipId"], batch["preparedByMembershipId"], batch["total"]),
                         (str(self.approver.id), str(self.finance.id), 450000))
        self.assertEqual(batch["lines"][0]["net"], 270000)

    def test_changing_a_salary_after_preparing_blocks_approval_until_prepared_again(self):
        self.salary("STAFF-A", "Aisha Bello", 400000, 30000)
        self.rejected(self.move(self.approver, "approved"), "Aisha Bello changed")
        lines = self.lines()
        lines[0]["net"] = 370000
        self.ok(self.prepare(lines=lines))
        self.ok(self.move(self.approver, "approved"))
        self.assertEqual(self.batch()["total"], 550000)

    def test_taking_someone_off_payroll_blocks_approval(self):
        self.salary("STAFF-B", "Musa Ibrahim", 200000, 20000, on_payroll=False)
        self.rejected(self.move(self.approver, "approved"), "Musa Ibrahim changed")

    def test_rejecting_needs_a_reason_and_the_approve_authority(self):
        self.rejected(self.move(self.approver, "rejected"), "why")
        self.rejected(self.move(self.approver, "rejected", rejectionReason="   "), "why")
        self.rejected(self.move(self.approver, "rejected", rejectionReason="x" * 301), "too long")
        self.rejected(self.move(self.finance, "rejected", rejectionReason="No"), "reject payroll batches")
        self.ok(self.move(self.approver, "rejected", rejectionReason="Wrong month"))
        batch = self.batch()
        self.assertEqual((batch["status"], batch["rejectionReason"]), ("rejected", "Wrong month"))
        self.assertEqual(batch["trail"][-1]["note"], "Wrong month")

    def test_a_rejected_batch_is_prepared_again_and_can_then_be_approved(self):
        self.ok(self.move(self.approver, "rejected", rejectionReason="Fix the amounts"))
        self.rejected(self.move(self.approver, "approved"), "rejected cannot become approved")
        self.ok(self.prepare())
        self.assertEqual(self.batch()["status"], "prepared")
        self.ok(self.move(self.approver, "approved"))
        self.assertEqual([e["action"] for e in self.batch()["trail"]], ["prepared", "rejected", "prepared", "approved"])


class PayingTests(PayrollTestCase):
    def setUp(self):
        super().setUp()
        self.ok(self.prepare())

    def approved(self):
        self.ok(self.move(self.approver, "approved"))

    def test_an_unapproved_batch_cannot_be_paid(self):
        for who in (self.payer, self.owner):
            self.rejected(self.move(who, "disbursementInstructed"), "prepared cannot become")

    def test_someone_with_the_pay_authority_instructs_payment_once_approved(self):
        self.approved()
        self.ok(self.move(self.payer, "disbursementInstructed"))
        batch = self.batch()
        self.assertEqual((batch["status"], batch["instructedByMembershipId"]), ("disbursementInstructed", str(self.payer.id)))
        self.assertEqual([e["action"] for e in batch["trail"]], ["prepared", "approved", "instructed"])
        self.assertNotIn("paid", str(batch).lower().replace("payroll", ""))

    def test_finance_and_approvers_cannot_release_payment_without_the_pay_authority(self):
        self.approved()
        self.rejected(self.move(self.finance, "disbursementInstructed"), "make payroll payments")
        self.rejected(self.move(self.approver, "disbursementInstructed"), "make payroll payments")
        self.assertEqual(self.batch()["status"], "approved")

    def test_an_approved_or_paid_batch_can_no_longer_change(self):
        self.approved()
        self.rejected(self.prepare(), "approved cannot become prepared")
        self.rejected(self.move(self.approver, "rejected", rejectionReason="Late"), "approved cannot become rejected")
        self.rejected(self.move(self.approver, "approved"), "approved cannot become approved")
        self.ok(self.move(self.payer, "disbursementInstructed"))
        for status in ("prepared", "approved", "rejected", "disbursementInstructed"):
            self.rejected(self.move(self.owner, status, rejectionReason="x"), "cannot become")

    def test_a_stale_device_gets_a_conflict_not_an_overwrite(self):
        self.approved()
        response = self.push(BATCH, PERIOD, {**self.batch(), "status": "disbursementInstructed"}, operation="update",
                             who=self.payer, base_version=1)
        self.assertEqual(response.status_code, 409)


class TellingPeopleTests(PayrollTestCase):
    def told(self, who):
        return list(Notification.objects.filter(recipient=who, kind="payroll_batch").order_by("created_at", "id").values_list("message", flat=True))

    def test_the_right_people_hear_about_each_step(self):
        self.ok(self.prepare())
        self.assertEqual(len(self.told(self.approver)), 1)
        self.assertEqual(len(self.told(self.owner)), 1)
        self.assertEqual(self.told(self.finance), [])          # they did it themselves
        self.assertEqual(self.told(self.members["teacher"]), [])
        self.ok(self.move(self.approver, "approved"))
        self.assertIn("approved", self.told(self.payer)[0])
        self.assertIn("approved", self.told(self.finance)[-1])
        self.ok(self.move(self.payer, "disbursementInstructed"))
        self.assertIn("instructed", self.told(self.finance)[-1])

    def test_the_preparer_is_told_why_it_was_rejected(self):
        self.ok(self.prepare())
        self.ok(self.move(self.approver, "rejected", rejectionReason="Wrong month"))
        self.assertIn("Wrong month", self.told(self.finance)[-1])


class WhoReceivesItTests(PayrollTestCase):
    def kinds(self, who):
        self.client.force_authenticate(who.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.school.id)}).json()["records"]
        return {r["entityType"] for r in found}

    def test_batches_and_salaries_reach_only_people_who_work_on_payroll(self):
        self.ok(self.prepare())
        for who in (self.owner, self.finance, self.approver, self.payer):
            self.assertGreaterEqual(self.kinds(who) & {BATCH, SALARY}, {BATCH, SALARY}, who.role)
        for role in ("teacher", "staff", "parent", "student"):
            self.assertFalse(self.kinds(self.members[role]) & {BATCH, SALARY}, role)

    def test_an_authority_nobody_has_claimed_shows_nothing(self):
        self.ok(self.prepare())
        other = self.members["teacher"]
        self.assign_approver(other, authorities=["view"], status="pendingActivation")
        self.assertFalse(self.kinds(other) & {BATCH, SALARY})
        SyncRecord.objects.filter(entity_id=f"AUTH-{other.id}").update(
            payload={"staffId": f"AUTH-{other.id}", "authorities": ["view"], "status": "active", "membershipId": str(other.id)})
        self.assertGreaterEqual(self.kinds(other) & {BATCH, SALARY}, {BATCH, SALARY})

    def test_another_school_never_sees_it(self):
        self.ok(self.prepare())
        self.client.force_authenticate(self.other_owner.user)
        found = self.client.get("/api/v1/sync/pull/", {"school": str(self.other_school.id)}).json()["records"]
        self.assertEqual(found, [])
