"""The Finance Office's own fee ledger: a billing authority (the owner, or a duty holder) decides what
is charged and when it is due; the owner or the Finance Office records and corrects payments, queues
reminders and keys in the bank statement. Restores apps.financeledger's handlers to the sync registry -
before this app existed, every one of these pushes was rejected outright (`SYNC_ALLOW_UNLISTED_ENTITY_TYPES`
is off in production), so the Finance Office's own fee structure, payments, reminders and reconciliation
screens could not save anything at all on a real server.
"""

from apps.financeledger.constants import BANK_LINE, FEE_STRUCTURE, PAYMENT, REMINDER, TERM_DUE
from apps.financeledger.sync_ids import sync_safe_id
from apps.staff.tests.helpers import StaffTestCase
from apps.sync import registry
from apps.sync.models import SyncRecord

TERM = "2026/2027 · Term 1"


class FinanceLedgerTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]

    def give_duty(self, member, duty, status="active"):
        SyncRecord.objects.create(
            school=self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}",
            payload={"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                     "membershipId": str(member.id)},
        )


class FeeStructureTests(FinanceLedgerTestCase):
    def structure(self, **over):
        body = {"term": TERM, "section": "Primary",
                "items": [{"name": "Tuition", "amount": 150000}], "updatedAt": "stale"}
        body.update(over)
        return body

    def save(self, who=None, term=TERM, section="Primary", **over):
        return self.push(FEE_STRUCTURE, sync_safe_id(f"{term}|{section}"),
                          self.structure(term=term, section=section, **over), who=who or self.owner)

    def test_the_owner_may_set_fees(self):
        self.ok(self.save())
        stored = self.stored(FEE_STRUCTURE, sync_safe_id(f"{TERM}|Primary")).payload
        self.assertEqual(stored["items"], [{"name": "Tuition", "amount": 150000}])
        self.assertNotEqual(stored["updatedAt"], "stale")      # the server's own clock, not the app's

    def test_accountant_alone_may_not_set_fees_but_a_given_duty_lets_them(self):
        self.rejected(self.save(who=self.finance), "billing authority")
        self.give_duty(self.finance, "finance.billing_authority")
        self.ok(self.save(who=self.finance))

    def test_nobody_else_may_set_fees(self):
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student"):
            self.rejected(self.save(who=self.members[role]), "billing authority")

    def test_the_id_must_match_term_and_section(self):
        response = self.push(FEE_STRUCTURE, "wrong-id", self.structure(), who=self.owner)
        self.rejected(response, "match")

    def test_an_unknown_section_is_refused(self):
        self.rejected(self.save(section="Boarding"), "section")

    def test_items_must_make_sense(self):
        self.rejected(self.save(items=[]), "at least one")
        self.rejected(self.save(items=[{"name": "", "amount": 1000}]))
        self.rejected(self.save(items=[{"name": "Tuition", "amount": 0}]))
        self.rejected(self.save(items=[{"name": "Tuition", "amount": -1}]))
        self.rejected(self.save(items=[{"name": "Tuition", "amount": 1000}, {"name": "tuition", "amount": 500}]),
                      "same name")

    def test_a_fee_structure_cannot_be_deleted(self):
        self.ok(self.save())
        response = self.push(FEE_STRUCTURE, sync_safe_id(f"{TERM}|Primary"), operation="delete", who=self.owner)
        self.rejected(response, "cannot be deleted")

    def test_only_an_operator_may_read_it_a_teacher_may_not(self):
        handler = registry.get(FEE_STRUCTURE)
        payload = {"term": TERM, "section": "Primary", "items": [], "updatedAt": "now"}
        self.assertEqual(handler.visible(self.finance, payload), payload)
        self.assertIsNone(handler.visible(self.members["teacher"], payload))


class TermDueTests(FinanceLedgerTestCase):
    def test_the_owner_may_set_a_due_date(self):
        response = self.push(TERM_DUE, sync_safe_id(TERM), {"term": TERM, "date": "2026-09-30"}, who=self.owner)
        self.ok(response)
        self.assertEqual(self.stored(TERM_DUE, sync_safe_id(TERM)).payload["date"], "2026-09-30")

    def test_accountant_alone_may_not_set_it(self):
        response = self.push(TERM_DUE, "T1", {"term": "T1", "date": "2026-09-30"}, who=self.finance)
        self.rejected(response, "billing authority")

    def test_the_id_must_be_the_term_and_the_date_must_be_real(self):
        self.rejected(self.push(TERM_DUE, "T1", {"term": "T2", "date": "2026-09-30"}, who=self.owner), "match")
        self.rejected(self.push(TERM_DUE, "T1", {"term": "T1", "date": "30 Sep"}, who=self.owner), "real date")


