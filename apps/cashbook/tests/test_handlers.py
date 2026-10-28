"""The school's own income and spending: the owner or Finance Office records it; income posts
immediately, an expense needs a billing authority's decision before it counts as spent."""

from apps.cashbook.constants import CASHFLOW_ENTRY
from apps.notifications.models import Notification
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord


class CashbookTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]

    def give_duty(self, member, duty, status="active"):
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            payload={"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                     "membershipId": str(member.id)},
        )

    def entry(self, id="EXP-1", who=None, **over):
        body = {"id": id, "date": "2026-09-15", "description": "Generator fuel", "type": "expense",
                "category": "Utilities", "amount": 25000}
        body.update(over)
        return self.push(CASHFLOW_ENTRY, id, body, who=who or self.finance)


class CreatingTests(CashbookTestCase):
    def test_the_owner_or_finance_office_may_record_an_expense(self):
        self.ok(self.entry())
        stored = self.stored(CASHFLOW_ENTRY, "EXP-1").payload
        self.assertEqual((stored["status"], stored["recordedBy"]), ("pendingApproval", str(self.finance.id)))

    def test_income_posts_immediately_with_no_approval_needed(self):
        self.ok(self.entry(id="INC-1", type="income", category="Donation", description="PTA gift"))
        self.assertEqual(self.stored(CASHFLOW_ENTRY, "INC-1").payload["status"], "posted")

    def test_a_duty_holder_may_record_entries_too(self):
        person = self.members["administrator"]
        self.rejected(self.entry(who=person), "Finance Office")
        self.give_duty(person, "finance.collections")
        self.ok(self.entry(who=person))

    def test_nobody_else_may_record_an_entry(self):
        for role in ("principal", "teacher", "staff", "parent", "student"):
            self.rejected(self.entry(who=self.members[role]), "Finance Office")

    def test_an_unknown_type_or_category_is_refused(self):
        self.rejected(self.entry(type="refund"))
        self.rejected(self.entry(category="Bribes"))

    def test_the_amount_and_date_must_make_sense(self):
        self.rejected(self.entry(amount=0))
        self.rejected(self.entry(amount=-5))
        self.rejected(self.entry(date="15 Sep 2026"))

    def test_an_entry_cannot_be_deleted(self):
        self.ok(self.entry())
        self.rejected(self.push(CASHFLOW_ENTRY, "EXP-1", operation="delete", who=self.finance), "cannot be deleted")

    def test_recording_an_expense_notifies_every_billing_authority_but_not_the_preparer(self):
        self.ok(self.entry(who=self.finance))
        self.assertTrue(Notification.objects.filter(recipient=self.owner, kind="expense_request").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.finance, kind="expense_request").exists())

    def test_recording_income_notifies_nobody(self):
        self.ok(self.entry(id="INC-1", type="income", category="Donation", description="Gift"))
        self.assertFalse(Notification.objects.filter(kind="expense_request").exists())


class DecidingTests(CashbookTestCase):
    def decide(self, status, who=None, id="EXP-1", **over):
        stored = self.stored(CASHFLOW_ENTRY, id).payload
        return self.push(CASHFLOW_ENTRY, id, {**stored, "status": status, **over}, operation="update", who=who or self.owner)

    def test_the_owner_may_approve_or_decline_an_expense(self):
        self.ok(self.entry())
        self.ok(self.decide("approved"))
        stored = self.stored(CASHFLOW_ENTRY, "EXP-1").payload
        self.assertEqual((stored["status"], stored["decidedBy"]), ("approved", str(self.owner.id)))

    def test_accountant_alone_may_not_decide_but_a_given_duty_lets_them(self):
        self.ok(self.entry())
        self.rejected(self.decide("approved", who=self.finance), "billing authority")
        self.give_duty(self.finance, "finance.billing_authority")
        self.ok(self.decide("approved", who=self.finance))

    def test_declining_needs_a_reason(self):
        self.ok(self.entry())
        self.rejected(self.decide("declined"), "required")
        self.ok(self.decide("declined", decisionNote="Not budgeted"))

    def test_a_decision_is_final(self):
        self.ok(self.entry())
        self.ok(self.decide("approved"))
        self.rejected(self.decide("declined"), "already")

    def test_income_can_never_be_decided_it_already_posted(self):
        self.ok(self.entry(id="INC-1", type="income", category="Donation", description="Gift"))
        self.rejected(self.decide("approved", id="INC-1"), "posted immediately")

    def test_deciding_an_expense_notifies_the_preparer_unless_they_decided_their_own(self):
        self.ok(self.entry(who=self.finance))
        self.ok(self.decide("approved", who=self.owner))
        self.assertTrue(Notification.objects.filter(recipient=self.finance, kind="expense_decided").exists())
