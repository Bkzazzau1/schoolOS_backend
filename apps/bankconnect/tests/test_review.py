from datetime import timedelta

from django.utils import timezone

from ..models import BankTransaction, ReconciliationDecision, TransactionAllocation
from .base import BankTestCase


class ReviewTestCase(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.row(self.connected(purpose="tuition")[0])
        self.aisha = self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello", phone="0803 123 4567", class_name="Primary 3")
        self.bilal = self.make_student("BG-0043", "Bilal", "Bello", guardian="Musa Bello", phone="0803 123 4567")

    def unclear(self, **over):
        """A payment the engine cannot settle: two siblings fit equally well."""
        fields = dict(sender_name="Musa Bello", narration="school fees 08031234567")
        fields.update(over)
        return self.deposit(self.connection, **fields)

    def decide(self, row, who=None, school=None, **body):
        return self.api_post(f"transactions/{row.id}/decide/", body, who=who, school=school)

    def decided(self, row, **body):
        response = self.decide(row, **body)
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["transaction"]


class ReviewQueueTests(ReviewTestCase):
    def test_it_lists_what_needs_a_person_oldest_first_with_counts(self):
        now = timezone.now()
        newer = self.unclear(transaction_date=now)
        older = self.unclear(transaction_date=now - timedelta(days=2), narration="another 08031234567")
        matched = self.deposit(self.connection, narration="BG-0042", transaction_date=now - timedelta(days=1))
        lost = self.deposit(self.connection, narration="mystery", sender_name="Nobody", transaction_date=now - timedelta(days=3))
        self.deposit(self.connection, direction="debit", narration="bank charge")
        body = self.api_get("review/").json()
        self.assertEqual([t["id"] for t in body["transactions"]], [str(lost.id), str(older.id), str(newer.id)])
        self.assertEqual(body["counts"], {"requires_review": 2, "unmatched": 1})
        self.assertEqual(body["total"], 3)
        self.assertNotIn(str(matched.id), str(body))

    def test_a_reviewer_sees_why_and_who_the_engine_suggests(self):
        self.unclear()
        first = self.api_get("review/").json()["transactions"][0]
        self.assertEqual((first["reconciliationStatus"], first["confidence"]), ("requires_review", 65))
        names = {r["studentName"] for r in first["matchReasons"] if r["kind"] == "candidate"}
        self.assertEqual(names, {"Aisha Bello", "Bilal Bello"})
        self.assertEqual(first["allocations"], [])

    def test_it_can_be_narrowed_and_can_leave_out_test_data(self):
        self.unclear()
        self.deposit(self.connection, narration="mystery", sender_name="Nobody")
        self.assertEqual(self.api_get("review/?status=unmatched").json()["total"], 1)
        self.assertEqual(self.api_get("review/?status=matched").json()["total"], 0)  # not a queue status
        self.assertEqual(self.api_get("review/?sandbox=exclude").json()["total"], 0)
        self.assertEqual(self.api_get("review/?status=bogus").status_code, 400)

    def test_once_decided_a_payment_leaves_the_queue(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        self.assertEqual(self.api_get("review/").json()["total"], 0)

    def test_only_the_owner_and_the_finance_office_see_it(self):
        self.unclear()
        self.assertEqual(self.api_get("review/", who=self.members["accountant"]).json()["total"], 1)
        for role in ("principal", "administrator", "teacher", "staff", "parent", "driver"):
            self.assertEqual(self.api_get("review/", who=self.members[role]).status_code, 403, role)

    def test_another_school_sees_none_of_it(self):
        self.unclear()
        theirs = self.api_get("review/", who=self.other_owner, school=self.other_school).json()
        self.assertEqual((theirs["total"], theirs["counts"]), (0, {}))


class AssignTests(ReviewTestCase):
    def test_a_person_assigns_a_payment_to_one_student(self):
        row = self.unclear()
        body = self.decided(row, action="assign", studentId=str(self.bilal.id), note="Father confirmed it is for Bilal")
        self.assertEqual((body["reconciliationStatus"], body["confidence"]), ("matched", 100))
        self.assertEqual([(a["studentName"], a["purpose"], a["amountMinor"], a["source"]) for a in body["allocations"]],
                         [("Bilal Bello", "tuition", 5_000_000, "manual")])
        engine, person = body["decisions"]
        self.assertEqual((engine["action"], engine["actorName"]), ("engine_review", "SchoolOS matching"))
        self.assertEqual((person["action"], person["actorRole"], person["note"]), ("assign", "proprietor", "Father confirmed it is for Bilal"))
        self.assertEqual(person["before"]["status"], "requires_review")
        self.assertEqual(person["after"]["status"], "matched")
        self.assertEqual(person["after"]["allocations"][0]["studentId"], str(self.bilal.id))

    def test_the_purpose_can_be_chosen(self):
        row = self.unclear()
        body = self.decided(row, action="assign", studentId=str(self.aisha.id), purpose="books")
        self.assertEqual(body["allocations"][0]["purpose"], "books")

    def test_the_finance_office_can_decide_too(self):
        row = self.unclear()
        response = self.decide(row, who=self.members["accountant"], action="assign", studentId=str(self.aisha.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ReconciliationDecision.objects.filter(action="assign").get().actor, self.members["accountant"])

    def test_reassigning_retires_the_old_allocation_but_keeps_the_history(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        body = self.decided(row, action="assign", studentId=str(self.bilal.id))
        self.assertEqual([a["studentName"] for a in body["allocations"] if not a["superseded"]], ["Bilal Bello"])
        self.assertEqual([a["studentName"] for a in body["allocations"] if a["superseded"]], ["Aisha Bello"])
        listed = self.api_get("transactions/").json()["transactions"][0]
        self.assertEqual([a["studentName"] for a in listed["allocations"]], ["Bilal Bello"])
        self.assertEqual(TransactionAllocation.objects.count(), 2)

    def test_a_student_from_another_school_or_nowhere_is_refused(self):
        row = self.unclear()
        stranger = self.make_student("ZZ-0001", "Zed", "Other", school=self.other_school)
        for student_id, code in ((str(stranger.id), "unknown_student"), ("not-a-uuid", "invalid_reference"),
                                 ("00000000-0000-0000-0000-000000000000", "unknown_student"), (None, "invalid_reference")):
            response = self.decide(row, action="assign", studentId=student_id)
            self.assertEqual((response.status_code, response.json()["code"]), (400, code), student_id)
        self.assertFalse(TransactionAllocation.objects.exists())

    def test_a_purpose_must_be_one_of_the_known_ones(self):
        row = self.unclear()
        self.assertEqual(self.decide(row, action="assign", studentId=str(self.aisha.id), purpose="holiday").json()["code"], "invalid_purpose")


class SplitTests(ReviewTestCase):
    def test_a_payment_shared_between_siblings(self):
        row = self.unclear()
        body = self.decided(row, action="split", allocations=[
            {"studentId": str(self.aisha.id), "amountMinor": 3_000_000},
            {"studentId": str(self.bilal.id), "amountMinor": 2_000_000, "purpose": "transport"},
        ])
        self.assertEqual(body["reconciliationStatus"], "matched")
        self.assertEqual(sorted((a["studentName"], a["purpose"], a["amountMinor"]) for a in body["allocations"]),
                         [("Aisha Bello", "tuition", 3_000_000), ("Bilal Bello", "transport", 2_000_000)])

    def test_less_than_the_whole_is_a_partial_match_and_stays_in_the_queue(self):
        row = self.unclear()
        body = self.decided(row, action="split", allocations=[{"studentId": str(self.aisha.id), "amountMinor": 1_000_000}])
        self.assertEqual(body["reconciliationStatus"], "partially_matched")
        self.assertEqual(self.api_get("review/").json()["total"], 1)

    def test_more_than_the_whole_or_nonsense_is_refused(self):
        row = self.unclear()
        aisha, bilal = str(self.aisha.id), str(self.bilal.id)
        cases = [
            [{"studentId": aisha, "amountMinor": 5_000_001}],
            [{"studentId": aisha, "amountMinor": 3_000_000}, {"studentId": bilal, "amountMinor": 3_000_000}],
            [{"studentId": aisha, "amountMinor": 0}], [{"studentId": aisha, "amountMinor": -5}],
            [{"studentId": aisha, "amountMinor": 10.5}], [{"studentId": aisha, "amountMinor": "100"}],
            [{"studentId": aisha, "amountMinor": True}],
            [{"studentId": aisha, "amountMinor": 100}, {"studentId": aisha, "amountMinor": 200}],
            [], "everything", [{"amountMinor": 100}], ["x"],
            [{"studentId": aisha, "amountMinor": 1}] * 21,
        ]
        for allocations in cases:
            response = self.decide(row, action="split", allocations=allocations)
            self.assertEqual(response.status_code, 400, allocations)
        self.assertEqual(BankTransaction.objects.get(id=row.id).reconciliation_status, "requires_review")
        self.assertFalse(TransactionAllocation.objects.exists())

    def test_the_same_student_may_appear_twice_for_different_purposes(self):
        row = self.unclear()
        body = self.decided(row, action="split", allocations=[
            {"studentId": str(self.aisha.id), "amountMinor": 4_000_000, "purpose": "tuition"},
            {"studentId": str(self.aisha.id), "amountMinor": 1_000_000, "purpose": "books"},
        ])
        self.assertEqual(body["reconciliationStatus"], "matched")


class OtherDecisionTests(ReviewTestCase):
    def test_unrelated_income_needs_a_reason_and_retires_any_allocation(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        self.assertEqual(self.decide(row, action="unrelated_income").json()["code"], "note_required")
        self.assertEqual(self.decide(row, action="unrelated_income", note="   ").json()["code"], "note_required")
        body = self.decided(row, action="unrelated_income", note="Hall hire deposit")
        self.assertEqual(body["reconciliationStatus"], "unrelated_income")
        self.assertEqual([a for a in body["allocations"] if not a["superseded"]], [])

    def test_investigating(self):
        row = self.unclear()
        body = self.decided(row, action="investigate", note="Ask the bank who this is")
        self.assertEqual(body["reconciliationStatus"], "investigating")
        self.assertEqual(self.api_get("review/?status=investigating").json()["total"], 1)

    def test_marking_a_payment_as_a_duplicate_of_another(self):
        original = self.deposit(self.connection, narration="BG-0042", sender_name="Musa Bello")
        row = self.unclear()
        body = self.decided(row, action="duplicate", duplicateOf=str(original.id), note="Same transfer, reported twice")
        self.assertEqual((body["reconciliationStatus"], body["duplicateOf"]), ("duplicate", str(original.id)))

    def test_a_duplicate_must_point_at_a_real_payment_that_does_not_point_back(self):
        one, two = self.unclear(), self.unclear(narration="different 08031234567")
        self.decided(one, action="duplicate", duplicateOf=str(two.id), note="same")
        for target, code in (("nope", "invalid_reference"), (str(one.id), "duplicate_cycle"),
                             (str(two.id), "unknown_payment"), ("00000000-0000-0000-0000-000000000000", "unknown_payment")):
            self.assertEqual(self.decide(two, action="duplicate", duplicateOf=target, note="x").json()["code"], code, target)

    def test_a_payment_from_another_school_cannot_be_named_as_the_original(self):
        other = self.row(self.connected(who=self.other_owner, school=self.other_school)[0])
        theirs = self.deposit(other, narration="mystery")
        row = self.unclear()
        self.assertEqual(self.decide(row, action="duplicate", duplicateOf=str(theirs.id), note="x").json()["code"], "unknown_payment")

    def test_reversed_and_refunded_are_final_until_reopened(self):
        row = self.unclear()
        self.decided(row, action="reversed", note="Bank reversed it")
        response = self.decide(row, action="assign", studentId=str(self.aisha.id))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "final"))
        reopened = self.decided(row, action="reopen", note="Reversal was itself a mistake")
        self.assertEqual(reopened["reconciliationStatus"], "requires_review")
        refunded = self.decided(row, action="refunded", note="Refunded to the parent")
        self.assertEqual(refunded["reconciliationStatus"], "refunded")

    def test_reopening_puts_a_decided_payment_back_and_retires_its_allocation(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        body = self.decided(row, action="reopen", note="Wrong child")
        self.assertEqual(body["reconciliationStatus"], "requires_review")
        self.assertEqual([a for a in body["allocations"] if not a["superseded"]], [])
        self.assertEqual(self.api_get("review/").json()["total"], 1)

    def test_reopening_something_already_waiting_is_refused(self):
        row = self.unclear()
        response = self.decide(row, action="reopen", note="again")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_decided"))

    def test_an_unknown_action_is_refused(self):
        row = self.unclear()
        for action in ("delete", "", None, "ASSIGN"):
            self.assertEqual(self.decide(row, action=action).json()["code"], "unknown_action", action)

    def test_money_going_out_cannot_be_reconciled(self):
        debit = self.deposit(self.connection, direction="debit")
        response = self.decide(debit, action="unrelated_income", note="x")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "not_a_credit"))

    def test_a_note_cannot_be_absurdly_long(self):
        row = self.unclear()
        self.assertEqual(self.decide(row, action="investigate", note="x" * 501).json()["code"], "invalid_note")


class AuditAndSafetyTests(ReviewTestCase):
    def test_every_decision_is_recorded_with_who_why_before_and_after_and_none_are_lost(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        self.decided(row, action="reopen", note="Wrong child")
        self.decided(row, action="unrelated_income", note="Actually a donation")
        actions = list(ReconciliationDecision.objects.filter(transaction=row).order_by("at", "id").values_list("action", flat=True))
        self.assertEqual(sorted(actions), sorted(["engine_review", "assign", "reopen", "unrelated_income"]))
        last = ReconciliationDecision.objects.get(action="unrelated_income")
        self.assertEqual((last.actor, last.note, last.before["status"], last.after["status"]),
                         (self.owner, "Actually a donation", "requires_review", "unrelated_income"))

    def test_a_decision_made_on_a_stale_view_is_refused(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id), expectedStatus="requires_review")
        response = self.decide(row, action="assign", studentId=str(self.bilal.id), expectedStatus="requires_review")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "stale"))
        self.assertEqual(TransactionAllocation.objects.get(superseded=False).student, self.aisha)

    def test_the_status_the_client_saw_is_optional(self):
        row = self.unclear()
        self.decided(row, action="assign", studentId=str(self.aisha.id))
        self.decided(row, action="assign", studentId=str(self.bilal.id))

    def test_the_people_who_may_not_decide_cannot(self):
        row = self.unclear()
        for role in ("principal", "administrator", "teacher", "staff", "parent", "driver"):
            self.assertEqual(self.decide(row, who=self.members[role], action="assign", studentId=str(self.aisha.id)).status_code, 403, role)
        self.assertFalse(TransactionAllocation.objects.exists())

    def test_another_schools_owner_cannot_touch_our_payment_or_see_it(self):
        row = self.unclear()
        response = self.decide(row, who=self.other_owner, school=self.other_school, action="assign", studentId=str(self.aisha.id))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.api_get(f"transactions/{row.id}/", who=self.other_owner, school=self.other_school).status_code, 404)
        self.assertEqual(self.decide(row, who=self.other_owner, action="assign", studentId=str(self.aisha.id)).status_code, 403)
        self.assertEqual(BankTransaction.objects.get(id=row.id).reconciliation_status, "requires_review")

    def test_the_detail_shows_the_whole_story(self):
        row = self.unclear()
        self.decided(row, action="investigate", note="Checking")
        body = self.api_get(f"transactions/{row.id}/").json()["transaction"]
        self.assertEqual([d["action"] for d in body["decisions"]], ["engine_review", "investigate"])
        self.assertEqual(body["maskedAccountNumber"], "****6789")
        self.assert_no_secrets(body)