class PaymentTests(FinanceLedgerTestCase):
    def pay(self, id="PAY-1", who=None, **over):
        body = {"id": id, "studentId": "S1", "studentName": "Musa Ibrahim", "className": "Primary 4",
                "term": "2026/2027 · Term 1", "amount": 50000, "method": "Cash", "receivedAt": "stale",
                "reference": "", "recordedBy": "forged", "note": "", "voidedReason": ""}
        body.update(over)
        return self.push(PAYMENT, id, body, who=who or self.finance)

    def test_the_owner_or_finance_office_may_record_a_payment(self):
        self.ok(self.pay())
        stored = self.stored(PAYMENT, "PAY-1").payload
        self.assertEqual(stored["receiptNumber"], "RCT-000001")
        self.assertEqual(stored["recordedBy"], str(self.finance.id))      # never the app's own value
        self.assertNotEqual(stored["receivedAt"], "stale")

    def test_a_duty_holder_may_record_a_payment_too(self):
        person = self.members["administrator"]
        self.rejected(self.pay(who=person), "Finance Office")
        self.give_duty(person, "finance.accounts")
        self.ok(self.pay(who=person))

    def test_nobody_else_may_record_a_payment(self):
        for role in ("principal", "teacher", "staff", "parent", "student"):
            self.rejected(self.pay(who=self.members[role]), "Finance Office")

    def test_receipt_numbers_follow_the_real_server_count(self):
        self.ok(self.pay(id="PAY-1"))
        self.ok(self.pay(id="PAY-2"))
        self.assertEqual(self.stored(PAYMENT, "PAY-2").payload["receiptNumber"], "RCT-000002")

    def test_amounts_and_methods_must_make_sense(self):
        self.rejected(self.pay(amount=0))
        self.rejected(self.pay(amount=-1))
        self.rejected(self.pay(method="Cheque"))

    def test_a_non_cash_payment_needs_a_reference(self):
        self.rejected(self.pay(method="POS", reference=""), "reference")
        self.ok(self.pay(method="POS", reference="POS-REF-1"))

    def test_the_same_reference_cannot_be_recorded_twice(self):
        self.ok(self.pay(id="PAY-1", method="Bank transfer", reference="REF-1"))
        self.rejected(self.pay(id="PAY-2", method="Bank transfer", reference="REF-1"), "already been recorded")
        self.rejected(self.pay(id="PAY-3", method="Bank transfer", reference="ref-1"), "already been recorded")

    def test_a_voided_payments_reference_may_be_reused(self):
        self.ok(self.pay(id="PAY-1", method="Bank transfer", reference="REF-1"))
        stored = self.stored(PAYMENT, "PAY-1").payload
        self.push(PAYMENT, "PAY-1", {**stored, "voidedReason": "typed twice"}, operation="update", who=self.finance)
        self.ok(self.pay(id="PAY-2", method="Bank transfer", reference="REF-1"))

    def test_the_same_reference_in_another_school_never_clashes(self):
        self.ok(self.pay(id="PAY-1", method="Bank transfer", reference="REF-1"))
        response = self.push(PAYMENT, "PAY-9", {
            "id": "PAY-9", "studentId": "S9", "studentName": "X", "className": "Y", "term": "T",
            "amount": 1000, "method": "Bank transfer", "receivedAt": "x", "reference": "REF-1",
            "recordedBy": "", "note": "", "voidedReason": "",
        }, who=self.other_owner, school=self.other_school)
        self.ok(response)

    def test_voiding_needs_a_reason_and_cannot_change_anything_else(self):
        self.ok(self.pay())
        stored = self.stored(PAYMENT, "PAY-1").payload
        self.rejected(self.push(PAYMENT, "PAY-1", {**stored, "voidedReason": ""}, operation="update", who=self.finance), "required")
        self.rejected(
            self.push(PAYMENT, "PAY-1", {**stored, "amount": 99, "voidedReason": "mistake"}, operation="update", who=self.finance),
            "only voided",
        )
        self.ok(self.push(PAYMENT, "PAY-1", {**stored, "voidedReason": "mistake"}, operation="update", who=self.finance))
        self.assertEqual(self.stored(PAYMENT, "PAY-1").payload["voidedReason"], "mistake")

    def test_a_payment_cannot_be_voided_twice(self):
        self.ok(self.pay())
        stored = self.stored(PAYMENT, "PAY-1").payload
        self.push(PAYMENT, "PAY-1", {**stored, "voidedReason": "first"}, operation="update", who=self.finance)
        voided = self.stored(PAYMENT, "PAY-1").payload
        self.rejected(self.push(PAYMENT, "PAY-1", {**voided, "voidedReason": "second"}, operation="update", who=self.finance), "already voided")

    def test_a_payment_cannot_be_deleted(self):
        self.ok(self.pay())
        self.rejected(self.push(PAYMENT, "PAY-1", operation="delete", who=self.finance), "cannot be deleted")


