"""Where a confirmed debit meets the receivables ledger: only a confirmed debit settles anything, it settles exactly once, the receivables stay the
only truth about what is owed, and a reversal takes it back out without deleting anything."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bankconnect.models import BankTransaction, TransactionAllocation
from apps.receivables.models import FamilyCreditEntry

from .. import execution, jobs, settlement
from ..constants import DebitOutcome, InstructionStatus
from ..models import MandateTransaction, SandboxDebit
from ..providers import sandbox
from .base import N, MandateTestCase


class SettlementTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.active_mandate(self.bello, maximum=500_000)  # the mandate's maximum is more than what is owed
        self.active_mandate(self.sani, account="0987654321")

    def run_for_bello_only(self):
        from .. import debit_batches

        batch = self.new_batch()
        debit_batches.set_selection(self.maker, batch.id, select=[str(self.item(batch, self.bello).id)])
        batch = self.refetch(batch)
        debit_batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        debit_batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        return self.run_batch(self.refetch(batch))

    def test_a_confirmed_debit_becomes_one_canonical_payment_settled_by_the_ordinary_allocation(self):
        batch = self.run_for_bello_only()
        tx = MandateTransaction.objects.get()
        payment = tx.payment
        self.assertEqual((payment.transaction_type, payment.connection_id, payment.family_id, payment.amount_minor), ("direct_debit", None, self.bello.id, 280_000 * N))
        self.assertEqual((payment.reconciliation_status, payment.provider, payment.direction), ("matched", "sandbox", "credit"))
        allocations = TransactionAllocation.objects.filter(transaction=payment, receivable__isnull=False, superseded=False)
        self.assertEqual(sum(a.amount_minor for a in allocations), 280_000 * N)
        self.assertEqual(self.owed(self.bello), 0)  # the ledger, and only the ledger, says what is owed
        self.assertEqual(self.refetch(batch).status, "completed")
        self.assertLedgerHolds(self.bello)

    def test_the_mandates_maximum_never_becomes_a_debit_or_a_balance(self):
        self.run_for_bello_only()
        self.assertEqual(SandboxDebit.objects.get().amount_minor, 280_000 * N)  # not 500,000
        self.assertEqual(FamilyCreditEntry.objects.filter(family=self.bello).count(), 0)  # nothing over-debited, nothing held as credit

    def test_an_unrelated_family_never_receives_the_settlement(self):
        self.run_for_bello_only()
        self.assertEqual(self.owed(self.sani), 100_000 * N)
        self.assertFalse(TransactionAllocation.objects.filter(family=self.sani).exists())
        self.assertFalse(MandateTransaction.objects.filter(family=self.sani).exists())
        self.assertLedgerHolds(self.sani)

    def test_a_pending_failed_or_unknown_debit_settles_nothing(self):
        for status in ("pending", "failed"):
            sandbox.set_next_debit_status(status)
        batch = self.approved()
        execution.start(self.maker, batch.id)
        jobs.drain()
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 0)
        self.assertEqual((self.owed(self.bello) + self.owed(self.sani)), 380_000 * N)
        sandbox.inject_fault("create_debit", "timeout_after")  # an unknown one, too
        self.assertEqual(TransactionAllocation.objects.filter(family__in=[self.bello, self.sani]).count(), 0)

    def test_settling_the_same_debit_twice_pays_once(self):
        self.run_for_bello_only()
        tx = MandateTransaction.objects.get()
        first = tx.payment_id
        self.assertEqual(settlement.settle(tx).id, first)
        self.assertEqual(settlement.settle(self.reload(tx)).id, first)
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 1)
        self.assertEqual(self.owed(self.bello), 0)

    def test_a_repeated_confirmation_from_the_provider_settles_once(self):
        batch = self.run_for_bello_only()
        item = self.item(batch, self.bello)
        for _ in range(3):  # callbacks and requeries that keep saying it succeeded
            execution.requery_instruction(item.id, force=True)
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 1)
        self.assertEqual((MandateTransaction.objects.count(), self.owed(self.bello)), (1, 0))

    def test_the_database_refuses_two_debits_sharing_one_payment_and_a_payment_for_an_unconfirmed_debit(self):
        self.run_batch(self.approved())
        first, second = list(MandateTransaction.objects.order_by("created_at"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateTransaction.objects.filter(pk=second.pk).update(payment=first.payment_id)  # one payment, one debit
        other = self.payment(1_000 * N)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateTransaction.objects.filter(pk=first.pk).update(payment=other, status=DebitOutcome.FAILED)  # only a confirmed debit is ever a payment

    def test_a_provider_reversal_takes_the_payment_back_out_and_the_family_owes_it_again(self):
        batch = self.run_for_bello_only()
        item = self.item(batch, self.bello)
        SandboxDebit.objects.update(status="reversed")
        execution.requery_instruction(item.id, force=True)
        tx = MandateTransaction.objects.get()
        self.assertEqual((tx.status, tx.reversed_at is not None), (DebitOutcome.REVERSED, True))
        self.assertEqual(self.owed(self.bello), 280_000 * N)  # owed again, exactly as before
        self.assertEqual(BankTransaction.objects.get(pk=tx.payment_id).reconciliation_status, "reversed")
        self.assertEqual(TransactionAllocation.objects.filter(transaction=tx.payment, superseded=False).count(), 0)
        self.assertTrue(TransactionAllocation.objects.filter(transaction=tx.payment, superseded=True).exists())  # kept, superseded: nothing was deleted
        self.assertLedgerHolds(self.bello)

    def test_a_refund_is_recorded_like_a_reversal_and_only_once(self):
        batch = self.run_for_bello_only()
        item = self.item(batch, self.bello)
        SandboxDebit.objects.update(status="refunded")
        execution.requery_instruction(item.id, force=True)
        execution.requery_instruction(item.id, force=True)  # the same news again
        tx = MandateTransaction.objects.get()
        self.assertEqual((tx.status, tx.refunded_at is not None, self.owed(self.bello)), (DebitOutcome.REFUNDED, True, 280_000 * N))
        self.assertEqual(self.item(batch, self.bello).status, InstructionStatus.SUCCESS)  # the history of the debit is kept

    def test_a_confirmed_excess_after_the_family_paid_meanwhile_becomes_family_credit_and_is_not_lost(self):
        sandbox.inject_fault("create_debit", "timeout_after")  # the provider debits 280,000 but the answer is lost
        batch = self.approved()
        execution.start(self.maker, batch.id)
        jobs.run_next()
        jobs.run_next()
        self.pay_manually(self.bello, 100_000)  # the family also paid another way, before SchoolOS found out
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))
        self.assertEqual(self.owed(self.bello), 0)
        credit = sum(e.amount_minor for e in FamilyCreditEntry.objects.filter(family=self.bello, kind="overpayment"))
        self.assertEqual(credit, 100_000 * N)  # 280,000 debited, 180,000 owed: the rest is credit, not vanished
        self.assertLedgerHolds(self.bello)

    def test_direct_debit_payments_are_not_smart_money_collection_payments(self):
        self.run_for_bello_only()
        from apps.bankconnect import summary

        body = summary.build(self.school, period="all", include_sandbox=True)
        self.assertEqual(body["selected"]["amountMinor"], 0)  # not counted as what a collection provider reported
        self.assertEqual(body["byProvider"], [])

    def test_a_smart_money_collection_payment_and_a_direct_debit_do_not_duplicate_each_other(self):
        self.pay_manually(self.bello, 80_000)  # a payment through the other path
        self.assertEqual(self.owed(self.bello), 200_000 * N)
        batch = self.new_batch()
        self.assertEqual(self.item(batch, self.bello).proposed_debit_minor, 200_000 * N)  # the debit is only for what is still owed
        from .. import debit_batches

        debit_batches.set_selection(self.maker, batch.id, select=[str(self.item(batch, self.bello).id)])
        batch = self.refetch(batch)
        debit_batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        debit_batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        self.run_batch(self.refetch(batch))
        self.assertEqual(self.owed(self.bello), 0)
        self.assertEqual(BankTransaction.objects.filter(family=self.bello).count(), 2)
        paid = sum(a.amount_minor for a in TransactionAllocation.objects.filter(family=self.bello, receivable__isnull=False, superseded=False))
        self.assertEqual(paid, 280_000 * N)  # each kobo of the fees paid once, by one path

    def test_a_debit_pays_only_the_charges_of_the_scope_it_was_approved_for(self):
        family = self.make_family("Twoterms", owes=100_000)
        self.charge_family(family, [family.members.first().student], 50_000, term=self.term2)
        self.active_mandate(family, account="0444444444")
        batch = self.approved(term=self.term2)
        self.run_batch(batch)
        item = self.item(batch, family)
        self.assertEqual(item.proposed_debit_minor, 50_000 * N)
        self.assertEqual(self.owed(family), 100_000 * N)  # only the second term's charge was paid; the first is still owed