class StudentSearchTests(ReviewTestCase):
    def search(self, query, **kw):
        return self.api_get(f"students/?q={query}", **kw)

    def test_by_name_code_or_admission_number(self):
        for query in ("aisha", "bello", "BG-0042", "bg-004"):
            self.assertIn("Aisha Bello", [s["name"] for s in self.search(query).json()["students"]], query)
        student = self.search("Aisha").json()["students"][0]
        self.assertEqual((student["studentCode"], student["className"], student["status"]), ("BG-0042", "Primary 3", "active"))
        self.assertEqual(self.search(self.aisha.admission_number).json()["students"][0]["id"], str(self.aisha.id))

    def test_a_single_letter_finds_nothing_and_an_unknown_name_finds_nothing(self):
        self.assertEqual(self.search("a").json()["students"], [])
        self.assertEqual(self.search("zzzz").json()["students"], [])

    def test_only_this_schools_students_and_only_for_those_who_may_look(self):
        self.make_student("BG-9999", "Bello", "Elsewhere", school=self.other_school)
        self.assertEqual(len(self.search("bello").json()["students"]), 2)
        self.assertEqual(self.search("bello", who=self.members["accountant"]).status_code, 200)
        self.assertEqual(self.search("bello", who=self.members["teacher"]).status_code, 403)
        self.assertEqual(len(self.search("bello", who=self.other_owner, school=self.other_school).json()["students"]), 1)
