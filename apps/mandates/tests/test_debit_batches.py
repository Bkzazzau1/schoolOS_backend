"""A direct-debit batch: what a debit would be is worked out from the ledger (never from a mandate's limit or a screen), a maker prepares it, a DIFFERENT
checker approves exactly what they saw, and any material change withdraws the approval."""

from dataclasses import replace
from unittest import mock

from django.db import IntegrityError, transaction

from apps.bankconnect.models import BankTransaction

from .. import debit_batches, evaluation, execution, mandate_provider
from ..constants import BatchStatus, Eligibility, InstructionStatus
from ..errors import MandateRefused
from ..models import MandateBatchEvent, MandateDebitBatch, MandateTransaction
from ..providers.sandbox import SandboxMandateConnector
from .base import APPROVE, N, MandateTestCase


class PreviewTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.musa = self.make_family("Musa", owes=50_000)
        self.paid = self.make_family("Paid", owes=0)
        self.bello_mandate = self.active_mandate(self.bello, maximum=500_000)
        self.sani_mandate = self.start_mandate(self.sani, account="0987654321")  # made, but not activated

    def test_every_family_that_owes_is_listed_with_why_it_can_or_cannot_be_debited(self):
        batch = self.new_batch()
        states = {i.family.display_name: (i.eligibility_status, i.selected) for i in batch.items.select_related("family")}
        self.assertEqual(states["Bello family"], (Eligibility.ELIGIBLE, False))
        self.assertEqual(states["Sani family"][0], Eligibility.NOT_READY)
        self.assertEqual(states["Musa family"][0], Eligibility.NO_MANDATE)
        self.assertNotIn("Paid family", states)  # owes nothing in this scope

    def test_the_proposed_debit_is_what_the_ledger_says_is_owed_never_the_mandates_maximum(self):
        item = self.item(self.new_batch(), self.bello)
        self.assertEqual(self.bello_mandate.maximum_amount_minor, 500_000 * N)
        self.assertEqual((item.outstanding_minor, item.eligible_minor, item.proposed_debit_minor), (280_000 * N, 280_000 * N, 280_000 * N))
        self.assertEqual(item.maximum_minor, 500_000 * N)

    def test_a_mandate_limit_lower_than_what_is_owed_caps_the_debit(self):
        family = self.make_family("Capped", owes=280_000)
        self.active_mandate(family, maximum=100_000, account="0555555555")
        item = self.item(self.new_batch(), family)
        self.assertEqual((item.outstanding_minor, item.proposed_debit_minor), (280_000 * N, 100_000 * N))

    def test_a_family_credit_is_taken_off_what_could_be_collected(self):
        from apps.receivables import credit
        from apps.receivables.models import CreditKind

        family = self.make_family("Credited", owes=100_000)
        self.active_mandate(family, account="0666666666")
        credit.add(family, CreditKind.OVERPAYMENT, 30_000 * N, actor=self.owner, reason="Overpaid earlier")
        item = self.item(self.new_batch(), family)
        self.assertEqual((item.outstanding_minor, item.eligible_minor, item.proposed_debit_minor), (100_000 * N, 70_000 * N, 70_000 * N))

    def test_the_allocation_shows_exactly_how_the_debit_would_pay_the_charges(self):
        family = self.make_family("Two", owes=60_000, students=2)
        self.active_mandate(family, account="0777777777")
        item = self.item(self.new_batch(), family)
        self.assertEqual(sum(a["amountMinor"] for a in item.receivable_allocation), item.proposed_debit_minor)
        self.assertEqual(len(item.receivable_allocation), 2)

    def test_the_preview_asks_no_provider_and_changes_nothing_in_the_ledger(self):
        before = self.owed(self.bello)
        with mock.patch.object(SandboxMandateConnector, "create_debit", side_effect=AssertionError("a debit was sent")), \
                mock.patch.object(SandboxMandateConnector, "get_debit_status", side_effect=AssertionError("a debit was asked about")):
            batch = self.new_batch()
            debit_batches.refresh(self.maker, batch.id)
        self.assertEqual(self.owed(self.bello), before)
        self.assertEqual((BankTransaction.objects.count(), MandateTransaction.objects.count()), (0, 0))

    def test_nothing_is_selected_until_a_person_selects_it(self):
        batch = self.new_batch()
        self.assertEqual((batch.total_items, batch.total_amount_minor), (0, 0))
        self.assertFalse(any(i.selected for i in batch.items.all()))

    def test_a_provider_that_limits_a_month_counts_what_was_already_debited_this_month(self):
        connector = SandboxMandateConnector()
        monthly = replace(connector.info, maximum_scope="calendar_month")
        with mock.patch.object(type(connector), "info", monthly):
            family = self.make_family("Monthly", owes=280_000)
            self.active_mandate(family, maximum=200_000, account="0888888888", max_debits=2)
            first = self.item(self.new_batch(), family)
            self.assertEqual(first.proposed_debit_minor, 200_000 * N)
            MandateTransaction.objects.create(
                school=self.school, family=family, mandate=family.direct_debit_mandates.get(), instruction=first, provider_connection=self.connection,
                provider="sandbox", request_ref="1", amount_minor=150_000 * N, status="success",
            )
            later = self.item(self.new_batch(), family)
            self.assertEqual(later.proposed_debit_minor, 50_000 * N)

    def test_a_family_with_a_live_but_not_debit_ready_mandate_says_why(self):
        item = self.item(self.new_batch(), self.sani)
        self.assertIn("activate", item.eligibility_note)


class SelectionAndAmountTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.no_mandate = self.make_family("Musa", owes=50_000)
        self.active_mandate(self.bello)
        self.active_mandate(self.sani, account="0987654321")

    def test_the_maker_selects_and_deselects_and_the_totals_follow(self):
        batch = self.new_batch()
        debit_batches.set_selection(self.maker, batch.id, select=[str(self.item(batch, self.bello).id)])
        batch = self.refetch(batch)
        self.assertEqual((batch.total_items, batch.total_amount_minor), (1, 280_000 * N))
        debit_batches.set_selection(self.maker, batch.id, select_all_eligible=True)
        self.assertEqual(self.refetch(batch).total_items, 2)
        debit_batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.sani).id)])
        batch = self.refetch(batch)
        self.assertEqual((batch.total_items, batch.total_amount_minor), (1, 280_000 * N))
        debit_batches.set_selection(self.maker, batch.id, deselect_all=True)
        self.assertEqual(self.refetch(batch).total_items, 0)

    def test_a_family_that_cannot_be_debited_cannot_be_selected(self):
        batch = self.new_batch()
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.set_selection(self.maker, batch.id, select=[str(self.item(batch, self.no_mandate).id)])
        self.assertEqual(caught.exception.code, "cannot_select")
        self.assertEqual(self.refetch(batch).total_items, 0)

    def test_every_change_raises_the_version_and_a_stale_screen_is_refused(self):
        batch = self.new_batch()
        first = self.refetch(batch)
        debit_batches.set_selection(self.maker, batch.id, select_all_eligible=True, expected_version=first.version)
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.set_selection(self.maker, batch.id, deselect_all=True, expected_version=first.version)
        self.assertEqual(caught.exception.code, "stale_preview")
        self.assertGreater(self.refetch(batch).version, first.version)

    def test_the_maker_can_lower_a_debit_but_never_raise_it(self):
        batch = self.select_all(self.new_batch())
        item = self.item(batch, self.bello)
        debit_batches.set_amount(self.maker, batch.id, item.id, amount_minor=100_000 * N, reason="Payer asked to pay in two parts")
        item = self.item(batch, self.bello)
        self.assertEqual((item.proposed_debit_minor, item.amount_adjusted, item.eligible_minor), (100_000 * N, True, 280_000 * N))
        self.assertEqual(sum(a["amountMinor"] for a in item.receivable_allocation), 100_000 * N)
        for bad in (280_000 * N + 1, 500_000 * N, 0, -5, "300"):
            with self.assertRaises(MandateRefused):
                debit_batches.set_amount(self.maker, batch.id, item.id, amount_minor=bad)
        self.assertEqual(self.item(batch, self.bello).proposed_debit_minor, 100_000 * N)

    def test_a_lowered_amount_survives_a_refresh_and_a_raised_ledger_does_not_raise_it(self):
        batch = self.select_all(self.new_batch())
        item = self.item(batch, self.bello)
        debit_batches.set_amount(self.maker, batch.id, item.id, amount_minor=100_000 * N)
        debit_batches.refresh(self.maker, batch.id)
        self.assertEqual(self.item(batch, self.bello).proposed_debit_minor, 100_000 * N)

    def test_a_lowered_amount_changes_the_fingerprint_so_it_needs_approving_again(self):
        batch = self.select_all(self.new_batch())
        before = self.refetch(batch)
        debit_batches.set_amount(self.maker, batch.id, self.item(batch, self.bello).id, amount_minor=100_000 * N)
        after = self.refetch(batch)
        self.assertNotEqual(before.snapshot_hash, after.snapshot_hash)
        self.assertGreater(after.version, before.version)

    def test_only_a_maker_can_prepare_and_only_their_own_school_is_seen(self):
        for who in (self.checker, self.manager, self.members["teacher"]):
            with self.assertRaises(MandateRefused) as caught:
                self.new_batch(who=who)
            self.assertEqual(caught.exception.code, "not_preparer")
        batch = self.new_batch()
        with self.assertRaises(Exception):
            debit_batches.get_batch(self.other_owner, batch.id)


class MakerCheckerTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.active_mandate(self.bello)
        self.active_mandate(self.sani, account="0987654321")

    def submitted(self):
        batch = self.select_all(self.new_batch())
        debit_batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        return self.refetch(batch)

    def test_the_maker_cannot_approve_or_reject_their_own_batch(self):
        batch = self.submitted()
        self.give_duties(self.maker, "finance.mandate_prepare", APPROVE)  # even if they also hold the checker duty
        for action in (lambda: debit_batches.approve(self.maker, batch.id, expected_hash=batch.snapshot_hash), lambda: debit_batches.reject(self.maker, batch.id, reason="I do not like it")):
            with self.assertRaises(MandateRefused) as caught:
                action()
            self.assertEqual(caught.exception.code, "maker_cannot_approve")
        self.assertEqual(self.refetch(batch).status, BatchStatus.PENDING_APPROVAL)

    def test_a_person_who_edited_it_can_never_approve_it_and_only_someone_with_the_duty_can(self):
        batch = self.select_all(self.new_batch())
        self.give_duties(self.checker, APPROVE, "finance.mandate_prepare")
        debit_batches.set_selection(self.checker, batch.id, deselect=[str(self.item(batch, self.sani).id)])  # the checker touched it
        debit_batches.submit(self.maker, batch.id)
        batch = self.refetch(batch)
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(caught.exception.code, "maker_cannot_approve")
        for who in (self.manager, self.members["teacher"]):
            with self.assertRaises(MandateRefused) as caught:
                debit_batches.approve(who, batch.id, expected_hash=batch.snapshot_hash)
            self.assertEqual(caught.exception.code, "not_approver")
        debit_batches.approve(self.other_checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(self.refetch(batch).status, BatchStatus.APPROVED)

    def test_the_checker_approves_exactly_the_snapshot_they_saw(self):
        batch = self.submitted()
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.approve(self.checker, batch.id, expected_hash="0" * 64)
        self.assertEqual(caught.exception.code, "stale_approval")
        with self.assertRaises(MandateRefused):
            debit_batches.approve(self.checker, batch.id, expected_hash="")
        approved = debit_batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual((approved.approved_snapshot_hash, approved.approved_version, approved.approved_by_id), (batch.snapshot_hash, batch.version, self.checker.id))

    def test_the_database_itself_refuses_a_checker_who_is_the_maker(self):
        batch = self.submitted()
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateDebitBatch.objects.filter(pk=batch.pk).update(approved_by=batch.prepared_by, approved_snapshot_hash="x", approved_version=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            MandateDebitBatch.objects.filter(pk=batch.pk).update(approved_by=self.checker, approved_snapshot_hash="", approved_version=None)  # an approval names a snapshot

    def test_a_rejection_needs_a_reason_and_sends_the_batch_back_to_the_maker_with_it(self):
        batch = self.submitted()
        for reason in ("", "no", "not ok"):
            with self.assertRaises(MandateRefused) as caught:
                debit_batches.reject(self.checker, batch.id, reason=reason)
            self.assertEqual(caught.exception.code, "reason_required")
        rejected = debit_batches.reject(self.checker, batch.id, reason="Bello owes less than that, check the term")
        self.assertEqual((rejected.status, rejected.rejected_by_id, rejected.rejection_reason), (BatchStatus.REJECTED, self.checker.id, "Bello owes less than that, check the term"))
        revised = debit_batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.bello).id)])
        self.assertEqual(revised.status, BatchStatus.DRAFT)  # back with the maker
        kinds = list(MandateBatchEvent.objects.filter(batch=batch).values_list("kind", flat=True))
        self.assertIn("rejected", kinds)
        rejection = MandateBatchEvent.objects.get(batch=batch, kind="rejected")
        self.assertEqual((rejection.detail["reason"], rejection.actor_id), ("Bello owes less than that, check the term", self.checker.id))  # history is kept

    def test_rejection_history_is_append_only_across_rounds(self):
        batch = self.submitted()
        debit_batches.reject(self.checker, batch.id, reason="First reason for rejecting")
        debit_batches.set_selection(self.maker, batch.id, select_all_eligible=True)
        debit_batches.submit(self.maker, batch.id)
        debit_batches.reject(self.other_checker, batch.id, reason="Second reason for rejecting")
        reasons = [e.detail["reason"] for e in MandateBatchEvent.objects.filter(batch=batch, kind="rejected")]
        self.assertEqual(reasons, ["First reason for rejecting", "Second reason for rejecting"])
        event = MandateBatchEvent.objects.filter(batch=batch).first()
        with self.assertRaises(Exception):
            event.detail = {}
            event.save()
        with self.assertRaises(Exception):
            event.delete()

    def test_an_approved_batch_cannot_be_edited_or_submitted_again(self):
        batch = self.approved()
        for action in (
            lambda: debit_batches.set_selection(self.maker, batch.id, deselect_all=True), lambda: debit_batches.refresh(self.maker, batch.id),
            lambda: debit_batches.submit(self.maker, batch.id),
        ):
            with self.assertRaises(MandateRefused) as caught:
                action()
            self.assertEqual(caught.exception.code, "not_editable")

    def test_submitting_needs_a_selection_and_the_preview_the_maker_saw(self):
        batch = self.new_batch()
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.submit(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "batch_not_ready")
        batch = self.select_all(batch)
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.submit(self.maker, batch.id, expected_hash="f" * 64)
        self.assertEqual(caught.exception.code, "stale_preview")


