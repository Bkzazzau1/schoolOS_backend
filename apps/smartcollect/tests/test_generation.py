"""Generating the accounts: nothing reaches a provider before approval, every call is made from the job queue, generation is idempotent, a
provider that does not answer is tried again without making a second account, partial failure keeps every success, and a retry reuses the
approval only while the snapshot is unchanged."""

from unittest import mock

from django.utils import timezone

from apps.bankconnect.models import BankAuditEvent
from apps.bankconnect.providers import base, sandbox
from apps.receivables.models import FamilyCollectionAccount

from .. import batches, generation, jobs, policy, running
from ..constants import MAX_ATTEMPTS, BatchStatus, GenerationStatus, JobStatus
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent, CollectionBatchEvent, CollectionGenerationBatchItem, ProviderJob
from .base import CollectTestCase


class NothingBeforeApprovalTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.make_family("Alpha"), self.make_family("Bravo")

    def test_no_provider_call_no_job_and_no_account_exists_until_the_batch_is_approved_and_started(self):
        batch = self.new_batch()
        self.assert_untouched()
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        self.assert_untouched()
        batch = self.refetch(batch)
        batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.assert_untouched()
        self.assertEqual(jobs.drain(), 0)
        self.assert_untouched()

    def assert_untouched(self):
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count(), ProviderJob.objects.count()), (0, 0, 0))

    def test_only_an_approved_batch_can_be_started(self):
        batch = self.new_batch()
        for stage in ("draft", "pending"):
            with self.assertRaises(CollectionRefused) as caught:
                running.start_processing(self.maker, batch.id)
            self.assertEqual(caught.exception.code, "not_approved")
            if stage == "draft":
                batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)

    def test_starting_needs_authority_and_cannot_be_repeated(self):
        batch = self.approved()
        with self.assertRaises(CollectionRefused) as caught:
            running.start_processing(self.members["teacher"], batch.id)
        self.assertEqual(caught.exception.code, "not_preparer")
        running.start_processing(self.maker, batch.id)
        with self.assertRaises(CollectionRefused) as caught:
            running.start_processing(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "not_approved")

    def test_starting_queues_the_work_and_makes_no_call_itself(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        self.assertEqual(ProviderJob.objects.filter(status=JobStatus.QUEUED).count(), 2)
        self.assertEqual((self.sandbox_accounts().count(), self.refetch(batch).status), (0, BatchStatus.PROCESSING))

    def test_a_cancelled_batch_makes_nothing_even_if_a_job_was_already_queued(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        from ..models import CollectionGenerationBatch

        CollectionGenerationBatch.objects.filter(pk=batch.pk).update(status=BatchStatus.CANCELLED)
        jobs.drain()
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count()), (0, 0))


class SuccessTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b = self.make_family("Alpha"), self.make_family("Bravo", owes=50_000)

    def test_every_selected_family_gets_one_account_from_the_active_provider_and_the_batch_completes(self):
        batch = self.run_batch(self.approved())
        self.assertEqual((batch.status, batch.success_count, batch.failed_count), (BatchStatus.COMPLETED, 2, 0))
        for family in (self.a, self.b):
            (account,) = self.live_accounts(family)
            self.assertEqual((account.provider, account.connection, account.origin, account.status), ("sandbox", self.connection, "provider", "active"))
            self.assertTrue(account.account_number and account.external_account_ref)
        self.assertEqual(self.sandbox_accounts().count(), 2)

    def test_the_item_records_what_happened_and_the_account_knows_which_batch_made_it(self):
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.a)
        (account,) = self.live_accounts(self.a)
        self.assertEqual((item.generation_status, item.account, item.attempt_count, item.error_code), (GenerationStatus.SUCCESS, account, 1, ""))
        self.assertEqual(item.provider_account_reference, account.external_account_ref)
        self.assertEqual(account.generation_items.get().batch, batch)
        self.assertEqual((account.idempotency_key, item.idempotency_key), (item.idempotency_key, account.idempotency_key))
        self.assertEqual(account.collection_target_minor, item.proposed_collection_minor)

    def test_a_static_account_records_how_long_it_is_promised(self):
        self.set_policy(reuse_scope="one_session")
        batch = self.run_batch(self.approved())
        (account,) = self.live_accounts(self.a)
        self.assertEqual((account.account_mode, account.reuse_scope, account.scope_session, account.valid_until), ("static", "one_session", self.session, self.session.ends_on))
        self.assertEqual(batch.status, BatchStatus.COMPLETED)

    def test_the_terms_promise_and_the_indefinite_promise(self):
        self.set_policy(reuse_scope="selected_terms", reuse_count=2)
        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.a)
        self.assertEqual((account.reuse_scope, account.reuse_count, account.valid_until), ("selected_terms", 2, self.term2.ends_on))

    def test_a_dynamic_account_is_made_for_this_period_and_its_target(self):
        self.set_policy(account_mode="dynamic")
        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.b)
        self.assertEqual((account.account_mode, account.scope_term, account.valid_until, account.collection_target_minor), ("dynamic", self.term1, self.term1.ends_on, 50_000 * 100))

    def test_the_account_is_made_for_the_family_not_a_child(self):
        many = self.make_family("Many", students=3)
        batch = self.run_batch(self.approved())
        self.assertEqual(len(self.live_accounts(many)), 1)
        self.assertEqual(batch.status, BatchStatus.COMPLETED)

    def test_a_one_time_family_override_is_used_up_by_the_account_it_was_for(self):
        policy.set_override(self.owner, scope="family", family=self.a, values={"account_mode": "dynamic"}, reason="Once", expiry_kind="one_time")
        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.a)
        self.assertEqual(account.account_mode, "dynamic")
        override = self.a.__class__.objects.get(pk=self.a.pk)
        self.assertEqual(policy.resolve(self.school, session=self.session, term=self.term1, family=override)["account_mode"], "static")

    def test_the_whole_thing_is_audited_without_a_secret(self):
        self.run_batch(self.approved())
        kinds = set(CollectionAuditEvent.objects.values_list("kind", flat=True))
        self.assertTrue({"batch_created", "batch_submitted", "batch_approved", "batch_started", "account_generated"} <= kinds)
        blob = str(list(CollectionAuditEvent.objects.values_list("detail", flat=True))) + str(list(BankAuditEvent.objects.values_list("detail", flat=True)))
        self.assertNotIn("sandbox-key-1", blob)

    def test_the_families_the_maker_did_not_select_are_left_alone(self):
        batch = self.new_batch()
        batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.b).id)])
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batch = self.refetch(batch)
        batches.approve(self.checker, batch.id, expected_hash=batch.snapshot_hash)
        self.run_batch(batch)
        self.assertEqual((len(self.live_accounts(self.a)), len(self.live_accounts(self.b))), (1, 0))


class IdempotencyTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_family("Alpha")

    def test_the_key_and_the_provider_reference_are_the_same_on_every_attempt(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        item = self.item(batch, self.a)
        self.assertEqual(item.idempotency_key, running.generation_key(batch, self.a.id))
        self.assertEqual(item.provider_request_reference, running.provider_reference(item))
        self.assertTrue(item.provider_request_reference.startswith("SOS-"))

    def test_running_a_finished_item_again_makes_nothing_more(self):
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.a)
        for _ in range(3):
            self.assertEqual(generation.execute_item(item.id).result, "done")
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count()), (1, 1))

    def test_a_worker_that_died_after_the_account_was_recorded_finds_it_and_marks_the_item(self):
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.a)
        CollectionGenerationBatchItem.objects.filter(pk=item.pk).update(generation_status=GenerationStatus.GENERATING, account=None, provider_account_reference="x")
        from ..models import CollectionGenerationBatch

        CollectionGenerationBatch.objects.filter(pk=batch.pk).update(status=BatchStatus.PROCESSING)
        generation.execute_item(item.id, attempt=2)
        item.refresh_from_db()
        self.assertEqual((item.generation_status, item.account is not None, FamilyCollectionAccount.objects.count()), (GenerationStatus.SUCCESS, True, 1))

    def test_the_same_job_cannot_be_queued_twice(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        item = self.item(batch, self.a)
        jobs.enqueue_provision(item)
        jobs.enqueue_provision(item)
        self.assertEqual(ProviderJob.objects.filter(item=item).count(), 1)

    def test_the_database_will_not_hold_two_accounts_for_one_generation_key_or_two_live_for_one_family(self):
        from django.db import IntegrityError, transaction

        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyCollectionAccount.objects.create(
                school=self.school, family=self.a, provider="sandbox", connection=self.connection, account_number="9999999990", status="active",
                idempotency_key="another-key",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyCollectionAccount.objects.create(
                school=self.school, family=self.make_family("Other"), provider="sandbox", connection=self.connection, account_number="9999999991",
                idempotency_key=account.idempotency_key,
            )


class PartialFailureTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.make_family("Alpha"), self.make_family("Bravo"), self.make_family("Charlie")

    def break_for(self, family, error=None, times=1):
        sandbox.inject_fault(error or base.ProviderRejected("provider_rejected", "The provider did not accept the request."), match=family.code, times=times)

    def test_one_family_failing_does_not_undo_or_stop_the_others(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        self.assertEqual((batch.status, batch.success_count, batch.failed_count), (BatchStatus.PARTIALLY_SUCCESSFUL, 2, 1))
        self.assertEqual((len(self.live_accounts(self.a)), len(self.live_accounts(self.b)), len(self.live_accounts(self.c))), (1, 0, 1))
        item = self.item(batch, self.b)
        self.assertEqual((item.generation_status, item.error_code, item.safe_error_message), (GenerationStatus.FAILED, "provider_rejected", "The provider did not accept the request."))
        self.assertEqual(item.attempt_count, 1)

    def test_every_family_failing_makes_the_batch_failed(self):
        for family in (self.a, self.b, self.c):
            self.break_for(family)
        self.assertEqual(self.run_batch(self.approved()).status, BatchStatus.FAILED)

    def test_the_failure_says_what_happened_in_the_schools_words_and_never_the_providers_or_a_secret(self):
        self.break_for(self.b, base.ProviderRejected("provider_rejected", "The provider did not accept the request."))
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.b)
        blob = " ".join([item.safe_error_message, item.error_code, str(item.checkpoint), str(list(CollectionAuditEvent.objects.values_list("detail", flat=True)))])
        self.assertNotIn("sandbox-key-1", blob)
        self.assertNotIn("Traceback", blob)

    def test_retry_all_failed_makes_only_the_failed_ones_and_the_successes_are_untouched(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        before = {f.id: self.live_accounts(f) for f in (self.a, self.c)}
        approved_by = batch.approved_by
        _, retried = running.retry_failed(self.maker, batch.id)
        self.assertEqual(retried, 1)
        jobs.drain()
        batch = self.refetch(batch)
        self.assertEqual((batch.status, batch.success_count, batch.failed_count), (BatchStatus.COMPLETED, 3, 0))
        self.assertEqual({f.id: self.live_accounts(f) for f in (self.a, self.c)}, before)  # the accounts already made were not touched
        self.assertEqual(self.sandbox_accounts().count(), 3)
        self.assertEqual(batch.approved_by, approved_by)  # the same approval stood

    def test_retry_selected_families_only(self):
        self.break_for(self.a)
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        self.assertEqual(batch.failed_count, 2)
        failed_b = self.item(batch, self.b)
        _, retried = running.retry_failed(self.maker, batch.id, item_ids=[str(failed_b.id)])
        jobs.drain()
        batch = self.refetch(batch)
        self.assertEqual((retried, batch.status, batch.success_count, batch.failed_count), (1, BatchStatus.PARTIALLY_SUCCESSFUL, 2, 1))
        self.assertEqual((len(self.live_accounts(self.a)), len(self.live_accounts(self.b))), (0, 1))

    def test_a_family_that_did_not_fail_cannot_be_retried(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        with self.assertRaises(CollectionRefused) as caught:
            running.retry_failed(self.maker, batch.id, item_ids=[str(self.item(batch, self.a).id)])
        self.assertEqual(caught.exception.code, "item_not_failed")

    def test_there_is_nothing_to_retry_in_a_completed_batch(self):
        batch = self.run_batch(self.approved())
        with self.assertRaises(CollectionRefused) as caught:
            running.retry_failed(self.maker, batch.id)
        self.assertEqual(caught.exception.code, "nothing_to_retry")

    def test_a_retry_under_an_unchanged_snapshot_reuses_the_approval_without_asking_again(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        running.retry_failed(self.maker, batch.id)
        self.assertEqual(self.refetch(batch).status, BatchStatus.PROCESSING)
        self.assertEqual(CollectionBatchEvent.objects.filter(batch=batch, kind="approved").count(), 1)

    def test_a_retry_after_something_material_changed_needs_a_fresh_approval_and_keeps_the_successes(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        self.set_policy(settlement_action="manual")  # the snapshot changes
        _, retried = running.retry_failed(self.maker, batch.id)
        self.assertEqual(retried, 0)
        batch = self.refetch(batch)
        self.assertEqual((batch.status, batch.approved_by), (BatchStatus.DRAFT, None))
        self.assertEqual(jobs.drain(), 0)
        self.assertEqual(len(self.live_accounts(self.b)), 0)
        self.assertEqual((len(self.live_accounts(self.a)), len(self.live_accounts(self.c))), (1, 1))  # successes stay
        self.assertEqual(self.item(batch, self.a).generation_status, GenerationStatus.SUCCESS)
        # approved afresh, then only the failed family is made
        batches.refresh_preview(self.maker, batch.id)
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        running.start_processing(self.maker, batch.id)
        jobs.drain()
        batch = self.refetch(batch)
        self.assertEqual((batch.status, batch.success_count, self.sandbox_accounts().count()), (BatchStatus.COMPLETED, 3, 3))

    def test_a_success_is_never_regenerated_even_when_the_batch_is_reapproved(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        first_numbers = {f.id: self.live_accounts(f)[0].account_number for f in (self.a, self.c)}
        self.set_policy(settlement_action="manual")
        running.retry_failed(self.maker, batch.id)
        batches.refresh_preview(self.maker, batch.id)
        batch = self.refetch(batch)
        batches.submit(self.maker, batch.id, expected_hash=batch.snapshot_hash)
        batches.approve(self.checker, batch.id, expected_hash=self.refetch(batch).snapshot_hash)
        running.start_processing(self.maker, batch.id)
        jobs.drain()
        self.assertEqual({f.id: self.live_accounts(f)[0].account_number for f in (self.a, self.c)}, first_numbers)
        self.assertEqual(self.item(batch, self.a).attempt_count, 1)

    def test_a_success_cannot_be_deselected_or_changed(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        self.set_policy(settlement_action="manual")
        running.retry_failed(self.maker, batch.id)
        with self.assertRaises(CollectionRefused) as caught:
            batches.set_selection(self.maker, batch.id, deselect=[str(self.item(batch, self.a).id)])
        self.assertEqual(caught.exception.code, "cannot_select")

    def test_failed_families_are_listed_with_why(self):
        self.break_for(self.b)
        batch = self.run_batch(self.approved())
        failed = CollectionGenerationBatchItem.objects.filter(batch=batch, generation_status=GenerationStatus.FAILED)
        self.assertEqual([i.family for i in failed], [self.b])


class UnknownOutcomeTests(CollectTestCase):
    """The provider did not answer: the request may have been carried out. Trying again finds it instead of making a second account."""

    def setUp(self):
        super().setUp()
        self.a = self.make_family("Alpha")

    def test_a_provider_that_does_not_answer_leaves_the_item_waiting_with_a_growing_delay(self):
        sandbox.inject_fault(base.ProviderUnavailable(), match=self.a.code, times=1)
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        self.assertEqual(jobs.drain(), 1)
        item = self.item(batch, self.a)
        job = ProviderJob.objects.get(item=item)
        self.assertEqual((item.generation_status, item.error_code, job.status, job.attempts), (GenerationStatus.RETRYING, "provider_unavailable", JobStatus.RETRY, 1))
        self.assertGreater(job.run_after, timezone.now())
        self.assertEqual(self.refetch(batch).status, BatchStatus.PROCESSING)  # still going: it is not a failure yet
        self.assertEqual(jobs.drain(), 0)  # not due
        later = timezone.now() + timezone.timedelta(hours=1) if hasattr(timezone, "timedelta") else None
        from datetime import timedelta

        jobs.run_next(timezone.now() + timedelta(hours=1))
        item.refresh_from_db()
        self.assertEqual((item.generation_status, self.refetch(batch).status, later is not None), (GenerationStatus.SUCCESS, BatchStatus.COMPLETED, True))
        self.assertEqual(item.attempt_count, 2)

    def test_when_the_provider_made_the_account_but_the_answer_was_lost_the_retry_finds_it_and_makes_no_second(self):
        connector = sandbox.SandboxConnector
        real = connector.provision_family_collection_account
        calls = {"n": 0}

        def made_then_silence(self_, secret, request, **kw):
            result = real(self_, secret, request, **kw)  # the provider carried it out ...
            calls["n"] += 1
            if calls["n"] == 1:
                raise base.ProviderUnavailable()  # ... but its answer never arrived
            return result

        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        with mock.patch.object(connector, "provision_family_collection_account", made_then_silence):
            jobs.drain()
            from datetime import timedelta

            jobs.run_next(timezone.now() + timedelta(hours=1))
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count(), self.refetch(batch).status), (1, 1, BatchStatus.COMPLETED))

    def test_after_the_last_attempt_the_item_fails_for_a_person_to_retry_and_says_so(self):
        sandbox.inject_fault(base.ProviderUnavailable(), match=self.a.code, times=MAX_ATTEMPTS + 2)
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        from datetime import timedelta

        for round_number in range(MAX_ATTEMPTS):
            jobs.run_next(timezone.now() + timedelta(hours=round_number + 1))
        item = self.item(batch, self.a)
        self.assertEqual((item.generation_status, item.error_code, item.attempt_count), (GenerationStatus.FAILED, "provider_unavailable", MAX_ATTEMPTS))
        self.assertEqual((self.refetch(batch).status, ProviderJob.objects.get(item=item).status), (BatchStatus.FAILED, JobStatus.FAILED))
        sandbox.clear_faults()
        running.retry_failed(self.maker, batch.id)
        jobs.drain()
        self.assertEqual(self.refetch(batch).status, BatchStatus.COMPLETED)
        self.assertEqual(self.sandbox_accounts().count(), 1)

    def test_bad_credentials_fail_the_item_at_once_and_mark_the_connection_for_new_credentials(self):
        sandbox.inject_fault(base.BadCredentials(), match=self.a.code, times=1)
        batch = self.run_batch(self.approved())
        item = self.item(batch, self.a)
        self.assertEqual((item.generation_status, item.error_code), (GenerationStatus.FAILED, "bad_credentials"))
        self.assertEqual(self.connection_row().status, "needs_reauth")

    def test_a_bug_in_schoolos_is_retried_then_failed_and_never_kills_the_worker_or_leaks_its_text(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        from datetime import timedelta

        with mock.patch.object(generation, "customer_of", side_effect=RuntimeError("secret detail 12345")):
            with self.assertLogs("apps.smartcollect.jobs", "ERROR"):
                for round_number in range(MAX_ATTEMPTS):
                    jobs.run_next(timezone.now() + timedelta(hours=round_number + 1))
        item = self.item(batch, self.a)
        self.assertEqual((item.generation_status, item.error_code), (GenerationStatus.FAILED, "internal_error"))
        self.assertNotIn("12345", item.safe_error_message)


class GuardTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_family("Alpha")

    def test_if_the_provider_stops_being_the_active_one_before_the_call_nothing_is_made(self):
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        self.connection.is_active_provider = False
        self.connection.save()
        jobs.drain()
        item = self.item(batch, self.a)
        self.assertEqual((item.generation_status, item.error_code), (GenerationStatus.FAILED, "provider_changed"))
        self.assertEqual((self.sandbox_accounts().count(), FamilyCollectionAccount.objects.count()), (0, 0))

    def test_a_family_that_got_an_account_by_other_means_since_is_not_given_a_second(self):
        from apps.bankconnect.providers.base import ProvisionedAccount
        from apps.receivables import collection_accounts

        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        collection_accounts.create_from_provider(
            self.a, self.connection, ProvisionedAccount(account_number="9000000111", provider_account_ref="R1", lookup_ref="9000000111"),
            actor=self.owner, idempotency_key="someone-else",
        )
        jobs.drain()
        item = self.item(batch, self.a)
        self.assertEqual((item.generation_status, item.error_code), (GenerationStatus.FAILED, "account_exists"))
        self.assertEqual(FamilyCollectionAccount.objects.count(), 1)

    def test_a_batch_that_no_longer_matches_its_approval_generates_nothing(self):
        from ..models import CollectionGenerationBatch

        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        CollectionGenerationBatch.objects.filter(pk=batch.pk).update(snapshot_hash="f" * 64)
        jobs.drain()
        self.assertEqual((self.item(batch, self.a).error_code, FamilyCollectionAccount.objects.count()), ("approval_mismatch", 0))

    def test_the_family_being_deactivated_after_approval_is_not_given_an_account(self):
        from apps.receivables import families

        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        families.set_status(self.a, "inactive", actor=self.owner)
        jobs.drain()
        self.assertEqual(self.item(batch, self.a).error_code, "family_inactive")

    def test_the_customer_details_sent_are_the_families_primary_payer(self):
        customer = generation.customer_of(self.a)
        self.assertEqual((customer.name, customer.email), ("Alpha Parent", "parent@example.com"))
        self.assertTrue(customer.phone)
        self.assertEqual((customer.bvn, customer.nin), ("", ""))


class ReplacementTests(CollectTestCase):
    """An account that no longer covers the period is retired at the provider and remade: the family never holds two live accounts."""

    def setUp(self):
        super().setUp()
        self.set_policy(reuse_scope="one_term", eligibility_policy="include")
        self.a = self.make_family("Alpha")

    def test_the_old_account_is_closed_at_the_provider_and_the_new_one_replaces_it(self):
        self.run_batch(self.approved())
        (old,) = self.live_accounts(self.a)
        self.into_term_two()
        self.charge_family(self.a, [m.student for m in self.a.members.all()], 40_000, term=self.term2)
        batch = self.run_batch(self.approved(term=self.term2))
        old.refresh_from_db()
        (new,) = self.live_accounts(self.a)
        self.assertEqual((old.status, new.status, batch.status), ("closed", "active", BatchStatus.COMPLETED))
        self.assertNotEqual(new.account_number, old.account_number)
        self.assertEqual(new.scope_term, self.term2)
        from apps.bankconnect.models import SandboxProviderAccount

        self.assertEqual(SandboxProviderAccount.objects.get(reference=old.external_account_ref).status, "closed")  # retired AT the provider
        self.assertEqual(FamilyCollectionAccount.objects.filter(family=self.a).count(), 2)  # history kept
        self.assertEqual(self.item(batch, self.a).eligibility_status, "eligible")
