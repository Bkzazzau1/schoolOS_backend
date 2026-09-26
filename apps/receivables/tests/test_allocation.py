from datetime import timedelta

from django.db import IntegrityError, transaction

from apps.bankconnect.models import ReconciliationDecision, TransactionAllocation

from .. import adjustments, allocation, ledger, policy, schedules
from ..errors import Refused
from ..models import CreditKind, FamilyCreditEntry, FinanceAuditEvent, StudentReceivable
from .base import ReceivablesTestCase, CLOCK

N = 100


class PaymentTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.schedule = self.publish_bello_fees()
        self.ahmad_charge, self.aisha_charge, self.maryam_charge = (self.charge(s) for s in (self.ahmad, self.aisha, self.maryam))

    def pay(self, amount, **kw):
        tx = self.payment(amount)
        return tx, allocation.allocate(tx, self.family, **kw)

    def refreshed(self, receivable):
        return StudentReceivable.objects.get(pk=receivable.pk)

    def active_rows(self, tx):
        return list(TransactionAllocation.objects.filter(transaction=tx, receivable__isnull=False, superseded=False))


class SettlingTests(PaymentTestCase):
    def test_a_payment_that_covers_one_charge_settles_it_and_leaves_the_others(self):
        tx, result = self.pay(120_000 * N, prefer_student=self.ahmad)
        self.assertEqual((result.allocated_minor, result.credit_minor), (120_000 * N, 0))
        self.assertEqual(self.refreshed(self.ahmad_charge).status, "settled")
        self.assertEqual(self.refreshed(self.aisha_charge).status, "open")
        self.assertEqual(ledger.family_position(self.family).outstanding, 180_000 * N)
        self.assertLedgerHolds()

    def test_a_part_payment_makes_a_charge_partly_paid_never_settled(self):
        self.pay(50_000 * N, prefer_student=self.aisha)
        aisha = self.refreshed(self.aisha_charge)
        self.assertEqual(aisha.status, "partially_paid")
        pos = ledger.position(aisha)
        self.assertEqual((pos.paid, pos.outstanding), (50_000 * N, 50_000 * N))
        self.assertLedgerHolds()

    def test_a_payment_can_span_several_charges_and_each_row_is_traceable(self):
        tx, result = self.pay(200_000 * N, prefer_student=self.ahmad)
        rows = self.active_rows(tx)
        self.assertEqual(sum(r.amount_minor for r in rows), 200_000 * N)
        self.assertEqual({r.family_id for r in rows}, {self.family.id})
        self.assertTrue(all(r.receivable_id and r.student_id == r.receivable.student_id and r.source == "auto" for r in rows))
        self.assertEqual({r.purpose for r in rows}, {"tuition"})
        self.assertEqual(allocation.unallocated(tx), 0)
        self.assertLedgerHolds()

    def test_an_overpayment_becomes_credit_never_a_negative_balance_and_never_lost(self):
        tx, result = self.pay(400_000 * N)
        self.assertEqual((result.allocated_minor, result.credit_minor), (300_000 * N, 100_000 * N))
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (0, 100_000 * N))
        entry = FamilyCreditEntry.objects.get()
        self.assertEqual((entry.kind, entry.amount_minor, entry.transaction, entry.family), ("overpayment", 100_000 * N, tx, self.family))
        self.assertEqual(allocation.unallocated(tx), 0)
        self.assertEqual(sum(r.amount_minor for r in self.active_rows(tx)) + entry.amount_minor, tx.amount_minor)
        self.assertLedgerHolds()

    def test_a_family_that_owes_nothing_has_its_whole_payment_held_as_credit(self):
        self.pay(300_000 * N)
        tx, result = self.pay(50_000 * N)
        self.assertEqual((result.allocated_minor, result.credit_minor), (0, 50_000 * N))
        self.assertEqual(ledger.family_position(self.family).credit, 50_000 * N)

    def test_the_amounts_and_purposes_follow_the_fee_category(self):
        due = CLOCK + timedelta(days=10)
        second = schedules.create_schedule(self.school, session=self.session, name="Extras", actor=self.owner)
        for code, category in (("BUS", "transport"), ("BOOK", "books"), ("LEVY", "levy"), ("UNI", "uniform"), ("ICT", "ict")):
            schedules.add_item(second, actor=self.owner, code=code, name=code, category=category, amount_minor=1000, due_date=due, scope="student", student=self.ahmad)
        schedules.publish(second, actor=self.owner)
        tx, _ = self.pay(305_000 * N)  # everything
        purposes = {r.receivable.item_code: r.purpose for r in self.active_rows(tx)}
        self.assertEqual((purposes["BUS"], purposes["BOOK"], purposes["LEVY"], purposes["UNI"], purposes["ICT"]), ("transport", "books", "capital", "uniforms", "other"))


