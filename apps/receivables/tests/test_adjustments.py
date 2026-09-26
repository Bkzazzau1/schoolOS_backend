from datetime import date, timedelta

from django.core.exceptions import ValidationError

from apps.bankconnect.models import TransactionAllocation

from .. import adjustments, allocation, ledger, schedules
from ..errors import Refused
from ..models import FinanceAuditEvent, ReceivableAdjustment, StudentReceivable
from .base import ReceivablesTestCase

N = 100


class AdjustmentTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.authority = self.members["principal"]
        self.give_duty(self.authority)
        self.bello_family()
        self.schedule = self.publish_bello_fees()
        self.tuition = self.charge(self.ahmad)  # 120,000

    def adjust(self, receivable=None, kind="scholarship", amount=20_000 * N, actor=None, **over):
        return adjustments.adjust(
            receivable or self.tuition, kind=kind, amount_minor=amount, reason=over.pop("reason", "Founder scholarship"),
            actor=actor or self.owner, **over,
        )

    def position(self, receivable=None):
        return ledger.position(StudentReceivable.objects.get(pk=(receivable or self.tuition).pk))


class WhoMayAdjustTests(AdjustmentTestCase):
    def test_the_owner_and_a_delegate_may(self):
        self.assertEqual(self.adjust(actor=self.owner).authorized_by, self.owner)
        self.assertEqual(self.adjust(actor=self.authority, amount=1000).authorized_by, self.authority)

    def test_nobody_else_may_not_even_the_finance_office(self):
        for role in ("accountant", "administrator", "teacher", "parent", "student"):
            with self.assertRaises(Refused, msg=role) as raised:
                self.adjust(actor=self.members[role])
            self.assertEqual(raised.exception.code, "not_billing_authority")
        with self.assertRaises(Refused):
            self.adjust(actor=self.other_owner)
        with self.assertRaises(Refused):
            adjustments.adjust(self.tuition, kind="discount", amount_minor=100, reason="x", actor=None)
        self.assertFalse(ReceivableAdjustment.objects.exists())

    def test_a_delegate_whose_duty_is_revoked_loses_the_power(self):
        self.give_duty(self.authority, status="revoked")
        with self.assertRaises(Refused):
            self.adjust(actor=self.authority)


