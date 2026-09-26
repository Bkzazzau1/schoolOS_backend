from apps.bankconnect import reconciliation, review
from apps.bankconnect.connections import BankRejected
from apps.bankconnect.models import BankTransaction, ReconciliationDecision, TransactionAllocation
from apps.notifications.models import Notification

from .. import collection_accounts, credit, families, ledger
from ..models import CreditKind, FamilyCreditEntry
from .base import ReceivablesTestCase

N = 100
ACCOUNT = "8012345678"


class BankIntegrationCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        # The bank connection is "gtbank" (see the base class), so the family account is that provider's.
        self.account = collection_accounts.register(self.family, provider="gtbank", account_number=ACCOUNT, actor=self.owner)

    def reconcile(self, tx):
        reconciliation.reconcile_pending(self.school)
        return BankTransaction.objects.get(pk=tx.pk)

    def into_account(self, amount, **over):
        return self.reconcile(self.payment(amount, receiving_account=ACCOUNT, **over))

    def active(self, tx):
        return list(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False))


class PaymentsIntoAFamilyAccountTests(BankIntegrationCase):
    def test_the_account_paid_into_identifies_the_family_with_certainty_and_the_charges_are_paid(self):
        tx = self.into_account(120_000 * N, sender="Somebody unknown", narration="thanks")
        self.assertEqual((tx.reconciliation_status, tx.reconciliation_confidence, tx.family), ("matched", 100, self.family))
        family_reason = next(r for r in tx.match_reasons if r["kind"] == "family")
        self.assertEqual((family_reason["familyId"], family_reason["familyCode"]), (str(self.family.id), self.family.code))
        self.assertEqual(tx.match_reasons[-1]["kind"], "note")
        self.assertEqual(sum(a.amount_minor for a in self.active(tx)), 120_000 * N)
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)
        decision = ReconciliationDecision.objects.get(transaction=tx)
        self.assertEqual((decision.action, decision.actor), ("auto_match", None))
        self.assertTrue(all(a.decision_id == decision.id and a.source == "auto" for a in self.active(tx)))
        self.assertLedgerHolds()

    def test_no_student_is_guessed_at_and_no_student_level_row_is_made(self):
        tx = self.into_account(50_000 * N, narration=f"for {self.ahmad.student_code}")
        self.assertFalse(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=True).exists())
        self.assertEqual({a.family_id for a in self.active(tx)}, {self.family.id})

    def test_paying_the_family_off_makes_the_account_dormant_and_the_overpayment_credit(self):
        tx = self.into_account(320_000 * N)
        self.assertEqual(tx.reconciliation_status, "matched")
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (0, 20_000 * N))
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, "dormant")

    def test_money_arriving_on_a_settled_account_is_held_as_credit_and_flagged_for_a_person(self):
        self.into_account(300_000 * N)
        tx = self.into_account(40_000 * N)
        self.assertEqual(tx.reconciliation_status, "requires_review")
        self.assertIn("held as family credit", tx.match_reasons[-1]["text"])
        self.assertEqual(ledger.family_position(self.family).credit, 40_000 * N)
        self.assertFalse(self.active(tx))
        self.assertEqual(FamilyCreditEntry.objects.filter(transaction=tx, kind=CreditKind.OVERPAYMENT).get().amount_minor, 40_000 * N)

    def test_two_identical_transfers_to_a_family_account_are_two_payments_not_a_duplicate(self):
        first = self.into_account(20_000 * N, sender="Musa Bello", narration="fees")
        second = self.into_account(20_000 * N, sender="Musa Bello", narration="fees")
        self.assertEqual((first.reconciliation_status, second.reconciliation_status), ("matched", "matched"))
        self.assertEqual(ledger.family_position(self.family).outstanding, 260_000 * N)

    def test_the_engine_running_again_allocates_nothing_twice(self):
        tx = self.into_account(100_000 * N)
        reconciliation.reconcile_pending(self.school)
        reconciliation.reconcile_pending(self.school)
        self.assertEqual(sum(a.amount_minor for a in self.active(tx)), 100_000 * N)
        self.assertEqual(ReconciliationDecision.objects.filter(transaction=tx).count(), 1)

    def test_a_closed_account_identifies_no_one_and_the_payment_falls_back_to_the_ordinary_matching(self):
        collection_accounts.close(self.account, actor=self.owner, reason="Retired")
        tx = self.into_account(10_000 * N, sender="Nobody", narration="hello")
        self.assertEqual((tx.reconciliation_status, tx.family), ("unmatched", None))

    def test_another_schools_payment_with_the_same_reference_never_reaches_this_family(self):
        other_family, _ = families.ensure_family_for_student(self.other_school, self.make_student("Zed", "Other", school=self.other_school, guardian="Zed Sr"))
        stranger = self.payment(10_000 * N, school=self.other_school, receiving_account=ACCOUNT)
        reconciliation.reconcile_pending(self.other_school)
        stranger.refresh_from_db()
        self.assertEqual((stranger.family, stranger.reconciliation_status), (None, "unmatched"))
        self.assertFalse(TransactionAllocation.objects.filter(transaction=stranger).exists())

    def test_the_finance_people_and_the_parent_are_told_and_the_message_names_the_family(self):
        parent = self.members["parent"]
        from apps.students.models import GuardianLink

        GuardianLink.objects.filter(student=self.ahmad).update(account_user=parent.user)
        self.into_account(100_000 * N, sender="Musa Bello")
        finance = Notification.objects.filter(kind="bank_payment_received", recipient=self.members["accountant"]).get()
        self.assertIn("was paid into Bello family's account", finance.message)
        mine = Notification.objects.filter(recipient=parent, kind="family_payment").get()
        self.assertIn("is still due", mine.message)