class NoDoubleAllocationTests(PaymentTestCase):
    def test_allocating_the_same_payment_twice_does_nothing_the_second_time(self):
        tx, first = self.pay(100_000 * N)
        again = allocation.allocate(tx, self.family)
        self.assertEqual((again.allocated_minor, again.credit_minor, again.allocations), (0, 0, []))
        self.assertEqual(sum(r.amount_minor for r in self.active_rows(tx)), 100_000 * N)
        self.assertEqual(FamilyCreditEntry.objects.count(), 0)

    def test_an_overpayment_is_not_turned_into_credit_twice(self):
        tx, _ = self.pay(400_000 * N)
        allocation.allocate(tx, self.family)
        allocation.allocate(tx, self.family)
        self.assertEqual(ledger.family_position(self.family).credit, 100_000 * N)

    def test_a_payment_can_be_allocated_in_parts_but_never_beyond_its_amount(self):
        tx = self.payment(100_000 * N)
        allocation.allocate(tx, self.family, amount_minor=40_000 * N, prefer_student=self.ahmad)
        allocation.allocate(tx, self.family, amount_minor=70_000 * N, prefer_student=self.ahmad)  # only 60,000 is left
        self.assertEqual(sum(r.amount_minor for r in self.active_rows(tx)), 100_000 * N)
        self.assertEqual(allocation.unallocated(tx), 0)

    def test_only_positive_whole_amounts_are_accepted(self):
        tx = self.payment(100 * N)
        for amount in (0, -5, 1.5, "10", True):
            with self.assertRaises(Refused, msg=repr(amount)):
                allocation.allocate(tx, self.family, amount_minor=amount)
        self.assertEqual(self.active_rows(tx), [])

    def test_the_database_refuses_a_zero_or_familyless_allocation_even_if_a_service_is_bypassed(self):
        tx = self.payment(100 * N)
        base = dict(school=self.school, transaction=tx, student=self.ahmad, purpose="tuition", source="auto")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TransactionAllocation.objects.create(amount_minor=0, family=self.family, receivable=self.ahmad_charge, **base)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TransactionAllocation.objects.create(amount_minor=5, receivable=self.ahmad_charge, **base)  # no family


class SchoolBoundaryTests(PaymentTestCase):
    def test_a_payment_from_another_school_can_never_pay_this_familys_charges(self):
        stranger = self.payment(100 * N, school=self.other_school)
        with self.assertRaises(Refused) as raised:
            allocation.allocate(stranger, self.family)
        self.assertEqual(raised.exception.code, "wrong_school")
        self.assertFalse(TransactionAllocation.objects.filter(transaction=stranger).exists())

    def test_money_going_out_is_never_allocated(self):
        out = self.payment(100 * N, direction="debit")
        with self.assertRaises(Refused) as raised:
            allocation.allocate(out, self.family)
        self.assertEqual(raised.exception.code, "not_a_credit")


