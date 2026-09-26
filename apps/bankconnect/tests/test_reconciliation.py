from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.notifications.models import Notification

from .. import matching, reconciliation, sandbox_tools, sync
from ..models import BankTransaction, ReconciliationDecision, TransactionAllocation
from .base import BankTestCase


class EngineTestCase(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.row(self.connected(purpose="tuition")[0])

    def signals(self, row):
        return [s["signal"] for c in row.match_reasons if c["kind"] == "candidate" for s in c["signals"]]

    def candidates(self, row):
        return [c for c in row.match_reasons if c["kind"] == "candidate"]


class StudentReferenceTests(EngineTestCase):
    def test_the_student_code_in_the_narration_matches_and_allocates_automatically(self):
        aisha = self.make_student("BG-0042", "Aisha", "Bello", class_name="Primary 3")
        row = self.deposit(self.connection, narration="BG-0042 first term tuition", sender_name="Someone Else")
        self.assertEqual((row.reconciliation_status, row.reconciliation_confidence, row.engine_version), ("matched", 90, "1"))
        allocation = TransactionAllocation.objects.get()
        self.assertEqual(
            (allocation.transaction, allocation.student, allocation.purpose, allocation.amount_minor, allocation.source),
            (row, aisha, "tuition", 5_000_000, "auto"),
        )
        decision = ReconciliationDecision.objects.get()
        self.assertEqual((decision.action, decision.actor, decision.after["status"]), ("auto_match", None, "matched"))
        self.assertEqual(self.candidates(row)[0]["studentName"], "Aisha Bello")
        self.assertEqual(self.candidates(row)[0]["className"], "Primary 3")

    def test_however_the_sender_punctuated_the_code(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        for narration in ("bg0042", "BG 0042 fees", "fees for bg/0042", "Bg_0042-tuition", "TRF BG-0042 FROM MUSA"):
            row = self.deposit(self.connection, narration=narration)
            self.assertEqual(row.reconciliation_status, "matched", narration)

    def test_a_code_inside_a_longer_one_is_not_a_match(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        for narration in ("BG-00421", "XBG-0042", "BG-0042A", "BG-0043"):
            row = self.deposit(self.connection, narration=narration)
            self.assertNotEqual(row.reconciliation_status, "matched", narration)

    def test_the_reference_can_be_in_the_payment_reference_too(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        row = self.deposit(self.connection, transaction_reference="BG-0042")
        self.assertEqual(row.reconciliation_status, "matched")

    def test_an_admission_number_alone_is_only_a_suggestion(self):
        self.make_student("BG-0042", "Aisha", "Bello", admission="2024/017")
        row = self.deposit(self.connection, narration="admission 2024/017")
        self.assertEqual((row.reconciliation_status, row.reconciliation_confidence), ("possible_match", 80))

    def test_a_short_number_could_be_anything_and_is_not_trusted(self):
        self.make_student("0042", "Aisha", "Bello")
        row = self.deposit(self.connection, narration="fees 0042")
        self.assertEqual(row.reconciliation_status, "unmatched")
        self.assertIn("student_code_short", self.signals(row))

    def test_nothing_pointing_anywhere_is_unmatched_and_says_so(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        row = self.deposit(self.connection, narration="thanks", sender_name="Nobody")
        self.assertEqual((row.reconciliation_status, row.reconciliation_confidence), ("unmatched", 0))
        self.assertEqual(row.match_reasons, [{"kind": "note", "text": "Nothing in the payment points to a student."}])
        self.assertFalse(TransactionAllocation.objects.exists())

    def test_a_student_who_has_left_is_suggested_not_matched(self):
        self.make_student("BG-0042", "Aisha", "Bello", status="graduated")
        row = self.deposit(self.connection, narration="BG-0042")
        self.assertEqual(row.reconciliation_status, "possible_match")
        self.assertIn("graduated", str(row.match_reasons))
        self.assertFalse(TransactionAllocation.objects.exists())

    def test_another_schools_student_is_never_a_candidate(self):
        self.make_student("BG-0042", "Aisha", "Bello", school=self.other_school)
        row = self.deposit(self.connection, narration="BG-0042")
        self.assertEqual(row.reconciliation_status, "unmatched")
        self.assertEqual(self.candidates(row), [])


class GuardianClueTests(EngineTestCase):
    def test_a_guardians_name_and_phone_together_suggest_but_do_not_decide(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        for phone in ("08031234567", "+234 803 123 4567", "0803-123-4567", "2348031234567"):
            row = self.deposit(self.connection, sender_name="MUSA BELLO", narration=f"fees {phone}")
            self.assertEqual((row.reconciliation_status, row.reconciliation_confidence), ("possible_match", 65), phone)

    def test_a_name_alone_is_shown_to_the_reviewer_but_matches_nothing(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello")
        row = self.deposit(self.connection, sender_name="Bello Musa Adamu", narration="fees")
        self.assertEqual(row.reconciliation_status, "unmatched")
        self.assertEqual(self.signals(row), ["guardian_name"])
        self.assertEqual(self.candidates(row)[0]["score"], 35)

    def test_accents_and_word_order_do_not_hide_a_name(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Ada Obi")
        row = self.deposit(self.connection, sender_name="ÖBI ÀDA", narration="x")
        self.assertIn("guardian_name", self.signals(row))

    def test_siblings_sharing_a_guardian_go_to_a_person_never_to_a_child(self):
        aisha = self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        self.make_student("BG-0043", "Bilal", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        row = self.deposit(self.connection, sender_name="Musa Bello", narration="school fees 08031234567")
        self.assertEqual(row.reconciliation_status, "requires_review")
        self.assertEqual(len(self.candidates(row)), 2)
        self.assertIn("2 students fit about equally well", str(row.match_reasons))
        self.assertFalse(TransactionAllocation.objects.exists())
        self.assertIsNotNone(aisha)

    def test_a_code_picks_one_sibling_out_of_several(self):
        aisha = self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        self.make_student("BG-0043", "Bilal", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        row = self.deposit(self.connection, sender_name="Musa Bello", narration="BG-0042 08031234567")
        self.assertEqual((row.reconciliation_status, row.reconciliation_confidence), ("matched", 100))
        self.assertEqual(TransactionAllocation.objects.get().student, aisha)

    def test_the_students_name_in_the_narration_adds_to_a_guardian_match(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello")
        row = self.deposit(self.connection, sender_name="Musa Bello", narration="fees for Aisha Bello")
        self.assertEqual((row.reconciliation_status, row.reconciliation_confidence), ("possible_match", 65))
        self.assertEqual(set(self.signals(row)), {"guardian_name", "student_name"})

    def test_a_wallet_account_ending_like_a_guardians_phone_only_corroborates(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello", phone="0803 123 4567")
        row = self.deposit(self.connection, sender_name="Musa Bello", sender_account_number="8031234567", narration="fees")
        self.assertEqual(row.reconciliation_confidence, 45)
        self.assertEqual(row.reconciliation_status, "unmatched")


class DuplicateTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.aisha = self.make_student("BG-0042", "Aisha", "Bello")
        self.now = timezone.now()

    def pay(self, *, hours=0, **over):
        fields = dict(sender_name="Musa Bello", narration="BG-0042", transaction_date=self.now + timedelta(hours=hours))
        fields.update(over)
        return self.deposit(self.connection, **fields)

    def test_the_same_transfer_reported_twice_is_held_not_allocated_twice(self):
        first = self.pay()
        second = self.pay(hours=2)
        self.assertEqual(first.reconciliation_status, "matched")
        self.assertEqual((second.reconciliation_status, second.duplicate_of), ("duplicate", first))
        self.assertIn("Same sender, amount and narration", str(second.match_reasons))
        self.assertEqual(TransactionAllocation.objects.count(), 1)

    def test_a_third_copy_points_at_the_original(self):
        first, _second, third = self.pay(), self.pay(hours=1), self.pay(hours=2)
        self.assertEqual(third.duplicate_of, first)

    def test_a_week_later_it_is_a_new_payment(self):
        self.pay()
        self.assertEqual(self.pay(hours=24 * 7).reconciliation_status, "matched")

    def test_a_different_sender_amount_or_narration_is_not_a_duplicate(self):
        self.pay()
        self.assertEqual(self.pay(sender_name="Ada Obi").reconciliation_status, "matched")
        self.assertEqual(self.pay(amount_minor=6_000_000).reconciliation_status, "matched")
        self.assertEqual(self.pay(narration="BG-0042 second child").reconciliation_status, "matched")

    def test_an_unnamed_sender_is_never_called_a_duplicate(self):
        self.pay(sender_name="")
        self.assertEqual(self.pay(sender_name="", hours=1).reconciliation_status, "matched")


class EngineRunTests(EngineTestCase):
    def test_debits_are_never_reconciled(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        row = self.deposit(self.connection, direction="debit", narration="BG-0042")
        self.assertEqual((row.reconciliation_status, row.engine_version), ("not_applicable", ""))
        self.assertFalse(ReconciliationDecision.objects.exists())

    def test_running_again_decides_nothing_twice(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        self.deposit(self.connection, narration="BG-0042")
        summary = reconciliation.reconcile_pending(self.school)
        self.assertEqual(summary.evaluated, 0)
        self.assertEqual((TransactionAllocation.objects.count(), ReconciliationDecision.objects.count()), (1, 1))

    def test_a_payment_a_person_already_decided_is_left_alone(self):
        row = self.deposit(self.connection, reconcile=False, narration="BG-0042")
        BankTransaction.objects.filter(id=row.id).update(reconciliation_status="unrelated_income")
        self.make_student("BG-0042", "Aisha", "Bello")
        self.assertEqual(reconciliation.reconcile_pending(self.school).evaluated, 0)

    def test_oldest_payments_are_decided_first(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        newer = self.deposit(self.connection, reconcile=False, narration="BG-0042", sender_name="Musa", transaction_date=timezone.now())
        older = self.deposit(
            self.connection, reconcile=False, narration="BG-0042", sender_name="Musa",
            transaction_date=timezone.now() - timedelta(hours=3),
        )
        reconciliation.reconcile_pending(self.school)
        newer.refresh_from_db(), older.refresh_from_db()
        self.assertEqual((older.reconciliation_status, newer.reconciliation_status, newer.duplicate_of), ("matched", "duplicate", older))

    def test_it_only_ever_touches_its_own_school(self):
        other = self.row(self.connected(who=self.other_owner, school=self.other_school)[0])
        theirs = self.deposit(other, reconcile=False, narration="BG-0042")
        self.make_student("BG-0042", "Aisha", "Bello")
        reconciliation.reconcile_pending(self.school)
        theirs.refresh_from_db()
        self.assertEqual(theirs.engine_version, "")

    def test_the_result_is_explained_signal_by_signal(self):
        self.make_student("BG-0042", "Aisha", "Bello", guardian="Musa Bello")
        row = self.deposit(self.connection, sender_name="Musa Bello", narration="BG-0042")
        signals = self.candidates(row)[0]["signals"]
        self.assertEqual({s["signal"]: s["points"] for s in signals}, {"student_code": 90, "guardian_name": 35})
        self.assertTrue(all(s["detail"] for s in signals))

    def test_a_large_school_is_not_scanned_once_per_payment(self):
        for n in range(30):
            self.make_student(f"XX-{n:04d}", f"Child{n}", "Family")
        for n in range(3):
            self.deposit(self.connection, reconcile=False, narration="hello", sender_name=f"Sender {n}")
        with mock.patch.object(matching.Directory, "__init__", autospec=True, side_effect=matching.Directory.__init__) as built:
            reconciliation.reconcile_pending(self.school)
        self.assertEqual(built.call_count, 1)

    def test_a_failing_engine_never_breaks_the_sync_and_the_next_run_catches_up(self):
        self.make_student("BG-0042", "Aisha", "Bello")
        sandbox_tools.add_feed_item(self.connection, external_transaction_id="A", narration="BG-0042")
        with mock.patch.object(reconciliation, "reconcile_pending", side_effect=RuntimeError("engine bug")):
            with self.assertLogs("apps.bankconnect.reconciliation", "ERROR"):
                outcome = sync.sync_connection(self.connection)
        self.assertEqual((outcome.ok, outcome.created), (True, 1))
        self.assertEqual(BankTransaction.objects.get().engine_version, "")
        reconciliation.reconcile_pending(self.school)
        self.assertEqual(BankTransaction.objects.get().reconciliation_status, "matched")


class EndToEndTests(EngineTestCase):
    def test_a_synced_payment_is_matched_to_the_student(self):
        aisha = self.make_student("BG-0042", "Aisha", "Bello")
        sandbox_tools.add_feed_item(self.connection, external_transaction_id="A", narration="BG-0042 tuition", amount_minor=7_500_000)
        sync.sync_connection(self.connection)
        row = BankTransaction.objects.get()
        self.assertEqual(row.reconciliation_status, "matched")
        self.assertEqual(TransactionAllocation.objects.get().student, aisha)

    def test_a_webhook_payment_is_matched_too(self):
        aisha = self.make_student("BG-0042", "Aisha", "Bello")
        made, hook = self.connected(account="0123456780")
        connection = self.row(made)
        body, headers = sandbox_tools.signed_webhook(connection, external_transaction_id="W1", narration="BG-0042")
        self.client.force_authenticate(None)
        response = self.client.post(
            f"/api/v1/{hook}", data=body, content_type="application/json", HTTP_X_SANDBOX_SIGNATURE=headers["x-sandbox-signature"]
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TransactionAllocation.objects.get().student, aisha)


class NotificationTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]
        self.delegate = self.members["staff"]
        self.give_duty(self.delegate)
        self.aisha = self.make_student("BG-0042", "Aisha", "Bello")

    def told(self, kind):
        return set(Notification.objects.filter(kind=kind).values_list("recipient__role", flat=True))

    def test_a_matched_payment_tells_the_owner_finance_and_delegates_only(self):
        self.deposit(self.connection, narration="BG-0042", sender_name="Musa Bello", amount_minor=5_000_000)
        self.assertEqual(self.told("bank_payment_received"), {"proprietor", "accountant", "staff"})
        note = Notification.objects.filter(kind="bank_payment_received", recipient=self.finance).get()
        self.assertEqual(note.title, "[Sandbox test data] Payment received")
        self.assertIn("₦50,000 from Musa Bello was matched to Aisha Bello (BG-0042) for tuition.", note.message)
        self.assertEqual(Notification.objects.filter(recipient=self.members["teacher"]).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.members["principal"]).count(), 0)

    def test_a_payment_for_a_person_to_look_at_says_so(self):
        self.deposit(self.connection, narration="who knows")
        self.assertEqual(self.told("bank_payment_review"), {"proprietor", "accountant", "staff"})
        self.assertEqual(self.told("bank_payment_received"), set())

    def test_a_batch_is_one_message_not_one_per_payment(self):
        for n in range(3):
            self.deposit(self.connection, reconcile=False, narration=f"BG-0042 n{n}", sender_name=f"S{n}", amount_minor=1_000_000)
        self.deposit(self.connection, reconcile=False, narration="mystery")
        reconciliation.reconcile_pending(self.school)
        received = Notification.objects.get(kind="bank_payment_received", recipient=self.finance)
        self.assertEqual(received.title, "[Sandbox test data] 3 payments received")
        self.assertIn("₦30,000", received.message)
        self.assertEqual(received.data["count"], 3)
        review = Notification.objects.get(kind="bank_payment_review", recipient=self.finance)
        self.assertEqual(review.data["count"], 1)

    def test_nothing_new_means_no_message(self):
        reconciliation.reconcile_pending(self.school)
        self.assertEqual(Notification.objects.count(), 0)

    def test_a_revoked_duty_no_longer_hears_about_payments(self):
        self.give_duty(self.delegate, status="revoked")
        self.deposit(self.connection, narration="BG-0042")
        self.assertEqual(self.told("bank_payment_received"), {"proprietor", "accountant"})

    def test_it_stays_inside_the_school(self):
        self.deposit(self.connection, narration="BG-0042")
        self.assertEqual(Notification.objects.filter(school=self.other_school).count(), 0)

    def test_money_is_written_the_way_people_read_it(self):
        self.assertEqual(reconciliation.format_money(5_000_000), "₦50,000")
        self.assertEqual(reconciliation.format_money(150_050), "₦1,500.50")
        self.assertEqual(reconciliation.format_money(100, "USD"), "USD 1")