class StudentChosenPaymentsTests(BankIntegrationCase):
    def test_a_payment_matched_to_a_student_is_put_towards_that_students_charges_first(self):
        tx = self.reconcile(self.payment(50_000 * N, sender="Somebody", narration=f"fees {self.maryam.student_code}"))
        self.assertEqual(tx.reconciliation_status, "matched")
        rows = self.active(tx)
        self.assertEqual({r.student_id for r in rows}, {self.maryam.id})
        self.assertEqual(self.charge(self.maryam).status, "partially_paid")
        # the plain student-level record has been turned into a charge-level one and is kept, superseded
        self.assertEqual(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=True, superseded=True).count(), 1)
        self.assertEqual(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=True, superseded=False).count(), 0)
        self.assertLedgerHolds()

    def test_anything_beyond_that_students_charges_goes_to_the_rest_of_the_family_not_lost(self):
        tx = self.reconcile(self.payment(100_000 * N, narration=self.maryam.student_code))
        self.assertEqual(self.charge(self.maryam).status, "settled")
        by_student = {}
        for r in self.active(tx):
            by_student[r.student_id] = by_student.get(r.student_id, 0) + r.amount_minor
        self.assertEqual(by_student.pop(self.maryam.id), 80_000 * N)  # her own charge, in full, first
        self.assertEqual(sum(by_student.values()), 20_000 * N)  # the rest went to her brother and sister, not lost
        self.assertLessEqual(set(by_student), {self.ahmad.id, self.aisha.id})

    def test_a_student_with_no_family_is_left_exactly_as_before_the_ledger_existed(self):
        loner = self.make_student("Lone", "Wolf", guardian="Lone Sr")
        tx = self.reconcile(self.payment(5_000 * N, narration=loner.student_code))
        self.assertEqual(tx.reconciliation_status, "matched")
        rows = TransactionAllocation.objects.filter(transaction=tx, superseded=False)
        self.assertEqual([(r.student_id, r.receivable_id, r.family_id) for r in rows], [(loner.id, None, None)])

    def test_a_school_that_has_not_set_up_families_behaves_exactly_as_it_always_did(self):
        session, term, primary, _ = self.make_year(self.other_school)
        student = self.make_student("Zed", "Other", school=self.other_school, guardian="Zed Sr")
        tx = self.payment(5_000 * N, school=self.other_school, narration=student.student_code)
        reconciliation.reconcile_pending(self.other_school)
        tx.refresh_from_db()
        self.assertEqual(tx.reconciliation_status, "matched")
        self.assertEqual(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False).count(), 0)
        self.assertEqual(FamilyCreditEntry.objects.filter(transaction=tx).count(), 0)