class PolicyTests(PaymentTestCase):
    def make_dated_charges(self):
        """Three charges for one student on known dates: overdue, due soon, due later."""
        schedule = schedules.create_schedule(self.school, session=self.session, name="Dated", actor=self.owner)
        today = CLOCK
        for code, days in (("LATER", 90), ("OVERDUE", -10), ("SOON", 12)):
            schedules.add_item(schedule, actor=self.owner, code=code, name=code, amount_minor=1000, due_date=today + timedelta(days=days), scope="student", student=self.maryam)
        schedules.publish(schedule, actor=self.owner)
        # Settle Ahmad's and Aisha's tuition and cancel Maryam's, so only the three dated charges are owed.
        allocation.allocate(self.payment(120_000 * N), self.family, prefer_student=self.ahmad)
        allocation.allocate(self.payment(100_000 * N), self.family, prefer_student=self.aisha)
        adjustments.void_receivable(self.maryam_charge, actor=self.owner, reason="Free place")
        return {r.item_code: r for r in StudentReceivable.objects.filter(student=self.maryam).exclude(status="void")}

    def test_the_default_order_is_overdue_then_due_now_then_later(self):
        charges = self.make_dated_charges()
        self.assertEqual(set(charges), {"LATER", "OVERDUE", "SOON"})
        self.assertEqual(ledger.family_position(self.family).outstanding, 3000)
        tx = self.payment(1500)
        allocation.allocate(tx, self.family)
        rows = {r.receivable.item_code: r.amount_minor for r in self.active_rows(tx)}
        self.assertEqual(rows, {"OVERDUE": 1000, "SOON": 500})

    def test_a_preferred_student_goes_first_but_the_rest_of_the_policy_is_unchanged(self):
        ordered = policy.default_policy(
            list(StudentReceivable.objects.all()), today=CLOCK, prefer_student=self.maryam,
        )
        self.assertEqual(ordered[0].student_id, self.maryam.id)
        plain = policy.default_policy(list(StudentReceivable.objects.all()), today=CLOCK)
        self.assertEqual([r.id for r in plain], [r.id for r in policy.default_policy(list(reversed(plain)), today=CLOCK)])  # deterministic

    def test_the_policy_can_be_replaced_from_settings(self):
        from django.test import override_settings

        with override_settings(RECEIVABLES_ALLOCATION_POLICY="apps.receivables.tests.test_allocation.smallest_first"):
            tx = self.payment(90_000 * N)
            allocation.allocate(tx, self.family)
        self.assertEqual({r.receivable.student_id for r in self.active_rows(tx)}, {self.maryam.id, self.aisha.id})  # 80,000 then 10,000 of Aisha's


def smallest_first(receivables, *, today, prefer_student=None):
    return sorted(receivables, key=lambda r: (r.gross_amount_minor, str(r.id)))