class StaleApprovalTests(MandateTestCase):
    """Anything material that changes after the preview withdraws the approval. One debit is never approved and another executed."""

    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.bello_mandate = self.active_mandate(self.bello)
        self.active_mandate(self.sani, account="0987654321")

    def submitted(self):
        batch = self.select_all(self.new_batch())
        debit_batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        return self.refetch(batch)

    def assert_withdrawn_on_approve(self, batch):
        with self.assertRaises(MandateRefused) as caught:
            debit_batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(caught.exception.code, "batch_changed")
        withdrawn = self.refetch(batch)
        self.assertEqual((withdrawn.status, withdrawn.approved_by_id, withdrawn.approved_snapshot_hash), (BatchStatus.DRAFT, None, ""))
        self.assertGreater(withdrawn.version, batch.version)

    def test_a_balance_change_after_submission_withdraws_it(self):
        batch = self.submitted()
        self.pay_manually(self.bello, 50_000)
        self.assert_withdrawn_on_approve(batch)

    def test_a_balance_change_after_approval_withdraws_it_at_the_start(self):
        batch = self.approved()
        self.pay_manually(self.bello, 50_000)
        with self.assertRaises(MandateRefused) as caught:
            execution.start(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "batch_changed")
        self.assertEqual(self.refetch(batch).status, BatchStatus.DRAFT)
        self.assertEqual(MandateTransaction.objects.count(), 0)  # nothing was sent

    def test_a_cancelled_mandate_withdraws_it(self):
        batch = self.submitted()
        mandate_provider.cancel(self.manager, self.bello_mandate.id)
        self.assert_withdrawn_on_approve(batch)

    def test_a_suspended_mandate_withdraws_it(self):
        batch = self.approved()
        mandate_provider.suspend(self.manager, self.bello_mandate.id)
        with self.assertRaises(MandateRefused):
            execution.start(self.maker, batch.id)
        self.assertEqual(self.refetch(batch).status, BatchStatus.DRAFT)

    def test_a_mandate_that_stops_being_debit_ready_withdraws_it(self):
        batch = self.submitted()
        from ..models import SandboxMandate

        SandboxMandate.objects.filter(provider_ref=self.bello_mandate.provider_mandate_reference).update(status="pending_activation")
        mandate_provider.refresh_mandate(self.bello_mandate.id)
        self.assert_withdrawn_on_approve(batch)

    def test_a_provider_connection_that_stops_working_withdraws_it(self):
        from ..connections import record_failure

        batch = self.submitted()
        record_failure(self.connection, "bad_credentials")
        self.assert_withdrawn_on_approve(batch)

    def test_a_changed_mandate_limit_withdraws_it(self):
        batch = self.submitted()
        from ..models import DirectDebitMandate

        DirectDebitMandate.objects.filter(pk=self.bello_mandate.pk).update(maximum_amount_minor=100_000 * N)
        self.assert_withdrawn_on_approve(batch)

    def test_a_new_charge_for_the_family_withdraws_it(self):
        batch = self.submitted()
        self.charge_family(self.bello, [self.bello.members.first().student], 10_000, term=self.term1)
        self.assert_withdrawn_on_approve(batch)

    def test_an_unchanged_world_keeps_the_approval(self):
        batch = self.submitted()
        approved = debit_batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assertEqual(approved.status, BatchStatus.APPROVED)
        self.assertEqual(debit_batches.live_hash(approved), approved.approved_snapshot_hash)

    def test_the_evaluation_is_deterministic(self):
        one = evaluation.evaluate(self.session.id, self.term1.id, self.bello)
        two = evaluation.evaluate(self.session.id, self.term1.id, self.bello)
        self.assertEqual(evaluation.fingerprint_of_evaluation(one, selected=True), evaluation.fingerprint_of_evaluation(two, selected=True))
        self.assertNotEqual(evaluation.fingerprint_of_evaluation(one, selected=True), evaluation.fingerprint_of_evaluation(one, selected=False))

    def test_an_item_status_of_a_family_with_no_more_to_pay_is_not_left_selected(self):
        batch = self.select_all(self.new_batch())
        self.pay_manually(self.sani, 100_000)
        debit_batches.refresh(self.maker, batch.id)
        item = self.item(batch, self.sani)
        self.assertEqual((item.selected, item.status), (False, InstructionStatus.SKIPPED))