class MakingAnAdjustmentTests(AdjustmentTestCase):
    def test_it_is_recorded_with_who_when_and_why_and_the_gross_never_changes(self):
        adjustment = self.adjust(actor=self.authority, requested_by=self.members["accountant"])
        self.assertEqual(
            (adjustment.kind, adjustment.amount_minor, adjustment.reason, adjustment.requested_by, adjustment.authorized_by),
            ("scholarship", 20_000 * N, "Founder scholarship", self.members["accountant"], self.authority),
        )
        self.assertIsNotNone(adjustment.authorized_at)
        self.tuition.refresh_from_db()
        self.assertEqual(self.tuition.gross_amount_minor, 120_000 * N)
        pos = self.position()
        self.assertEqual((pos.gross, pos.adjustments, pos.net, pos.outstanding), (120_000 * N, 20_000 * N, 100_000 * N, 100_000 * N))
        self.assertLedgerHolds()

    def test_every_kind_is_accepted_and_nonsense_is_not(self):
        for kind in ("scholarship", "discount", "waiver", "correction", "other"):
            self.adjust(kind=kind, amount=100)
        for kind in ("gift", "", None, "SCHOLARSHIP"):
            with self.assertRaises(Refused, msg=repr(kind)) as raised:
                self.adjust(kind=kind)
            self.assertEqual(raised.exception.code, "invalid_kind")

    def test_the_amount_must_be_a_positive_whole_number_of_kobo_and_not_more_than_is_payable(self):
        for amount in (0, -1, 10.5, "100", True, None):
            with self.assertRaises(Refused, msg=repr(amount)) as raised:
                self.adjust(amount=amount)
            self.assertEqual(raised.exception.code, "invalid_amount")
        with self.assertRaises(Refused) as raised:
            self.adjust(amount=120_000 * N + 1)
        self.assertEqual(raised.exception.code, "exceeds_charge")
        self.assertFalse(ReceivableAdjustment.objects.exists())

    def test_a_reason_is_required(self):
        for reason in ("", "   ", None):
            with self.assertRaises(Refused) as raised:
                self.adjust(reason=reason)
            self.assertEqual(raised.exception.code, "reason_required")

    def test_adjustments_add_up_but_can_never_make_a_charge_negative(self):
        self.adjust(amount=50_000 * N)
        self.adjust(kind="discount", amount=50_000 * N)
        with self.assertRaises(Refused) as raised:
            self.adjust(amount=20_001 * N)
        self.assertEqual(raised.exception.code, "exceeds_charge")
        self.assertEqual(self.position().net, 20_000 * N)
        self.adjust(kind="waiver", amount=20_000 * N)  # the rest can be waived: the charge is then settled
        self.tuition.refresh_from_db()
        self.assertEqual((self.position().net, self.tuition.status), (0, "settled"))
        with self.assertRaises(Refused):
            self.adjust(amount=1)
        self.assertLedgerHolds()

    def test_the_same_decision_applied_twice_is_one_decision(self):
        first = self.adjust(source_ref="concession:CNC-1")
        again = self.adjust(source_ref="concession:CNC-1")
        self.assertEqual(first.id, again.id)
        self.assertEqual(ReceivableAdjustment.objects.count(), 1)
        self.assertEqual(self.position().adjustments, 20_000 * N)

    def test_an_adjustment_cannot_be_edited_or_deleted(self):
        adjustment = self.adjust()
        adjustment.amount_minor = 1
        with self.assertRaises(ValidationError):
            adjustment.save()
        with self.assertRaises(ValidationError):
            adjustment.delete()

    def test_it_is_audited_without_secrets(self):
        self.adjust(actor=self.authority, reason="Founder scholarship")
        event = FinanceAuditEvent.objects.get(kind="adjustment_applied")
        self.assertEqual((event.actor, event.detail["amount"], event.detail["reason"], event.detail["adjustment"]), (self.authority, 20_000 * N, "Founder scholarship", "scholarship"))

    def test_another_schools_charge_is_out_of_reach(self):
        with self.assertRaises(Refused):
            adjustments.adjust(self.tuition, kind="discount", amount_minor=100, reason="x", actor=self.other_owner)


class ReversalTests(AdjustmentTestCase):
    def test_a_reversal_restores_the_charge_and_keeps_both_records(self):
        original = self.adjust()
        reversal = adjustments.reverse(original, actor=self.authority, reason="Awarded in error")
        self.assertEqual((reversal.reverses, reversal.amount_minor, reversal.kind), (original, original.amount_minor, "scholarship"))
        self.assertEqual(ReceivableAdjustment.objects.count(), 2)
        self.assertEqual(self.position().net, 120_000 * N)
        self.assertLedgerHolds()

    def test_a_reversal_needs_authority_and_a_reason(self):
        original = self.adjust()
        with self.assertRaises(Refused):
            adjustments.reverse(original, actor=self.members["accountant"], reason="x")
        with self.assertRaises(Refused) as raised:
            adjustments.reverse(original, actor=self.owner, reason=" ")
        self.assertEqual(raised.exception.code, "reason_required")

    def test_an_adjustment_is_reversed_once_and_a_reversal_is_never_reversed(self):
        original = self.adjust()
        reversal = adjustments.reverse(original, actor=self.owner, reason="Error")
        with self.assertRaises(Refused) as raised:
            adjustments.reverse(original, actor=self.owner, reason="Again")
        self.assertEqual(raised.exception.code, "already_reversed")
        with self.assertRaises(Refused) as raised:
            adjustments.reverse(reversal, actor=self.owner, reason="Undo the undo")
        self.assertEqual(raised.exception.code, "already_a_reversal")

    def test_reversing_after_the_charge_was_part_paid_leaves_it_part_paid(self):
        original = self.adjust(amount=120_000 * N)  # the whole charge waived: settled
        tuition = StudentReceivable.objects.get(pk=self.tuition.pk)
        self.assertEqual(tuition.status, "settled")
        adjustments.reverse(original, actor=self.owner, reason="Waiver withdrawn")
        tuition.refresh_from_db()
        self.assertEqual((tuition.status, self.position().outstanding), ("open", 120_000 * N))

    def test_it_is_audited(self):
        adjustments.reverse(self.adjust(), actor=self.owner, reason="Error")
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="adjustment_reversed", actor=self.owner).exists())