class ReleasingAPaymentTests(PaymentTestCase):
    def test_a_reversed_payment_comes_out_of_the_ledger_and_the_charges_are_owed_again(self):
        tx, _ = self.pay(120_000 * N, prefer_student=self.ahmad)
        allocation.release_transaction(tx, actor=self.owner, reason="Bank reversed it")
        self.assertEqual(self.refreshed(self.ahmad_charge).status, "open")
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)
        rows = TransactionAllocation.objects.filter(transaction=tx)
        self.assertEqual((rows.count(), rows.filter(superseded=True).count()), (1, 1))  # kept, superseded
        self.assertEqual(allocation.unallocated(tx), 120_000 * N)
        self.assertLedgerHolds()

    def test_releasing_a_payment_that_created_credit_takes_the_credit_back(self):
        tx, _ = self.pay(400_000 * N)
        allocation.release_transaction(tx, actor=self.owner, reason="Refunded to the sender")
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (300_000 * N, 0))
        self.assertLedgerHolds()

    def test_if_the_credit_was_already_used_the_charges_it_paid_come_due_again(self):
        tx, _ = self.pay(400_000 * N)  # 100,000 credit
        due = CLOCK + timedelta(days=40)
        nxt = schedules.create_schedule(self.school, session=self.session, name="Next", actor=self.owner)
        schedules.add_item(nxt, actor=self.owner, code="NEXT", name="Next", amount_minor=100_000 * N, due_date=due, scope="student", student=self.ahmad)
        schedules.publish(nxt, actor=self.owner)  # the credit pays it automatically
        self.assertEqual(ledger.family_position(self.family).outstanding, 0)
        allocation.release_transaction(tx, actor=self.owner, reason="Reversed")
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (400_000 * N, 0))
        self.assertLedgerHolds()

    def test_releasing_twice_is_harmless(self):
        tx, _ = self.pay(100_000 * N)
        allocation.release_transaction(tx, actor=self.owner, reason="Reversed")
        allocation.release_transaction(tx, actor=self.owner, reason="Reversed")
        self.assertEqual(ledger.family_position(self.family).outstanding, 300_000 * N)
        self.assertLedgerHolds()

    def test_a_released_payment_can_be_allocated_again(self):
        tx, _ = self.pay(100_000 * N)
        allocation.release_transaction(tx, actor=self.owner, reason="Wrongly matched")
        allocation.allocate(tx, self.family)
        self.assertEqual(sum(r.amount_minor for r in self.active_rows(tx)), 100_000 * N)
        self.assertLedgerHolds()