class ReviewDecisionsTests(BankIntegrationCase):
    def setUp(self):
        super().setUp()
        self.tx = self.into_account(120_000 * N)  # matched to the Bello family by its account
        self.other_student = self.make_student("Yusuf", "Sani", guardian="Ada Sani")
        self.sani, _ = families.ensure_family_for_student(self.school, self.other_student)

    def decide(self, action, **kw):
        return review.decide(self.owner, self.tx.id, action=action, note=kw.pop("note", "because"), **kw)

    def test_reopening_takes_the_money_out_of_the_ledger_and_the_charges_are_owed_again(self):
        self.decide("reopen")
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)
        self.assertEqual(self.active(self.tx), [])
        self.assertEqual(BankTransaction.objects.get(pk=self.tx.pk).reconciliation_status, "requires_review")
        self.assertLedgerHolds()

    def test_every_decision_that_says_it_is_not_fees_takes_it_out_too(self):
        for action in ("unrelated_income", "investigate", "reversed", "refunded"):
            tx = self.into_account(10_000 * N)
            before = ledger.family_position(self.family).outstanding
            review.decide(self.owner, tx.id, action=action, note="why")
            self.assertEqual(ledger.family_position(self.family).outstanding, before + 10_000 * N, action)
        self.assertLedgerHolds()

    def test_a_person_can_assign_it_to_a_student_of_another_family_and_that_wins_over_the_account(self):
        from .. import schedules
        from datetime import date, timedelta

        s = schedules.create_schedule(self.school, session=self.session, name="Sani fees", actor=self.owner)
        schedules.add_item(s, actor=self.owner, code="SANI", name="Tuition", category="tuition", amount_minor=50_000 * N, due_date=date.today() + timedelta(days=9), scope="student", student=self.other_student)
        schedules.publish(s, actor=self.owner)
        self.decide("assign", student_id=str(self.other_student.id))
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)  # Bello are owed again
        sani = ledger.family_position(self.sani)
        self.assertEqual((sani.outstanding, sani.credit), (0, 70_000 * N))  # the Sanis owed 50,000: 70,000 is credit
        self.assertEqual({a.family_id for a in self.active(self.tx)}, {self.sani.id})
        manual = [a for a in self.active(self.tx) if a.source == "manual"]
        self.assertTrue(manual and all(a.decision.actor == self.owner for a in manual))
        self.assertLedgerHolds(self.family)
        self.assertLedgerHolds(self.sani)

    def test_a_split_puts_each_part_towards_its_own_student(self):
        self.decide("split", allocations=[
            {"studentId": str(self.ahmad.id), "amountMinor": 70_000 * N}, {"studentId": str(self.aisha.id), "amountMinor": 30_000 * N},
        ])
        by_student = {}
        for a in self.active(self.tx):
            by_student[a.student_id] = by_student.get(a.student_id, 0) + a.amount_minor
        self.assertEqual(by_student, {self.ahmad.id: 70_000 * N, self.aisha.id: 30_000 * N})
        self.assertEqual(BankTransaction.objects.get(pk=self.tx.pk).reconciliation_status, "partially_matched")
        self.assertEqual(ledger.family_position(self.family).paid, 100_000 * N)  # the other 20,000 is not counted
        self.assertLedgerHolds()

    def test_a_decision_that_cannot_take_the_money_back_is_refused_and_changes_nothing(self):
        big = self.into_account(400_000 * N)  # 100,000 credit
        credit.refund(self.family, 100_000 * N, actor=self.owner, reason="Paid back")  # ...which is then paid out
        before = ledger.family_position(self.family)
        with self.assertRaises(BankRejected) as raised:
            review.decide(self.owner, big.id, action="reversed", note="Bank reversed it")
        self.assertEqual(raised.exception.code, "credit_in_use")
        self.assertEqual(ledger.family_position(self.family), before)
        self.assertEqual(BankTransaction.objects.get(pk=big.pk).reconciliation_status, "matched")

    def test_the_payments_own_history_tells_the_story(self):
        self.decide("reopen")
        actions = list(ReconciliationDecision.objects.filter(transaction=self.tx).order_by("at", "id").values_list("action", flat=True))
        self.assertEqual(actions, ["auto_match", "reopen"])
