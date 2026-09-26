"""What happens to a family's account when the family has paid everything it owes, as the school's policy says - and retiring an account at the
provider. Nothing is ever deleted, and SchoolOS invents no rule about money arriving at an account in any state."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bankconnect.models import SandboxProviderAccount
from apps.bankconnect.providers import base
from apps.receivables import credit, ledger
from apps.receivables.models import FamilyCollectionAccount, FamilyCreditEntry

from .. import batches, jobs, lifecycle, policy
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent, ProviderJob
from .base import CollectTestCase


class LifecycleCase(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Alpha", owes=100_000)

    def generate(self):
        """The school's provider makes the family's account through an approved batch."""
        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.family)
        return account

    def settle(self, account):
        self.pay(self.family, 100_000, account=account)
        account.refresh_from_db()
        return account


class SettlementActionTests(LifecycleCase):
    def test_by_default_a_paid_family_account_becomes_dormant_and_wakes_up_when_the_family_owes_again(self):
        account = self.settle(self.generate())
        self.assertEqual((account.status, account.dormant_at is not None), ("dormant", True))
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 20_000, term=self.term1)
        account.refresh_from_db()
        self.assertEqual(account.status, "active")

    def test_close_immediately_starts_closing_and_the_provider_is_asked_from_the_queue_not_the_payment(self):
        self.set_policy(settlement_action="close_immediately")
        account = self.generate()
        self.settle(account)
        self.assertEqual(account.status, "closing")
        self.assertEqual(SandboxProviderAccount.objects.get(reference=account.external_account_ref).status, "active")  # not yet
        self.assertEqual(ProviderJob.objects.filter(kind="retire", status="queued").count(), 1)
        jobs.drain()
        account.refresh_from_db()
        self.assertEqual((account.status, account.closed_at is not None), ("closed", True))
        self.assertEqual(SandboxProviderAccount.objects.get(reference=account.external_account_ref).status, "closed")

    def test_grace_then_dormant_waits_the_schools_own_period_and_then_goes_dormant(self):
        self.set_policy(settlement_action="grace_then_dormant", grace_period_hours=48)
        account = self.settle(self.generate())
        self.assertEqual((account.status, account.after_grace), ("grace", "dormant"))
        self.assertAlmostEqual((account.grace_until - timezone.now()).total_seconds(), 48 * 3600, delta=120)
        self.assertEqual(lifecycle.process_grace(timezone.now() + timedelta(hours=47)), 0)
        account.refresh_from_db()
        self.assertEqual(account.status, "grace")
        self.assertEqual(lifecycle.process_grace(timezone.now() + timedelta(hours=49)), 1)
        account.refresh_from_db()
        self.assertEqual((account.status, account.grace_until, account.after_grace), ("dormant", None, ""))

    def test_grace_then_close_ends_in_a_close_through_the_queue(self):
        self.set_policy(settlement_action="grace_then_close", grace_period_hours=24)
        account = self.settle(self.generate())
        self.assertEqual(account.status, "grace")
        lifecycle.process_grace(timezone.now() + timedelta(hours=25))
        account.refresh_from_db()
        self.assertEqual(account.status, "closing")
        jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")

    def test_owing_again_during_the_grace_period_makes_the_account_active_again_and_the_wait_is_forgotten(self):
        self.set_policy(settlement_action="grace_then_close", grace_period_hours=24)
        account = self.settle(self.generate())
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 20_000, term=self.term1)
        account.refresh_from_db()
        self.assertEqual((account.status, account.grace_until), ("active", None))
        self.assertEqual(lifecycle.process_grace(timezone.now() + timedelta(days=30)), 0)

    def test_a_family_that_owes_again_when_the_grace_period_ends_is_not_closed_on(self):
        self.set_policy(settlement_action="grace_then_close", grace_period_hours=24)
        account = self.settle(self.generate())
        FamilyCollectionAccount.objects.filter(pk=account.pk).update(status="grace")  # stays in grace though a charge lands unnoticed
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 20_000, term=self.term1)
        FamilyCollectionAccount.objects.filter(pk=account.pk).update(status="grace", grace_until=timezone.now() - timedelta(hours=1), after_grace="close")
        lifecycle.process_grace()
        account.refresh_from_db()
        self.assertEqual(account.status, "active")

    def test_manual_leaves_it_settled_for_a_person_and_asks_the_provider_for_nothing(self):
        self.set_policy(settlement_action="manual")
        account = self.settle(self.generate())
        self.assertEqual(account.status, "settled")
        self.assertEqual(ProviderJob.objects.filter(kind="retire").count(), 0)

    def test_provider_native_leaves_it_settled_and_asks_the_provider_for_nothing(self):
        self.set_policy(settlement_action="provider_native")
        account = self.settle(self.generate())
        self.assertEqual((account.status, ProviderJob.objects.filter(kind="retire").count()), ("settled", 0))
        self.assertEqual(SandboxProviderAccount.objects.get(reference=account.external_account_ref).status, "active")

    def test_a_settled_account_wakes_up_when_the_family_owes_again(self):
        self.set_policy(settlement_action="manual")
        account = self.settle(self.generate())
        self.charge_family(self.family, [m.student for m in self.family.members.all()], 20_000, term=self.term1)
        account.refresh_from_db()
        self.assertEqual(account.status, "active")

    def test_a_wait_with_no_waiting_period_leaves_the_account_for_a_person_instead_of_guessing(self):
        from ..models import SchoolCollectionPolicy

        account = self.generate()
        SchoolCollectionPolicy.objects.filter(school=self.school).update(default_settlement_action="grace_then_close", default_grace_period_hours=None)
        self.settle(account)
        self.assertEqual(account.status, "settled")
        self.assertTrue(CollectionAuditEvent.objects.filter(kind="policy_incomplete_at_settlement").exists())

    def test_a_family_can_have_its_own_settlement_behaviour(self):
        policy.set_override(self.owner, scope="family", family=self.family, values={"settlement_action": "manual"}, reason="Keeps paying in parts")
        self.assertEqual(self.settle(self.generate()).status, "settled")

    def test_a_batch_can_have_its_own_settlement_behaviour_for_the_accounts_it_makes(self):
        batch = self.new_batch()
        batches.set_batch_policy(self.maker, batch.id, values={"settlement_action": "manual"})
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        self.run_batch(self.refetch(batch))
        (account,) = self.live_accounts(self.family)
        self.assertEqual(self.settle(account).status, "settled")

    def test_nothing_is_ever_deleted(self):
        account = self.settle(self.generate())
        with self.assertRaises(ValidationError):
            account.delete()
        self.assertEqual(FamilyCollectionAccount.objects.count(), 1)


class ParentViewTests(LifecycleCase):
    def facts(self):
        from apps.receivables import statements

        (row,) = statements.collection_account_facts(self.family)
        return row

    def test_a_settled_dormant_or_waiting_account_is_still_offered_to_the_parent_with_its_number(self):
        for action, hours in (("dormant_immediately", None), ("manual", None), ("grace_then_close", 24)):
            values = {"settlement_action": action, **({"grace_period_hours": hours} if hours else {})}
            self.set_policy(**values)
            family = self.make_family(f"Family {action}", owes=10_000)
            self.family = family
            account = self.settle_family_account(family)
            row = self.facts()
            self.assertTrue(row["canPay"] and row["accountNumber"] == account.account_number, (action, row["status"]))

    def settle_family_account(self, family):
        batch = self.new_batch()
        from .. import batches as b

        for item in self.items(batch).values():
            if item.family_id != family.id:
                b.set_selection(self.maker, batch.id, deselect=[str(item.id)])
        batch = self.refetch(batch)
        b.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        b.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        self.run_batch(self.refetch(batch))
        (account,) = self.live_accounts(family)
        self.pay(family, 10_000, account=account)
        account.refresh_from_db()
        return account

    def test_an_account_being_closed_is_not_offered(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Left")
        row = self.facts()
        self.assertEqual((row["status"], row["canPay"], row["accountNumber"]), ("closing", False, ""))


class MoneyAfterSettlementTests(LifecycleCase):
    """A payment the provider confirmed goes through the normal pipeline whatever state the account is in."""

    def test_a_payment_into_a_dormant_account_is_reconciled_and_held_as_credit_with_no_forced_review(self):
        account = self.settle(self.generate())
        self.assertEqual(account.status, "dormant")
        self.pay(self.family, 5_000, account=account)
        from apps.bankconnect.models import BankTransaction

        tx = BankTransaction.objects.latest("created_at")
        self.assertEqual((tx.reconciliation_status, tx.family), ("matched", self.family))
        self.assertEqual(credit.total_credit_from(tx), 5_000 * 100)
        self.assertEqual(ledger.verify_family(self.family), [])

    def test_a_payment_into_a_closed_account_still_identifies_its_family_and_is_credited(self):
        self.set_policy(settlement_action="close_immediately")
        account = self.generate()
        self.settle(account)
        jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")
        self.pay(self.family, 3_000, account=account)
        self.assertEqual(FamilyCreditEntry.objects.filter(family=self.family).count(), 1)

    def test_a_payment_into_an_account_still_owed_on_pays_the_charges_and_never_settles_twice(self):
        account = self.generate()
        self.pay(self.family, 40_000, account=account)
        self.assertEqual(ledger.family_position(self.family).outstanding, 60_000 * 100)
        account.refresh_from_db()
        self.assertEqual(account.status, "active")


class RetiringTests(LifecycleCase):
    def test_a_person_retires_an_account_and_the_provider_is_asked_from_the_queue(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Family left the school")
        account.refresh_from_db()
        self.assertEqual((account.status, account.close_reason), ("closing", "Family left the school"))
        jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")
        self.assertEqual(SandboxProviderAccount.objects.get(reference=account.external_account_ref).status, "closed")

    def test_a_reason_and_authority_are_needed_and_only_this_schools_accounts_are_found(self):
        account = self.generate()
        with self.assertRaises(CollectionRefused) as caught:
            lifecycle.retire(self.owner, account.id, reason="")
        self.assertEqual(caught.exception.code, "reason_required")
        with self.assertRaises(Exception):
            lifecycle.retire(self.members["teacher"], account.id, reason="Because")
        with self.assertRaises(CollectionRefused) as caught:
            lifecycle.retire(self.other_owner, account.id, reason="Because")
        self.assertEqual(caught.exception.code, "account_not_found")

    def test_retiring_twice_is_refused(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="One")
        with self.assertRaises(CollectionRefused) as caught:
            lifecycle.retire(self.owner, account.id, reason="Two")
        self.assertEqual(caught.exception.code, "already_closed")

    def test_the_closing_endpoint_in_receivables_goes_through_the_provider_too(self):
        from apps.receivables import collection_accounts

        account = self.generate()
        collection_accounts.close(account, actor=self.owner, reason="Left")
        account.refresh_from_db()
        self.assertEqual(account.status, "closing")
        jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")

    def test_an_account_recorded_by_hand_has_no_provider_to_ask_and_is_closed_at_once(self):
        from apps.receivables import collection_accounts

        other = self.make_family("Other")
        legacy = collection_accounts.register(other, provider="gtbank", account_number="0123456789", actor=self.owner)
        lifecycle.retire(self.owner, legacy.id, reason="Old arrangement")
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, "closed")

    def test_a_provider_that_will_not_cancel_still_leaves_our_account_closed_with_that_said(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Left")
        connector = type(__import__("apps.bankconnect.providers.registry", fromlist=["x"]).get_connector("sandbox"))
        with mock.patch.object(connector, "close_collection_account", side_effect=base.ProviderRejected("provider_rejected", "no")):
            jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")
        self.assertIn("did not confirm the cancellation", account.close_reason)

    def test_a_provider_that_does_not_answer_is_asked_again_later_and_the_account_stays_closing_meanwhile(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Left")
        connector = type(__import__("apps.bankconnect.providers.registry", fromlist=["x"]).get_connector("sandbox"))
        with mock.patch.object(connector, "close_collection_account", side_effect=base.ProviderUnavailable()):
            jobs.drain()
        account.refresh_from_db()
        self.assertEqual(account.status, "closing")
        job = ProviderJob.objects.get(kind="retire")
        self.assertEqual(job.status, "retry")
        jobs.run_next(timezone.now() + timedelta(hours=1))
        account.refresh_from_db()
        self.assertEqual(account.status, "closed")

    def test_bad_credentials_while_retiring_are_reported_and_the_account_stays_closing_for_a_person(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Left")
        connector = type(__import__("apps.bankconnect.providers.registry", fromlist=["x"]).get_connector("sandbox"))
        with mock.patch.object(connector, "close_collection_account", side_effect=base.BadCredentials()):
            jobs.drain()
        account.refresh_from_db()
        self.assertEqual((account.status, self.connection_row().status), ("closing", "needs_reauth"))
        self.assertEqual(ProviderJob.objects.get(kind="retire").status, "failed")

    def test_an_account_that_is_closing_still_counts_as_the_familys_one_live_account(self):
        account = self.generate()
        lifecycle.retire(self.owner, account.id, reason="Left")
        self.assertEqual(len(self.live_accounts(self.family)), 1)
        batch = self.new_batch()
        self.assertEqual(self.item(batch, self.family).eligibility_status, "provider_conflict")