class CorrectingAnAllocationTests(PaymentTestCase):
    def setUp(self):
        super().setUp()
        self.tx, _ = self.pay(120_000 * N, prefer_student=self.ahmad)
        self.accountant = self.members["accountant"]

    def correct(self, plan, actor=None, reason="Parent said it was for Aisha", **kw):
        return allocation.correct_allocations(self.tx, plan, actor=actor or self.accountant, reason=reason, **kw)

    def test_the_finance_office_moves_a_payment_and_the_old_allocation_stays_on_record(self):
        self.correct([(self.aisha_charge, 100_000 * N), (self.maryam_charge, 20_000 * N)])
        rows = self.active_rows(self.tx)
        self.assertEqual({(r.receivable.student_id, r.amount_minor, r.source) for r in rows}, {(self.aisha.id, 100_000 * N, "manual"), (self.maryam.id, 20_000 * N, "manual")})
        self.assertEqual(TransactionAllocation.objects.filter(transaction=self.tx, superseded=True).count(), 1)  # Ahmad's, kept
        self.assertEqual(self.refreshed(self.ahmad_charge).status, "open")
        self.assertEqual(self.refreshed(self.aisha_charge).status, "settled")
        self.assertLedgerHolds()

    def test_whatever_the_plan_does_not_place_is_held_as_credit_and_used_at_once_if_the_family_owes(self):
        result = self.correct([(self.aisha_charge, 60_000 * N)])
        self.assertEqual((result.allocated_minor, result.credit_minor), (60_000 * N, 60_000 * N))
        pos = ledger.family_position(self.family)
        # The family still owes, so the credit is never left idle: the whole 120,000 is working for it.
        self.assertEqual((pos.credit, pos.paid), (0, 120_000 * N))
        self.assertEqual(FamilyCreditEntry.objects.filter(kind=CreditKind.OVERPAYMENT).count(), 1)
        # (Which charge the credit lands on first depends on the policy's tie-break, so only the total is asserted.)
        applied = sum(e.amount_minor for e in FamilyCreditEntry.objects.filter(kind=CreditKind.APPLIED))
        self.assertEqual(applied, 60_000 * N)
        self.assertLedgerHolds()

    def test_the_owner_may_too_and_nobody_else_may(self):
        self.correct([(self.aisha_charge, 10 * N)], actor=self.owner)
        for role in ("teacher", "parent", "student", "principal"):
            with self.assertRaises(Refused, msg=role) as raised:
                self.correct([(self.aisha_charge, 10 * N)], actor=self.members[role])
            self.assertEqual(raised.exception.code, "not_authorised")
        with self.assertRaises(Refused):
            self.correct([(self.aisha_charge, 10 * N)], actor=self.other_owner)

    def test_a_reason_and_a_plan_are_required(self):
        with self.assertRaises(Refused) as raised:
            self.correct([(self.aisha_charge, 10 * N)], reason=" ")
        self.assertEqual(raised.exception.code, "reason_required")
        with self.assertRaises(Refused) as raised:
            self.correct([])
        self.assertEqual(raised.exception.code, "empty_plan")

    def test_a_plan_cannot_ask_for_more_than_the_payment_or_more_than_a_charge_needs(self):
        with self.assertRaises(Refused) as raised:
            self.correct([(self.aisha_charge, 100_000 * N), (self.maryam_charge, 30_000 * N)])
        self.assertEqual(raised.exception.code, "over_allocated")
        with self.assertRaises(Refused) as raised:
            self.correct([(self.maryam_charge, 90_000 * N)])  # Maryam owes only 80,000
        self.assertEqual(raised.exception.code, "over_allocated")
        # ...and a refused correction changes nothing.
        self.assertEqual({r.receivable.student_id for r in self.active_rows(self.tx)}, {self.ahmad.id})
        self.assertLedgerHolds()

    def test_bad_amounts_are_refused(self):
        for amount in (0, -1, 1.5, "5", True):
            with self.assertRaises(Refused, msg=repr(amount)):
                self.correct([(self.aisha_charge, amount)])

    def test_a_plan_is_for_one_family_and_never_another_schools_charge(self):
        other = self.make_student("Zed", "Other", school=self.other_school)
        from .. import families as fam

        other_family, _ = fam.ensure_family_for_student(self.other_school, other)
        with self.assertRaises(Refused):
            self.correct([(self.aisha_charge, 10 * N), (self._foreign_charge(other, other_family), 10 * N)])
        with self.assertRaises(Refused) as raised:
            self.correct([(self._foreign_charge(other, other_family), 10 * N)])
        self.assertEqual(raised.exception.code, "wrong_school")

    def _foreign_charge(self, student, family):
        existing = StudentReceivable.objects.filter(student=student).first()
        if existing is not None:
            return existing
        session, term, primary, _ = self.make_year(self.other_school)
        self.enroll(student, primary, session)
        schedule = schedules.create_schedule(self.other_school, session=session, name="Theirs", actor=self.other_owner)
        schedules.add_item(schedule, actor=self.other_owner, code="X", name="X", amount_minor=50_000 * N, due_date=CLOCK + timedelta(days=5), scope="student", student=student)
        schedules.publish(schedule, actor=self.other_owner)
        return StudentReceivable.objects.get(student=student)

    def test_a_void_charge_cannot_be_paid(self):
        adjustments.void_receivable(self.aisha_charge, actor=self.owner, reason="Error")
        with self.assertRaises(Refused) as raised:
            self.correct([(self.refreshed(self.aisha_charge), 10 * N)])
        self.assertEqual(raised.exception.code, "receivable_not_payable")

    def test_a_correction_is_audited_and_can_write_the_payments_own_history(self):
        decision = allocation.decision_for(self.tx, action="reallocate", actor=self.accountant, note="Moved")
        self.correct([(self.aisha_charge, 10 * N)], decision=decision)
        event = FinanceAuditEvent.objects.get(kind="allocation_corrected")
        self.assertEqual((event.actor, event.detail["reason"]), (self.accountant, "Parent said it was for Aisha"))
        self.assertTrue(ReconciliationDecision.objects.filter(transaction=self.tx, action="reallocate").exists())
        self.assertEqual(TransactionAllocation.objects.filter(transaction=self.tx, decision=decision, superseded=False).count(), 1)