class InstalmentTests(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        due = date.today() + timedelta(days=5)
        schedule = schedules.create_schedule(self.school, session=self.session, term=self.term, name="Plan term", actor=self.owner)
        plan = [{"basisPoints": 5000, "dueDate": (due).isoformat()}, {"basisPoints": 3000, "dueDate": (due + timedelta(days=30)).isoformat()}, {"basisPoints": 2000, "dueDate": (due + timedelta(days=60)).isoformat()}]
        schedules.add_item(schedule, actor=self.owner, code="TUI", name="Tuition", category="tuition", amount_minor=12_000_001, plan=plan, scope="student", student=self.ahmad)
        schedules.publish(schedule, actor=self.owner)
        self.parts = list(StudentReceivable.objects.filter(student=self.ahmad).order_by("installment_number"))

    def test_a_scholarship_on_a_charge_is_spread_across_every_instalment_and_adds_up_exactly(self):
        made = adjustments.adjust_charge(self.parts[0], kind="scholarship", amount_minor=2_000_003, reason="Founder scholarship", actor=self.owner)
        self.assertEqual(sum(a.amount_minor for a in made), 2_000_003)
        self.assertEqual(len({a.group_id for a in made}), 1)
        self.assertEqual(len(made), 3)
        nets = [ledger.position(StudentReceivable.objects.get(pk=p.pk)).net for p in self.parts]
        self.assertEqual(sum(nets), 12_000_001 - 2_000_003)
        # in proportion to each instalment's share (50 / 30 / 20 percent), give or take a kobo
        shares = [a.amount_minor for a in sorted(made, key=lambda a: a.receivable.installment_number)]
        for share, ratio in zip(shares, (0.5, 0.3, 0.2)):
            self.assertAlmostEqual(share / 2_000_003, ratio, delta=0.001)
        self.assertLedgerHolds()

    def test_the_same_scholarship_applied_twice_is_applied_once(self):
        first = adjustments.adjust_charge(self.parts[1], kind="scholarship", amount_minor=1_000_000, reason="x", actor=self.owner, source_ref="concession:C1")
        again = adjustments.adjust_charge(self.parts[2], kind="scholarship", amount_minor=1_000_000, reason="x", actor=self.owner, source_ref="concession:C1")
        self.assertEqual({a.id for a in first}, {a.id for a in again})
        self.assertEqual(ReceivableAdjustment.objects.count(), 3)

    def test_more_than_the_whole_charge_still_payable_is_refused(self):
        with self.assertRaises(Refused) as raised:
            adjustments.adjust_charge(self.parts[0], kind="discount", amount_minor=12_000_002, reason="x", actor=self.owner)
        self.assertEqual(raised.exception.code, "exceeds_charge")
        self.assertFalse(ReceivableAdjustment.objects.exists())

    def test_a_charge_that_has_been_partly_adjusted_takes_the_rest_in_proportion_to_what_remains(self):
        adjustments.adjust(self.parts[0], kind="discount", amount_minor=6_000_000, reason="First instalment waived mostly", actor=self.owner)
        made = adjustments.adjust_charge(self.parts[0], kind="scholarship", amount_minor=3_000_000, reason="More", actor=self.owner)
        for adjustment in made:
            self.assertLessEqual(adjustment.amount_minor, ledger.position(StudentReceivable.objects.get(pk=adjustment.receivable_id)).gross)
        self.assertLedgerHolds()

    def test_the_spread_is_exact_and_never_gives_a_part_more_than_it_needs(self):
        for amount, weights in ((2, [1, 1, 1]), (7, [3, 3, 1]), (100, [50, 30, 20]), (1, [5, 5]), (99, [33, 33, 34]), (4, [3, 3, 1])):
            shares = adjustments._spread(amount, weights)
            self.assertEqual(sum(shares), amount)
            self.assertTrue(all(s <= w for s, w in zip(shares, weights)), (amount, weights, shares))


class AdjustmentAfterPaymentTests(AdjustmentTestCase):
    def pay(self, amount, **kw):
        # Aimed at Ahmad, so his charge is the one that gets paid first.
        kw.setdefault("prefer_student", self.ahmad)
        return allocation.allocate(self.payment(amount), self.family, **kw)

    def test_a_scholarship_after_the_fee_was_paid_releases_the_excess_as_credit_never_loses_it(self):
        # Only Ahmad owes anything, so nothing else can use the credit.
        for student in (self.aisha, self.maryam):
            adjustments.adjust(self.charge(student), kind="waiver", amount_minor=self.charge(student).gross_amount_minor, reason="Free place", actor=self.owner)
        self.pay(120_000 * N)
        self.assertEqual(self.position().outstanding, 0)
        self.adjust(amount=20_000 * N)
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (0, 20_000 * N))  # the 20,000 is credit, visibly
        self.assertEqual(sum(a.amount_minor for a in TransactionAllocation.objects.filter(receivable=self.tuition, superseded=False)), 100_000 * N)
        # the original allocation is still on record, superseded
        self.assertEqual(TransactionAllocation.objects.filter(receivable=self.tuition, superseded=True).count(), 1)
        self.tuition.refresh_from_db()
        self.assertEqual(self.tuition.status, "settled")
        self.assertLedgerHolds()

    def test_the_released_money_goes_straight_to_what_else_the_family_owes(self):
        self.pay(120_000 * N)  # Ahmad's charge is paid
        self.assertEqual(StudentReceivable.objects.get(pk=self.tuition.pk).status, "settled")
        before = ledger.family_position(self.family)
        self.assertEqual(before.outstanding, 180_000 * N)
        self.adjust(amount=20_000 * N)  # frees 20,000, which the family's other charges immediately use
        after = ledger.family_position(self.family)
        self.assertEqual((after.outstanding, after.credit), (160_000 * N, 0))
        self.assertLedgerHolds()

    def test_voiding_a_paid_charge_releases_everything_paid_as_credit(self):
        for student in (self.aisha, self.maryam):
            adjustments.adjust(self.charge(student), kind="waiver", amount_minor=self.charge(student).gross_amount_minor, reason="Free place", actor=self.owner)
        self.pay(120_000 * N)
        adjustments.void_receivable(self.tuition, actor=self.authority, reason="Raised in error")
        self.tuition.refresh_from_db()
        self.assertEqual((self.tuition.status, self.tuition.void_reason, self.tuition.voided_by), ("void", "Raised in error", self.authority))
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.outstanding, pos.credit), (0, 120_000 * N))
        self.assertLedgerHolds()