class ReminderTests(FinanceLedgerTestCase):
    def remind(self, id="REM-1", who=None, **over):
        body = {"id": id, "studentId": "S1", "studentName": "Musa", "term": "2026/2027 · Term 1",
                "level": 1, "balance": 50000, "queuedAt": "stale", "message": "Please pay", "queuedBy": "forged"}
        body.update(over)
        return self.push(REMINDER, id, body, who=who or self.finance)

    def test_the_owner_or_finance_office_may_queue_a_reminder(self):
        self.ok(self.remind())
        stored = self.stored(REMINDER, "REM-1").payload
        self.assertEqual(stored["queuedBy"], str(self.finance.id))
        self.assertNotEqual(stored["queuedAt"], "stale")

    def test_nobody_else_may_queue_one(self):
        self.rejected(self.remind(who=self.members["teacher"]), "Finance Office")

    def test_the_level_and_balance_must_make_sense(self):
        self.rejected(self.remind(level=0))
        self.rejected(self.remind(level=4))
        self.rejected(self.remind(balance=0))

    def test_a_reminder_can_never_be_changed_or_deleted_once_queued(self):
        self.ok(self.remind())
        stored = self.stored(REMINDER, "REM-1").payload
        self.rejected(self.push(REMINDER, "REM-1", {**stored, "level": 2}, operation="update", who=self.finance), "cannot be changed")
        self.rejected(self.push(REMINDER, "REM-1", operation="delete", who=self.finance), "cannot be changed")


class BankLineTests(FinanceLedgerTestCase):
    def line(self, id="BNK-1", who=None, **over):
        body = {"id": id, "date": "2026-09-15", "amount": 50000, "reference": "STMT-1", "narration": "", "addedBy": "forged"}
        body.update(over)
        return self.push(BANK_LINE, id, body, who=who or self.finance)

    def test_the_owner_or_finance_office_may_add_a_line(self):
        self.ok(self.line())
        stored = self.stored(BANK_LINE, "BNK-1").payload
        self.assertEqual(stored["addedBy"], str(self.finance.id))

    def test_nobody_else_may_add_a_line(self):
        self.rejected(self.line(who=self.members["parent"]), "Finance Office")

    def test_the_date_and_amount_must_make_sense(self):
        self.rejected(self.line(date="15 Sep 2026"))
        self.rejected(self.line(amount=0))

    def test_a_reference_can_only_be_entered_once(self):
        self.ok(self.line(id="BNK-1", reference="STMT-1"))
        self.rejected(self.line(id="BNK-2", reference="STMT-1"), "already on the statement")
        self.rejected(self.line(id="BNK-3", reference="stmt-1"), "already on the statement")

    def test_a_line_can_never_be_changed_or_deleted_once_added(self):
        self.ok(self.line())
        stored = self.stored(BANK_LINE, "BNK-1").payload
        self.rejected(self.push(BANK_LINE, "BNK-1", {**stored, "amount": 1}, operation="update", who=self.finance), "cannot be changed")
        self.rejected(self.push(BANK_LINE, "BNK-1", operation="delete", who=self.finance), "cannot be changed")
