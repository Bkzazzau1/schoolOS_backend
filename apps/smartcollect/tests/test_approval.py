"""Maker and checker: the person who prepared a batch never approves it, an approval is for exactly the snapshot the checker saw, a rejection
needs a reason and returns to the maker with its history, and anything that changes what would be generated withdraws the approval."""

from django.db import IntegrityError, transaction

from apps.receivables import ledger

from .. import batches, policy, running
from ..constants import BatchStatus
from ..errors import CollectionRefused
from ..models import CollectionBatchEvent, CollectionGenerationBatch
from .base import APPROVE, PREPARE, CollectTestCase


class MakerCheckerTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_family("Alpha")
        self.bravo = self.make_family("Bravo", owes=50_000)
        self.batch = self.new_batch()

    def submit(self, batch=None):
        batch = batch or self.refetch(self.batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        return self.refetch(batch)

    def test_a_submitted_batch_waits_for_approval_and_can_no_longer_be_edited(self):
        batch = self.submit()
        self.assertEqual((batch.status, batch.submitted_by), (BatchStatus.PENDING_APPROVAL, self.maker))
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, batch.id, deselect_all=True)
        self.assertEqual(caught.exception.code, "not_editable")

    def test_a_different_checker_approves_exactly_what_was_submitted(self):
        batch = self.submit()
        batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        batch = self.refetch(batch)
        self.assertEqual((batch.status, batch.approved_by, batch.approved_snapshot_hash, batch.approved_version), (BatchStatus.APPROVED, self.checker, batch.snapshot_hash, batch.version))

    def test_the_person_who_prepared_or_submitted_it_cannot_approve_it_even_as_the_owner(self):
        batch = self.submit()
        self.give_duties(self.maker, PREPARE, APPROVE)  # holds both duties, and is still the maker
        for who in (self.maker,):
            with self.assertRaises(CollectionRefused) as caught:
                batches.approve(who, batch.id, expected_hash=batch.snapshot_hash)
            self.assertEqual(caught.exception.code, "maker_cannot_approve")
        # the owner holds every duty; an owner who prepared the batch cannot approve it either
        own = self.new_batch(who=self.owner)
        batches.submit(self.owner, own.id, expected_hash=own.snapshot_hash)
        with self.assertRaises(CollectionRefused) as caught:
            batches.approve(self.owner, own.id, expected_hash=self.refetch(own).snapshot_hash)
        self.assertEqual(caught.exception.code, "maker_cannot_approve")

    def test_anyone_who_changed_the_batch_along_the_way_cannot_approve_it_either(self):
        item = self.item(self.batch, self.bravo)
        batches.set_selection(self.owner, self.batch.id, deselect=[str(item.id)])  # the owner edited it
        batch = self.refetch(self.batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        with self.assertRaises(CollectionRefused) as caught:
            batches.approve(self.owner, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        self.assertEqual(caught.exception.code, "maker_cannot_approve")
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)

    def test_only_someone_with_the_approve_duty_can_approve_or_reject(self):
        batch = self.submit()
        for who in (self.members["teacher"], self.members["parent"], self.members["accountant"]):
            with self.assertRaises(CollectionRefused) as caught:
                batches.approve(who, batch.id, expected_hash=batch.snapshot_hash)
            self.assertIn(caught.exception.code, ("not_approver", "maker_cannot_approve"))
            with self.assertRaises(CollectionRefused):
                batches.reject(who, batch.id, reason="Not right at all")

    def test_the_database_itself_refuses_a_checker_who_is_the_maker(self):
        batch = self.submit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionGenerationBatch.objects.filter(pk=batch.pk).update(
                status="approved", approved_by=self.maker, approved_snapshot_hash="x" * 64, approved_version=1
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionGenerationBatch.objects.filter(pk=batch.pk).update(
                status="approved", approved_by=batch.submitted_by, approved_snapshot_hash="x" * 64, approved_version=1
            )

    def test_an_approval_without_a_snapshot_or_a_rejection_without_a_reason_cannot_be_stored(self):
        batch = self.submit()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionGenerationBatch.objects.filter(pk=batch.pk).update(approved_by=self.checker)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionGenerationBatch.objects.filter(pk=batch.pk).update(rejected_by=self.checker, rejection_reason="")

    def test_a_draft_that_is_not_submitted_cannot_be_approved(self):
        with self.assertRaises(CollectionRefused) as caught:
            batches.approve(self.checker, self.batch.id, expected_hash=self.batch.snapshot_hash)
        self.assertEqual(caught.exception.code, "not_pending")

    def test_approving_a_snapshot_other_than_the_one_on_the_batch_is_refused(self):
        batch = self.submit()
        for wrong in ("", "0" * 64):
            with self.assertRaises(CollectionRefused) as caught:
                batches.approve(self.checker, batch.id, expected_hash=wrong)
            self.assertEqual(caught.exception.code, "stale_approval")
        self.assertEqual(self.refetch(batch).status, BatchStatus.PENDING_APPROVAL)


class RejectionTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.make_family("Alpha")
        self.make_family("Bravo", owes=50_000)
        self.batch = self.new_batch()
        batches.submit(self.maker, self.batch.id, expected_hash=self.batch.snapshot_hash)

    def test_a_rejection_needs_a_reason_and_keeps_it(self):
        for reason in ("", "no", "   "):
            with self.assertRaises(CollectionRefused) as caught:
                batches.reject(self.checker, self.batch.id, reason=reason)
            self.assertEqual(caught.exception.code, "reason_required")
        batches.reject(self.checker, self.batch.id, reason="Bravo family should not be included")
        batch = self.refetch(self.batch)
        self.assertEqual((batch.status, batch.rejected_by, batch.rejection_reason), (BatchStatus.REJECTED, self.checker, "Bravo family should not be included"))

    def test_the_maker_sees_it_returned_edits_it_and_sends_it_again(self):
        batches.reject(self.checker, self.batch.id, reason="Bravo family should not be included")
        item = self.item(self.batch, self.item(self.batch, self.alpha).family)
        batches.set_selection(self.maker, self.batch.id, deselect=[str(item.id)])
        self.assertEqual(self.refetch(self.batch).status, BatchStatus.DRAFT)
        batch = self.refetch(self.batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batch = self.refetch(batch)
        self.assertEqual((batch.status, batch.rejection_reason), (BatchStatus.PENDING_APPROVAL, ""))
        batches.approve(self.other_checker, batch.id, expected_hash=batch.snapshot_hash)  # a different checker this time is fine

    def test_the_whole_history_stays(self):
        batches.reject(self.checker, self.batch.id, reason="Bravo family should not be included")
        batch = self.refetch(self.batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        kinds = list(CollectionBatchEvent.objects.filter(batch=batch).values_list("kind", flat=True))
        self.assertEqual(kinds, ["created", "preview_refreshed", "submitted", "rejected", "revised", "submitted"])
        rejected = CollectionBatchEvent.objects.get(batch=batch, kind="rejected")
        self.assertEqual((rejected.actor, rejected.detail["reason"]), (self.checker, "Bravo family should not be included"))

    def test_the_maker_cannot_reject_their_own_batch(self):
        with self.assertRaises(CollectionRefused) as caught:
            batches.reject(self.maker, self.batch.id, reason="Changed my mind about this")
        self.assertIn(caught.exception.code, ("not_approver", "maker_cannot_approve"))

    def test_a_rejected_batch_can_be_cancelled(self):
        batches.reject(self.checker, self.batch.id, reason="Not this term at all")
        batches.cancel(self.maker, self.batch.id, reason="Started again")
        self.assertEqual(self.refetch(self.batch).status, BatchStatus.CANCELLED)

    def test_a_batch_cannot_be_rejected_twice_or_after_approval(self):
        batches.reject(self.checker, self.batch.id, reason="Not this term at all")
        with self.assertRaises(CollectionRefused):
            batches.reject(self.checker, self.batch.id, reason="Still not this term")


class StalenessTests(CollectTestCase):
    """What was approved is a fingerprint. If anything that matters changes, the approval no longer covers what would run."""

    def setUp(self):
        super().setUp()
        self.alpha = self.make_family("Alpha")
        self.bravo = self.make_family("Bravo", owes=50_000)
        self.batch = self.new_batch()
        batches.submit(self.maker, self.batch.id, expected_hash=self.batch.snapshot_hash)
        self.batch = self.refetch(self.batch)

    def approve(self):
        return batches.approve(self.checker, self.batch.id, expected_hash=self.batch.snapshot_hash)

    def assert_withdrawn(self, code="batch_changed"):
        with self.assertRaises(CollectionRefused) as caught:
            self.approve()
        self.assertEqual(caught.exception.code, code)
        batch = self.refetch(self.batch)
        self.assertEqual((batch.status, batch.approved_by, batch.approved_snapshot_hash, batch.submitted_by), (BatchStatus.DRAFT, None, "", None))
        self.assertGreater(batch.version, self.batch.version)
        self.assertTrue(CollectionBatchEvent.objects.filter(batch=batch, kind="approval_invalidated").exists())

    def test_a_payment_that_changes_what_a_family_owes_withdraws_the_approval_and_returns_the_batch_to_its_maker(self):
        self.pay_alpha()
        self.assert_withdrawn()

    def pay_alpha(self):
        from apps.receivables import collection_accounts
        from apps.bankconnect.providers.base import ProvisionedAccount

        # a payment into an account made elsewhere: the ledger changes without anything touching the batch
        account = collection_accounts.create_from_provider(
            self.alpha, self.connection, ProvisionedAccount(account_number="9000000777", provider_account_ref="R7", lookup_ref="9000000777"),
            actor=self.owner, idempotency_key="seed-alpha",
        )
        self.pay(self.alpha, 40_000, account=account)

    def test_a_new_charge_withdraws_it(self):
        self.charge_family(self.alpha, [m.student for m in self.alpha.members.all()], 10_000, term=self.term1)
        self.assert_withdrawn()

    def test_a_change_of_policy_withdraws_it(self):
        self.set_policy(account_mode="dynamic")
        self.assert_withdrawn()

    def test_a_family_override_withdraws_it(self):
        policy.set_override(self.owner, scope="family", family=self.alpha, values={"settlement_action": "manual"}, reason="Agreed")
        self.assert_withdrawn()

    def test_a_family_getting_an_account_elsewhere_withdraws_it(self):
        from apps.bankconnect.providers.base import ProvisionedAccount
        from apps.receivables import collection_accounts

        collection_accounts.create_from_provider(
            self.bravo, self.connection, ProvisionedAccount(account_number="9000000888", provider_account_ref="R8", lookup_ref="9000000888"),
            actor=self.owner, idempotency_key="seed-bravo",
        )
        self.assert_withdrawn()

    def test_the_payer_details_going_missing_withdraws_it(self):
        from apps.students.models import GuardianLink

        GuardianLink.objects.filter(student__family_memberships__family=self.alpha).update(email="")
        self.assert_withdrawn()

    def test_something_that_does_not_matter_does_not_withdraw_it(self):
        self.alpha.display_name  # nothing about the numbers changes
        self.approve()
        self.assertEqual(self.refetch(self.batch).status, BatchStatus.APPROVED)

    def test_a_change_after_approval_but_before_the_batch_is_started_withdraws_the_approval_and_nothing_is_generated(self):
        self.approve()
        self.set_policy(account_mode="dynamic")
        with self.assertRaises(CollectionRefused) as caught:
            running.start_processing(self.maker, self.batch.id)
        self.assertEqual(caught.exception.code, "batch_changed")
        self.assertEqual(self.refetch(self.batch).status, BatchStatus.DRAFT)
        self.assertEqual(self.sandbox_accounts().count(), 0)

    def test_submitting_a_preview_that_has_gone_stale_is_refused_and_it_stays_a_draft(self):
        batch = self.new_batch(term=self.term1)
        self.set_policy(account_mode="dynamic")
        with self.assertRaises(CollectionRefused) as caught:
            batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertEqual(self.refetch(batch).status, BatchStatus.DRAFT)
        batches.refresh_preview(self.maker, batch.id)
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)

    def test_the_ledger_itself_is_never_touched_by_any_of_this(self):
        before = ledger.family_position(self.bravo)
        self.set_policy(account_mode="dynamic")
        with self.assertRaises(CollectionRefused):
            self.approve()
        self.assertEqual(ledger.family_position(self.bravo), before)

    def test_a_batch_prepared_for_a_provider_that_is_no_longer_active_cannot_be_approved_or_run(self):
        self.connection.is_active_provider = False
        self.connection.save()
        with self.assertRaises(CollectionRefused) as caught:
            self.approve()
        self.assertEqual(caught.exception.code, "provider_changed")

    def test_submitting_needs_someone_selected_and_a_finished_policy(self):
        batch = self.new_batch()
        batches.set_selection(self.maker, batch.id, deselect_all=True)
        batch = self.refetch(batch)
        with self.assertRaises(CollectionRefused) as caught:
            batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(caught.exception.code, "batch_not_ready")
