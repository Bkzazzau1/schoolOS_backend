from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .. import adjustments, allocation, collection_accounts, credit, ledger, schedules
from ..errors import Refused
from ..models import AccountStatus, CreditKind, FamilyCollectionAccount, FamilyCreditEntry, FinanceAuditEvent
from .base import ReceivablesTestCase, CLOCK

N = 100


class CreditTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.schedule = self.publish_bello_fees()

    def overpay(self, total=400_000 * N):
        tx = self.payment(total)
        allocation.allocate(tx, self.family)
        return tx

    def new_fees(self, amount, days=45, code="NEXT"):
        s = schedules.create_schedule(self.school, session=self.session, name=f"More {code}", actor=self.owner)
        schedules.add_item(s, actor=self.owner, code=code, name=code, amount_minor=amount, due_date=CLOCK + timedelta(days=days), scope="student", student=self.ahmad)
        schedules.publish(s, actor=self.owner)
        return s


class CreditLedgerTests(CreditTestCase):
    def test_the_balance_is_the_sum_of_the_entries_and_nothing_else(self):
        self.overpay()
        entries = FamilyCreditEntry.objects.filter(family=self.family)
        self.assertEqual(ledger.credit_balance(self.family), sum(e.amount_minor for e in entries))
        self.assertEqual(ledger.credit_balance(self.family), 100_000 * N)

    def test_it_is_never_revenue_or_lost_it_stays_a_visible_balance(self):
        tx = self.overpay()
        pos = ledger.family_position(self.family)
        self.assertEqual(pos.credit, 100_000 * N)
        self.assertEqual(pos.collectible, 0)
        self.assertEqual(allocation.unallocated(tx), 0)

    def test_it_is_used_automatically_against_new_charges(self):
        self.overpay()
        self.new_fees(60_000 * N)
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.credit, pos.outstanding), (40_000 * N, 0))
        applied = FamilyCreditEntry.objects.get(kind=CreditKind.APPLIED)
        self.assertEqual((applied.amount_minor, applied.receivable.item_code), (60_000 * N, "NEXT"))
        self.assertLedgerHolds()

    def test_only_what_is_owed_is_used_and_the_rest_stays(self):
        self.overpay()
        self.new_fees(150_000 * N)
        pos = ledger.family_position(self.family)
        self.assertEqual((pos.credit, pos.outstanding), (0, 50_000 * N))

    def test_credit_can_never_go_negative(self):
        self.overpay()
        with self.assertRaises(Refused) as raised:
            credit.add(self.family, CreditKind.REFUNDED, 100_000 * N + 1, actor=self.owner)
        self.assertEqual(raised.exception.code, "insufficient_credit")
        credit.add(self.family, CreditKind.REFUNDED, 100_000 * N, actor=self.owner)
        self.assertEqual(ledger.credit_balance(self.family), 0)
        self.assertLedgerHolds()

    def test_an_entry_is_positive_and_typed(self):
        for amount in (0, -1, 1.5, "5", True):
            with self.assertRaises(Refused, msg=repr(amount)):
                credit.add(self.family, CreditKind.OVERPAYMENT, amount)
        with self.assertRaises(Refused):
            credit.add(self.family, "gift", 100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyCreditEntry.objects.create(school=self.school, family=self.family, kind="overpayment", amount_minor=0)

    def test_entries_are_never_edited_or_deleted(self):
        self.overpay()
        entry = FamilyCreditEntry.objects.get()
        entry.amount_minor = 1
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.delete()

    def test_asking_twice_with_the_same_reference_is_one_entry(self):
        a = credit.add(self.family, CreditKind.OVERPAYMENT, 500, ref="once:1")
        b = credit.add(self.family, CreditKind.OVERPAYMENT, 500, ref="once:1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(FamilyCreditEntry.objects.count(), 1)

    def test_an_entry_can_only_touch_the_families_own_charges_at_its_own_school(self):
        other = self.make_student("Yusuf", "Sani", guardian="Ada Sani")
        from .. import families

        other_family, _ = families.ensure_family_for_student(self.school, other)
        with self.assertRaises(ValidationError):
            FamilyCreditEntry(school=self.school, family=other_family, kind="applied", amount_minor=5, receivable=self.charge(self.ahmad)).save()
        with self.assertRaises(ValidationError):
            FamilyCreditEntry(school=self.other_school, family=self.family, kind="overpayment", amount_minor=5).save()


class RefundTests(CreditTestCase):
    def test_the_finance_office_records_a_refund_with_a_reason(self):
        self.overpay()
        entry = credit.refund(self.family, 30_000 * N, actor=self.members["accountant"], reason="Paid back by transfer")
        self.assertEqual((entry.kind, entry.amount_minor, entry.reason, entry.actor), ("refunded", 30_000 * N, "Paid back by transfer", self.members["accountant"]))
        self.assertEqual(ledger.credit_balance(self.family), 70_000 * N)
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="credit_refunded").exists())
        self.assertLedgerHolds()

    def test_only_those_who_work_the_ledger_and_only_with_a_reason(self):
        self.overpay()
        for role in ("teacher", "parent", "student", "principal"):
            with self.assertRaises(Refused, msg=role):
                credit.refund(self.family, 100, actor=self.members[role], reason="x")
        with self.assertRaises(Refused):
            credit.refund(self.family, 100, actor=self.other_owner, reason="x")
        with self.assertRaises(Refused) as raised:
            credit.refund(self.family, 100, actor=self.owner, reason=" ")
        self.assertEqual(raised.exception.code, "reason_required")

    def test_a_family_that_owes_has_no_credit_to_refund_because_it_is_already_being_used(self):
        self.overpay(150_000 * N)  # half of what is owed: the family still owes, so it holds no credit
        with self.assertRaises(Refused) as raised:
            credit.refund(self.family, 1, actor=self.owner, reason="x")
        self.assertEqual(raised.exception.code, "insufficient_credit")


