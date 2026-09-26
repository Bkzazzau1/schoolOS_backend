from datetime import timedelta

from .. import adjustments, allocation, collection_accounts, ledger
from ..models import FamilyCollectionAccount
from .base import ReceivablesTestCase, CLOCK

NAIRA = 100


class TheWholeFlowTests(ReceivablesTestCase):
    """The story from the brief, start to finish: the owner delegates, fees are published, discounts are
    decided, the family pays, overpays, and its collection account follows what it owes."""

    def test_from_authority_to_dormant_and_back_again(self):
        self.give_duty(self.members["principal"])
        authority = self.members["principal"]
        family = self.bello_family()
        account = collection_accounts.register(family, provider="monnify", account_number="8012345678", actor=self.owner)
        self.assertEqual(account.status, "dormant")  # nothing is owed yet

        self.publish_bello_fees()
        account.refresh_from_db()
        self.assertEqual(account.status, "active")  # published debt: the account wakes up
        self.assertEqual(ledger.family_position(family).outstanding, 300_000 * NAIRA)

        # The delegate (not the owner, not the Finance Office) decides a scholarship and a discount.
        adjustments.adjust(self.charge(self.ahmad), kind="scholarship", amount_minor=20_000 * NAIRA, reason="Founder scholarship", actor=authority)
        adjustments.adjust(self.charge(self.aisha), kind="discount", amount_minor=10_000 * NAIRA, reason="Sibling discount", actor=authority)
        position = ledger.family_position(family)
        self.assertEqual((position.gross, position.adjustments, position.net, position.outstanding), (300_000 * NAIRA, 30_000 * NAIRA, 270_000 * NAIRA, 270_000 * NAIRA))
        self.assertEqual(self.charge(self.ahmad).gross_amount_minor, 120_000 * NAIRA)  # the charge itself never changed

        # The family pays 300,000 into its account: 270,000 settles everything, 30,000 is credit.
        tx = self.payment(300_000 * NAIRA, receiving_account="8012345678")
        result = allocation.allocate(tx, family, source="auto")
        self.assertEqual((result.allocated_minor, result.credit_minor), (270_000 * NAIRA, 30_000 * NAIRA))
        position = ledger.family_position(family)
        self.assertEqual((position.outstanding, position.credit), (0, 30_000 * NAIRA))
        for student in (self.ahmad, self.aisha, self.maryam):
            self.assertEqual(self.charge(student).status, "settled")
        account.refresh_from_db()
        self.assertEqual(account.status, "dormant")  # nothing owed: dormant, not deleted
        self.assertEqual(account.account_number, "8012345678")
        self.assertLedgerHolds()

        # Next term's fees are published: the credit is used, the rest is owed, and the SAME account is active again.
        from .. import schedules

        due = CLOCK + timedelta(days=60)
        second = schedules.create_schedule(self.school, session=self.session, name="Second Term", actor=self.owner)
        schedules.add_item(second, actor=self.owner, code="TUI2", name="Tuition", category="tuition", amount_minor=50_000 * NAIRA, due_date=due, scope="student", student=self.ahmad)
        schedules.publish(second, actor=self.owner)
        position = ledger.family_position(family)
        self.assertEqual((position.credit, position.outstanding), (0, 20_000 * NAIRA))
        account.refresh_from_db()
        self.assertEqual((account.status, account.account_number), ("active", "8012345678"))
        self.assertEqual(FamilyCollectionAccount.objects.count(), 1)
        self.assertLedgerHolds()
