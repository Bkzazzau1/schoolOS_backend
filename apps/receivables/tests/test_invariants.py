"""Whatever order things happen in, the ledger must stay true.

A seeded random sequence of every kind of operation is run against a family, and after EACH step the whole
ledger is checked: no charge is over-paid or negative, every status matches the ledger, credit is never
negative, and money is conserved - each payment is either fully accounted for (allocated to charges plus
held as credit) or not counted at all, never partly, never more than it was.
"""

import random
from datetime import date, timedelta

from apps.bankconnect.models import BankTransaction, TransactionAllocation

from .. import adjustments, allocation, credit, ledger, schedules
from ..errors import Refused
from ..models import CreditKind, FamilyCreditEntry, ReceivableAdjustment, StudentReceivable
from .base import ReceivablesTestCase

N = 100
SEEDS = (1, 7, 42, 2026, 31337)
STEPS = 22


class RandomLedgerTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        self.students = [self.ahmad, self.aisha, self.maryam]
        self.payments = []
        self.fee_number = 0

    # -- the operations -----------------------------------------------------------------------

    def pay(self, rng):
        tx = self.payment(rng.randint(1, 200) * 1000 * N // 10)
        allocation.allocate(tx, self.family, prefer_student=rng.choice(self.students + [None]))
        self.payments.append(tx)

    def adjust(self, rng):
        live = [r for r in StudentReceivable.objects.filter(family=self.family).exclude(status="void") if ledger.position(r).net > 0]
        if not live:
            return
        r = rng.choice(live)
        amount = rng.randint(1, ledger.position(r).net)
        adjustments.adjust(r, kind=rng.choice(["scholarship", "discount", "waiver", "correction"]), amount_minor=amount, reason="random", actor=self.owner)

    def reverse(self, rng):
        open_ones = [a for a in ReceivableAdjustment.objects.filter(reverses__isnull=True) if not ReceivableAdjustment.objects.filter(reverses=a).exists()]
        if open_ones:
            try:
                adjustments.reverse(rng.choice(open_ones), actor=self.owner, reason="random")
            except Refused:  # a charge that was voided since cannot have its adjustment reversed: fine
                pass

    def void(self, rng):
        live = list(StudentReceivable.objects.filter(family=self.family).exclude(status="void"))
        if len(live) > 1:
            adjustments.void_receivable(rng.choice(live), actor=self.owner, reason="random")

    def release(self, rng):
        if self.payments:
            try:
                allocation.release_transaction(rng.choice(self.payments), actor=self.owner, reason="random")
            except Refused as refused:
                # Money already refunded to the family cannot be taken back: refused, and nothing changes.
                self.assertEqual(refused.code, "credit_in_use")

    def reallocate(self, rng):
        owing = [r for r in StudentReceivable.objects.filter(family=self.family).exclude(status="void")]
        if not (self.payments and owing):
            return
        tx = rng.choice(self.payments)
        target = rng.choice(owing)
        try:
            allocation.correct_allocations(tx, [(target, rng.randint(1, tx.amount_minor))], actor=self.members["accountant"], reason="random")
        except Refused:  # asking for more than the charge needs, or a void charge: refused, and nothing changes
            pass

    def refund(self, rng):
        balance = ledger.credit_balance(self.family)
        if balance > 0:
            credit.refund(self.family, rng.randint(1, balance), actor=self.owner, reason="random")

    def new_fees(self, rng):
        self.fee_number += 1
        s = schedules.create_schedule(self.school, session=self.session, name=f"More {self.fee_number}", actor=self.owner)
        schedules.add_item(s, actor=self.owner, code=f"X{self.fee_number}", name="Extra", amount_minor=rng.randint(1, 90) * 1000 * N,
                           due_date=date.today() + timedelta(days=rng.randint(-20, 90)), scope="student", student=rng.choice(self.students))
        schedules.publish(s, actor=self.owner)

    # -- the checks ---------------------------------------------------------------------------

    def check(self, step, operation):
        where = f"after step {step} ({operation})"
        self.assertEqual(ledger.verify_family(self.family), [], where)
        for tx in BankTransaction.objects.filter(school=self.school):
            counted = (
                sum(a.amount_minor for a in TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False))
                + credit.total_credit_from(tx)
            )
            self.assertIn(counted, (0, tx.amount_minor), f"{where}: payment {tx.id} is partly counted ({counted} of {tx.amount_minor})")
        pos = ledger.family_position(self.family)
        self.assertGreaterEqual(pos.outstanding, 0, where)
        self.assertGreaterEqual(pos.credit, 0, where)
        # credit and debt never sit side by side: if the family owes anything, no credit is left idle
        self.assertFalse(pos.outstanding > 0 and pos.credit > 0, f"{where}: {pos.credit} credit sits beside {pos.outstanding} owed")
        # the credit ledger's own arithmetic
        in_ = sum(e.amount_minor for e in FamilyCreditEntry.objects.filter(family=self.family, kind__in=(CreditKind.OVERPAYMENT, CreditKind.RELEASED, CreditKind.APPLICATION_REVERSED)))
        out = sum(e.amount_minor for e in FamilyCreditEntry.objects.filter(family=self.family, kind__in=(CreditKind.APPLIED, CreditKind.REFUNDED, CreditKind.OVERPAYMENT_REVERSED)))
        self.assertEqual(pos.credit, in_ - out, where)
        # every charge's gross is exactly what was published
        for r in StudentReceivable.objects.filter(family=self.family):
            self.assertEqual(r.gross_amount_minor, r.fee_item.amount_minor if r.installment_count == 1 else r.gross_amount_minor, where)

    def run_seed(self, seed):
        rng = random.Random(seed)
        operations = [self.pay, self.pay, self.pay, self.adjust, self.adjust, self.reverse, self.void, self.release, self.reallocate, self.refund, self.new_fees]
        for step in range(STEPS):
            operation = rng.choice(operations)
            operation(rng)
            self.check(step, operation.__name__)


def _seeded(seed):
    def test(self):
        self.run_seed(seed)

    test.__name__ = f"test_the_ledger_stays_true_whatever_happens_seed_{seed}"
    return test


# One test per seed, so each starts from a fresh database and a failure names the seed that reproduces it.
for _seed in SEEDS:
    _test = _seeded(_seed)
    setattr(RandomLedgerTests, _test.__name__, _test)