class VoidTests(AdjustmentTestCase):
    def test_a_void_charge_stays_on_record_and_leaves_what_the_family_owes(self):
        before = ledger.family_position(self.family).outstanding
        adjustments.void_receivable(self.tuition, actor=self.owner, reason="Raised in error")
        self.assertTrue(StudentReceivable.objects.filter(pk=self.tuition.pk).exists())
        self.assertEqual(ledger.family_position(self.family).outstanding, before - 120_000 * N)
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="receivable_voided").exists())
        self.assertLedgerHolds()

    def test_it_needs_authority_and_a_reason_and_happens_once(self):
        with self.assertRaises(Refused):
            adjustments.void_receivable(self.tuition, actor=self.members["accountant"], reason="x")
        with self.assertRaises(Refused):
            adjustments.void_receivable(self.tuition, actor=self.owner, reason="")
        adjustments.void_receivable(self.tuition, actor=self.owner, reason="Error")
        with self.assertRaises(Refused) as raised:
            adjustments.void_receivable(StudentReceivable.objects.get(pk=self.tuition.pk), actor=self.owner, reason="Again")
        self.assertEqual(raised.exception.code, "receivable_void")

    def test_a_void_charge_cannot_be_adjusted(self):
        adjustments.void_receivable(self.tuition, actor=self.owner, reason="Error")
        with self.assertRaises(Refused) as raised:
            self.adjust(receivable=StudentReceivable.objects.get(pk=self.tuition.pk))
        self.assertEqual(raised.exception.code, "receivable_void")

    def test_voiding_a_whole_schedule_leaves_paid_charges_for_a_person_to_decide(self):
        allocation.allocate(self.payment(120_000 * N), self.family, prefer_student=self.ahmad)
        result = adjustments.void_schedule_charges(self.schedule, actor=self.owner, reason="Wrong fees")
        self.assertEqual(result["voided"], 2)
        self.assertEqual([r.id for r in result["skipped_paid"]], [self.tuition.id])
        self.assertEqual(StudentReceivable.objects.exclude(status="void").count(), 1)
        self.assertLedgerHolds()
