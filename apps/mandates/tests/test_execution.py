"""Sending approved debits: idempotent, asked-about-not-repeated when the outcome is unknown, refreshed against the ledger just before the provider is
asked, partial-failure tolerant, and only retried under the same approval when nothing approved has changed."""

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.bankconnect.models import BankTransaction

from .. import debit_batches, execution, jobs
from ..constants import BatchStatus, DebitOutcome, InstructionStatus, JobKind, MandateStatus
from ..errors import MandateRefused
from ..models import MandateDebitInstruction, MandateProviderJob, MandateTransaction, SandboxDebit
from ..providers import sandbox
from ..providers.sandbox import SandboxMandateConnector
from .base import N, MandateTestCase


class RunningTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.sani = self.make_family("Sani", owes=100_000)
        self.active_mandate(self.bello)
        self.active_mandate(self.sani, account="0987654321")

    def test_an_approved_batch_is_started_debited_and_settled_into_the_ledger(self):
        batch = self.approved()
        self.assertEqual((self.owed(self.bello), self.owed(self.sani)), (280_000 * N, 100_000 * N))
        done = self.run_batch(batch)
        self.assertEqual((done.status, done.success_count, done.failed_count), (BatchStatus.COMPLETED, 2, 0))
        self.assertEqual((self.owed(self.bello), self.owed(self.sani)), (0, 0))
        self.assertEqual(SandboxDebit.objects.count(), 2)
        self.assertEqual(sorted(t.amount_minor for t in MandateTransaction.objects.all()), [100_000 * N, 280_000 * N])
        self.assertTrue(all(t.status == DebitOutcome.SUCCESS and t.payment_id for t in MandateTransaction.objects.all()))
        self.assertLedgerHolds(self.bello)
        self.assertLedgerHolds(self.sani)

    def test_only_an_approved_batch_can_be_started_and_only_by_someone_who_prepares_or_approves(self):
        batch = self.select_all(self.new_batch())
        with self.assertRaises(MandateRefused) as caught:
            execution.start(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "not_approved")
        approved = self.approved()
        for who in (self.manager, self.members["teacher"]):
            with self.assertRaises(MandateRefused) as caught:
                execution.start(who, approved.id)
            self.assertEqual(caught.exception.code, "not_operator")
        self.assertEqual(MandateProviderJob.objects.count(), 0)

    def test_starting_twice_debits_once(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        with self.assertRaises(MandateRefused) as caught:
            execution.start(self.checker, batch.id)
        self.assertEqual(caught.exception.code, "not_approved")
        jobs.drain()
        jobs.drain()
        self.assertEqual((SandboxDebit.objects.count(), MandateTransaction.objects.count()), (2, 2))

    def test_running_the_same_debit_twice_never_debits_twice(self):
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.bello)
        again = execution.execute_instruction(item.id)  # a worker that picked the job up a second time
        self.assertEqual(again.result, "done")
        self.assertEqual((SandboxDebit.objects.count(), self.owed(self.bello)), (2, 0))
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 2)

    def test_every_debit_has_one_deterministic_reference_the_provider_is_given(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        first = {i.family_id: i.request_ref for i in MandateDebitInstruction.objects.filter(batch=batch, selected=True)}
        self.assertTrue(all(ref.isdigit() and len(ref) == 13 for ref in first.values()))
        self.assertEqual(len(set(first.values())), 2)
        jobs.drain()
        self.assertEqual(set(SandboxDebit.objects.values_list("request_ref", flat=True)), set(first.values()))

    def test_nothing_is_sent_for_a_family_that_paid_between_the_approval_and_the_provider_call(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)  # queued...
        self.pay_manually(self.bello, 280_000)  # ...and then the family pays another way before the worker runs
        jobs.drain()
        bello, sani = self.item(batch, self.bello), self.item(batch, self.sani)
        self.assertEqual((bello.status, bello.error_code), (InstructionStatus.FAILED, "changed_since_approval"))
        self.assertEqual(sani.status, InstructionStatus.SUCCESS)
        self.assertEqual(SandboxDebit.objects.count(), 1)  # only Sani's; Bello was never debited
        self.assertEqual(self.owed(self.bello), 0)  # and Bello paid exactly what they paid, once
        self.assertEqual(self.refetch(batch).status, BatchStatus.PARTIALLY_SUCCESSFUL)

    def test_a_debit_is_not_quietly_shrunk_and_sent_when_the_ledger_changed_a_little(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        self.pay_manually(self.bello, 30_000)  # what is owed is now less than what was approved
        jobs.drain()
        self.assertEqual(self.item(batch, self.bello).error_code, "changed_since_approval")
        self.assertFalse(SandboxDebit.objects.filter(amount_minor=250_000 * N).exists())
        self.assertFalse(MandateTransaction.objects.filter(family=self.bello).exists())

    def test_a_mandate_cancelled_after_the_batch_started_stops_that_debit_and_only_that_one(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        from .. import mandate_provider

        mandate_provider.cancel(self.manager, self.bello.direct_debit_mandates.get().id)
        jobs.drain()
        self.assertEqual(self.item(batch, self.bello).error_code, "changed_since_approval")
        self.assertEqual(self.item(batch, self.sani).status, InstructionStatus.SUCCESS)
        self.assertEqual(SandboxDebit.objects.count(), 1)

    def test_the_provider_is_never_called_inside_a_database_transaction(self):
        from django.db import connection

        seen = []
        real = SandboxMandateConnector.create_debit
        baseline = len(connection.savepoint_ids)  # the test's own transaction is not the code's: only a transaction the CODE opened counts

        def spy(connector, *args, **kwargs):
            seen.append(len(connection.savepoint_ids) == baseline)
            return real(connector, *args, **kwargs)

        with mock.patch.object(SandboxMandateConnector, "create_debit", spy):
            batch = self.approved()
            execution.start(self.maker, batch.id)
            jobs.run_next()
        self.assertEqual(seen, [True])  # no transaction of the code's own was open


class UnknownOutcomeTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.bello = self.make_family("Bello", owes=280_000)
        self.active_mandate(self.bello)

    def start_running(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        return batch

    def test_a_timeout_after_the_provider_debited_is_unknown_then_found_and_recorded_once(self):
        sandbox.inject_fault("create_debit", "timeout_after")
        batch = self.start_running()
        jobs.run_next()  # the debit: the provider did it, and its answer was lost
        item = self.item(batch, self.bello)
        self.assertEqual((item.status, MandateTransaction.objects.get().status), (InstructionStatus.UNKNOWN, DebitOutcome.UNKNOWN))
        self.assertEqual((SandboxDebit.objects.count(), self.owed(self.bello)), (1, 280_000 * N))  # done at the provider, not yet in the ledger
        self.assertEqual(self.refetch(batch).status, BatchStatus.PROCESSING)  # not finished, and not "failed"
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))  # the requery
        done = self.item(batch, self.bello)
        self.assertEqual((done.status, SandboxDebit.objects.count(), self.owed(self.bello)), (InstructionStatus.SUCCESS, 1, 0))
        self.assertEqual(BankTransaction.objects.filter(transaction_type="direct_debit").count(), 1)

    def test_a_timeout_before_the_provider_saw_it_is_found_never_sent_and_only_then_sent_again_with_the_same_reference(self):
        sandbox.inject_fault("create_debit", "timeout_before")
        batch = self.start_running()
        jobs.run_next()
        item = self.item(batch, self.bello)
        reference = item.request_ref
        self.assertEqual((item.status, SandboxDebit.objects.count()), (InstructionStatus.UNKNOWN, 0))
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))  # asked: the provider has no such request, so it is sent again
        done = self.item(batch, self.bello)
        self.assertEqual((done.status, done.request_ref, SandboxDebit.objects.count()), (InstructionStatus.SUCCESS, reference, 1))
        self.assertEqual(SandboxDebit.objects.get().request_ref, reference)

    def test_an_unknown_debit_is_asked_about_before_anything_else_even_if_the_debit_job_runs_again(self):
        sandbox.inject_fault("create_debit", "timeout_after")
        batch = self.start_running()
        jobs.run_next()
        item = self.item(batch, self.bello)
        again = execution.execute_instruction(item.id, attempt=2)  # the debit job is picked up again
        self.assertEqual((again.result, again.code), ("done", "requery"))
        self.assertEqual(SandboxDebit.objects.count(), 1)  # never sent again

    def test_an_unknown_debit_is_never_retried_by_a_person_either(self):
        sandbox.inject_fault("create_debit", "timeout_after")
        batch = self.start_running()
        jobs.run_next()
        with self.assertRaises(MandateRefused) as caught:
            execution.retry(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "not_retryable")
        self.assertEqual(SandboxDebit.objects.count(), 1)

    def test_a_provider_that_stays_silent_leaves_it_unknown_and_flagged_and_never_sends_it_again(self):
        sandbox.inject_fault("create_debit", "timeout_after")
        for _ in range(12):
            sandbox.inject_fault("get_debit_status", "unavailable")
        batch = self.start_running()
        jobs.run_next()
        for hours in range(1, 12):
            jobs.drain(now_fn=lambda h=hours: timezone.now() + timedelta(hours=h * 3))
        item = self.item(batch, self.bello)
        self.assertEqual(item.status, InstructionStatus.UNKNOWN)
        self.assertEqual(SandboxDebit.objects.count(), 1)
        self.assertEqual(self.owed(self.bello), 280_000 * N)  # nothing invented in the ledger
        from ..models import MandateAuditEvent

        self.assertTrue(MandateAuditEvent.objects.filter(kind="debit_outcome_unresolved").exists())
        self.assertEqual(jobs.unresolved(self.school).count(), 1)

    def test_a_pending_debit_is_not_settled_until_the_provider_says_it_succeeded(self):
        sandbox.set_next_debit_status("pending")
        batch = self.start_running()
        jobs.run_next()
        item = self.item(batch, self.bello)
        self.assertEqual((item.status, MandateTransaction.objects.get().status), (InstructionStatus.DEBITING, DebitOutcome.PENDING))
        self.assertEqual((self.owed(self.bello), BankTransaction.objects.filter(transaction_type="direct_debit").count()), (280_000 * N, 0))
        SandboxDebit.objects.update(status="success")
        jobs.drain(now_fn=lambda: timezone.now() + timedelta(hours=1))
        self.assertEqual((self.item(batch, self.bello).status, self.owed(self.bello)), (InstructionStatus.SUCCESS, 0))

    def test_the_intent_is_written_before_the_provider_is_asked(self):
        seen = []
        real = SandboxMandateConnector.create_debit

        def spy(connector, secret, request, **kw):
            seen.append(MandateTransaction.objects.filter(request_ref=request.request_ref, status=DebitOutcome.PENDING).exists())
            return real(connector, secret, request, **kw)

        with mock.patch.object(SandboxMandateConnector, "create_debit", spy):
            self.start_running()
            jobs.run_next()
        self.assertEqual(seen, [True])

    def test_a_worker_that_died_after_writing_the_intent_asks_before_it_sends(self):
        batch = self.start_running()
        item = self.item(batch, self.bello)
        real = SandboxMandateConnector.create_debit
        with mock.patch.object(SandboxMandateConnector, "create_debit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                jobs.run_next()  # the worker dies inside the provider call
        self.assertEqual(self.item(batch, self.bello).status, InstructionStatus.DEBITING)
        job = MandateProviderJob.objects.get(instruction=item, kind=JobKind.DEBIT)
        MandateProviderJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        jobs.drain()  # its lease ran out: the job is picked up again
        self.assertEqual(self.item(batch, self.bello).status, InstructionStatus.SUCCESS)
        self.assertEqual(SandboxDebit.objects.count(), 1)
        self.assertIsNotNone(real)


class FailureAndRetryTests(MandateTestCase):
    def setUp(self):
        super().setUp()
        self.families = [self.make_family(name, owes=100_000) for name in ("Adamu", "Bello", "Chike")]
        for i, family in enumerate(self.families):
            self.active_mandate(family, account=f"01234567{i}0")

    def fail_for(self, family):
        real = SandboxMandateConnector.create_debit
        refs = {self.item_ref(family)}

        def choose(connector, secret, request, **kw):
            if request.request_ref in refs:
                return mock.sentinel.failed
            return real(connector, secret, request, **kw)

        return choose

    def item_ref(self, family):
        return self.item(self.batch, family).request_ref

    def run_with_one_failure(self):
        self.batch = self.approved()
        execution.start(self.maker, self.batch.id)
        target = self.families[1]
        sandbox.set_next_debit_status("success")
        original = SandboxMandateConnector.create_debit
        ref = self.item(self.batch, target).request_ref

        def wrapper(connector, secret, request, **kw):
            from ..providers.base import DebitResult

            if request.request_ref == ref:
                return DebitResult(outcome=DebitOutcome.FAILED, provider_status="No sufficient funds", provider_status_code="051", failure_code="insufficient_funds")
            return original(connector, secret, request, **kw)

        with mock.patch.object(SandboxMandateConnector, "create_debit", wrapper):
            jobs.drain()
        return target

    def test_a_partial_failure_keeps_the_successes_and_says_what_failed(self):
        target = self.run_with_one_failure()
        batch = self.refetch(self.batch)
        self.assertEqual((batch.status, batch.success_count, batch.failed_count), (BatchStatus.PARTIALLY_SUCCESSFUL, 2, 1))
        failed = self.item(batch, target)
        self.assertEqual((failed.status, failed.error_code), (InstructionStatus.FAILED, "insufficient_funds"))
        self.assertIn("enough money", failed.safe_error_message)
        self.assertEqual(self.owed(target), 100_000 * N)  # the failure changed nothing in the ledger
        self.assertEqual(sum(1 for f in self.families if self.owed(f) == 0), 2)  # and the two that were debited stay debited

    def test_an_unchanged_failed_debit_is_retried_under_the_same_approval_with_a_new_reference(self):
        target = self.run_with_one_failure()
        approval_before = self.refetch(self.batch)
        old_ref = self.item(self.batch, target).request_ref
        result = execution.retry(self.maker, self.batch.id)
        self.assertEqual((len(result["retried"]), result["needsFreshApproval"]), (1, []))
        jobs.drain()
        batch = self.refetch(self.batch)
        item = self.item(batch, target)
        self.assertEqual((batch.status, item.status, item.retry_round), (BatchStatus.COMPLETED, InstructionStatus.SUCCESS, 1))
        self.assertNotEqual(item.request_ref, old_ref)
        self.assertEqual((batch.approved_snapshot_hash, batch.approved_by_id), (approval_before.approved_snapshot_hash, approval_before.approved_by_id))  # no new approval
        self.assertEqual(self.owed(target), 0)
        self.assertEqual(SandboxDebit.objects.count(), 3)

    def test_a_failed_debit_whose_facts_changed_needs_a_fresh_maker_and_checker(self):
        target = self.run_with_one_failure()
        self.pay_manually(target, 20_000)  # the family paid part of it meanwhile
        result = execution.retry(self.maker, self.batch.id)
        self.assertEqual((result["retried"], [n["family"] for n in result["needsFreshApproval"]]), ([], [target.display_name]))
        self.assertEqual(SandboxDebit.objects.count(), 2)  # nothing was sent
        self.assertEqual(self.refetch(self.batch).status, BatchStatus.PARTIALLY_SUCCESSFUL)

    def test_retry_selected_only_retries_what_was_chosen(self):
        target = self.run_with_one_failure()
        other = self.item(self.batch, self.families[0])
        result = execution.retry(self.maker, self.batch.id, item_ids=[str(other.id)])
        self.assertEqual(result["retried"], [])  # that one did not fail
        result = execution.retry(self.maker, self.batch.id, item_ids=[str(self.item(self.batch, target).id)])
        self.assertEqual(len(result["retried"]), 1)

    def test_a_failure_that_no_retry_can_fix_says_so(self):
        batch = self.approved()
        execution.start(self.maker, batch.id)
        self.pay_manually(self.families[0], 100_000)
        jobs.drain()
        result = execution.retry(self.maker, batch.id)
        self.assertEqual([n["code"] for n in result["needsFreshApproval"]], ["changed_since_approval"])

    def test_only_a_batch_that_finished_with_failures_can_be_retried(self):
        batch = self.run_batch(self.approved())
        with self.assertRaises(MandateRefused) as caught:
            execution.retry(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "not_retryable")

    def test_progress_says_what_is_done_failed_and_waiting(self):
        target = self.run_with_one_failure()
        progress = execution.progress(self.refetch(self.batch))
        self.assertEqual((progress["success"], progress["failed"], progress["unknown"], progress["done"], progress["total"]), (2, 1, 0, True, 3))
        self.assertIsNotNone(target)

    def test_a_provider_that_cannot_debit_through_schoolos_yet_is_not_offered_a_debit(self):
        from dataclasses import replace

        connector = SandboxMandateConnector()
        no_debit = replace(connector.info.capabilities, supports_manual_debit=False)
        with mock.patch.object(type(connector), "info", replace(connector.info, capabilities=no_debit)):
            batch = self.new_batch()
            statuses = {i.eligibility_status for i in batch.items.all()}
            self.assertEqual(statuses, {"provider_cannot_debit"})
            with self.assertRaises(MandateRefused) as caught:  # nothing can be selected, so nothing can be submitted
                debit_batches.submit(self.maker, batch.id)
            self.assertEqual(caught.exception.code, "batch_not_ready")
        self.assertEqual(self.families[0].direct_debit_mandates.get().status, MandateStatus.ACTIVE)