class TakingBackAppliedCreditTests(CreditTestCase):
    def test_uses_of_credit_are_taken_back_newest_first_until_enough_is_free(self):
        self.overpay()
        self.new_fees(30_000 * N, code="A")
        self.new_fees(30_000 * N, code="B")
        self.assertEqual(ledger.family_position(self.family).credit, 40_000 * N)
        credit.ensure_available(self.family, 70_000 * N, actor=self.owner, reason="test")
        # Only the newest use (B) had to be undone: 40,000 + 30,000 is enough.
        self.assertEqual(ledger.credit_balance(self.family), 70_000 * N)
        undone = FamilyCreditEntry.objects.filter(kind=CreditKind.APPLICATION_REVERSED)
        self.assertEqual([e.receivable.item_code for e in undone], ["B"])

    def test_if_there_is_nothing_left_to_take_back_it_says_so(self):
        with self.assertRaises(Refused) as raised:
            credit.ensure_available(self.family, 1, actor=self.owner)
        self.assertEqual(raised.exception.code, "credit_in_use")

    def test_an_application_is_reversed_once(self):
        self.overpay()
        self.new_fees(10_000 * N)
        applied = FamilyCreditEntry.objects.get(kind=CreditKind.APPLIED)
        credit.reverse_application(applied, actor=self.owner, reason="x")
        with self.assertRaises(Refused) as raised:
            credit.reverse_application(applied, actor=self.owner, reason="again")
        self.assertEqual(raised.exception.code, "already_reversed")
        with self.assertRaises(Refused):
            credit.reverse_application(FamilyCreditEntry.objects.get(kind=CreditKind.OVERPAYMENT), actor=self.owner)


class CollectionAccountLifecycleTests(CreditTestCase):
    def register(self, **over):
        fields = dict(provider="monnify", account_number="8012345678", account_name="BRIGHTGATE / BELLO", actor=self.owner)
        fields.update(over)
        return collection_accounts.register(self.family, **fields)

    def test_an_account_is_active_while_the_family_owes_and_dormant_when_it_does_not(self):
        account = self.register()
        self.assertEqual(account.status, AccountStatus.ACTIVE)
        self.assertIsNotNone(account.activated_at)
        allocation.allocate(self.payment(300_000 * N), self.family)
        account.refresh_from_db()
        self.assertEqual((account.status, account.account_number), (AccountStatus.DORMANT, "8012345678"))
        self.assertIsNotNone(account.dormant_at)

    def test_it_is_dormant_from_the_start_if_the_family_owes_nothing(self):
        allocation.allocate(self.payment(300_000 * N), self.family)
        self.assertEqual(self.register().status, AccountStatus.DORMANT)

    def test_new_debt_wakes_the_same_account_up_it_is_never_replaced(self):
        account = self.register()
        allocation.allocate(self.payment(300_000 * N), self.family)
        self.new_fees(10_000 * N)
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.ACTIVE)
        self.assertEqual(FamilyCollectionAccount.objects.count(), 1)
        self.assertEqual(FamilyCollectionAccount.objects.get().account_number, "8012345678")

    def test_a_reversed_payment_makes_the_family_owe_again_so_the_account_wakes(self):
        account = self.register()
        tx = self.payment(300_000 * N)
        allocation.allocate(tx, self.family)
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.DORMANT)
        allocation.release_transaction(tx, actor=self.owner, reason="Reversed")
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.ACTIVE)

    def test_waiving_everything_makes_it_dormant_and_undoing_it_wakes_it(self):
        account = self.register()
        made = [adjustments.adjust(self.charge(s), kind="waiver", amount_minor=self.charge(s).gross_amount_minor, reason="Free places", actor=self.owner) for s in (self.ahmad, self.aisha, self.maryam)]
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.DORMANT)
        adjustments.reverse(made[0], actor=self.owner, reason="Withdrawn")
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.ACTIVE)

    def test_an_account_still_being_set_up_waits_until_it_is_provisioned(self):
        account = self.register(provisioned=False)
        self.assertEqual(account.status, AccountStatus.PROVISIONING)
        allocation.allocate(self.payment(300_000 * N), self.family)
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.PROVISIONING)  # settling does not touch it
        self.assertEqual(collection_accounts.mark_provisioned(account, actor=self.owner).status, AccountStatus.DORMANT)
        with self.assertRaises(Refused):
            collection_accounts.mark_provisioned(account, actor=self.owner)

    def test_a_suspended_account_stays_suspended_whatever_the_family_owes_until_a_person_reinstates_it(self):
        account = self.register()
        collection_accounts.suspend(account, actor=self.owner, reason="Provider flagged fraud")
        allocation.allocate(self.payment(300_000 * N), self.family)
        self.new_fees(10_000 * N)
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.SUSPENDED)
        self.assertEqual(collection_accounts.reinstate(account, actor=self.owner).status, AccountStatus.ACTIVE)

    def test_a_closed_account_is_gone_for_good_but_its_family_can_have_a_new_one(self):
        account = self.register()
        collection_accounts.close(account, actor=self.owner, reason="Moved provider")
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.CLOSED)
        self.new_fees(10_000 * N)
        account.refresh_from_db()
        self.assertEqual(account.status, AccountStatus.CLOSED)  # never revived
        again = self.register(account_number="9099999999")
        self.assertEqual(again.status, AccountStatus.ACTIVE)

    def test_an_account_is_never_deleted(self):
        account = self.register()
        with self.assertRaises(ValidationError):
            account.delete()

    def test_every_change_of_state_is_audited(self):
        account = self.register()
        allocation.allocate(self.payment(300_000 * N), self.family)
        events = FinanceAuditEvent.objects.filter(kind="collection_account_status_changed", object_id=str(account.id)).order_by("at", "id")
        self.assertEqual([(e.detail["before"], e.detail["after"]) for e in events], [("provisioning", "active"), ("active", "dormant")])


class RegisteringAccountsTests(CreditTestCase):
    def register(self, **over):
        fields = dict(provider="monnify", account_number="8012345678", actor=self.owner)
        fields.update(over)
        return collection_accounts.register(self.family, **fields)

    def test_only_those_who_work_the_ledger_may(self):
        for role in ("teacher", "parent", "student", "principal"):
            with self.assertRaises(Refused, msg=role):
                self.register(actor=self.members[role])
        self.assertEqual(self.register(actor=self.members["accountant"]).status, AccountStatus.ACTIVE)

    def test_a_provider_and_an_identifier_are_required(self):
        with self.assertRaises(Refused) as raised:
            self.register(provider="")
        self.assertEqual(raised.exception.code, "provider_required")
        with self.assertRaises(Refused) as raised:
            self.register(account_number="", external_account_ref="")
        self.assertEqual(raised.exception.code, "identifier_required")

    def test_a_family_has_one_live_account_per_provider_and_a_number_is_used_once(self):
        self.register()
        with self.assertRaises(Refused) as raised:
            self.register(account_number="1111111111")
        self.assertEqual(raised.exception.code, "account_exists")
        from .. import families

        other, _ = families.ensure_family_for_student(self.school, self.make_student("Yusuf", "Sani", guardian="Ada"))
        with self.assertRaises(Refused) as raised:
            collection_accounts.register(other, provider="monnify", account_number="8012345678", actor=self.owner)
        self.assertEqual(raised.exception.code, "identifier_in_use")
        collection_accounts.register(other, provider="paystack", account_number="8012345678", actor=self.owner)  # another provider: fine

    def test_a_connection_from_another_school_is_refused(self):
        with self.assertRaises(Refused) as raised:
            self.register(connection=self.bank_connection(self.other_school))
        self.assertEqual(raised.exception.code, "connection_not_found")
        self.assertEqual(self.register(connection=self.bank_connection()).connection, self.bank_connection())

    def test_a_payment_is_traced_to_its_family_by_the_account_it_was_paid_into(self):
        account = self.register(external_account_ref="prov-123")
        find = collection_accounts.find_by_receiving_reference
        self.assertEqual(find(self.school, "monnify", "8012345678"), account)
        self.assertEqual(find(self.school, "monnify", "prov-123"), account)
        self.assertIsNone(find(self.school, "paystack", "8012345678"))  # another provider's number
        self.assertIsNone(find(self.other_school, "monnify", "8012345678"))  # another school
        self.assertIsNone(find(self.school, "monnify", ""))
        self.assertIsNone(find(self.school, "monnify", "0000000000"))
        collection_accounts.close(account, actor=self.owner, reason="Retired")
        self.assertIsNone(find(self.school, "monnify", "8012345678"))  # a closed account identifies no one

    def test_the_database_holds_the_line_on_a_shared_number(self):
        self.register()
        from .. import families

        other, _ = families.ensure_family_for_student(self.school, self.make_student("Yusuf", "Sani", guardian="Ada"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyCollectionAccount.objects.create(school=self.school, family=other, provider="monnify", account_number="8012345678")
