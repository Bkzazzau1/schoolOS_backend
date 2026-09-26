"""The queue of provider calls: claimed once, leased, retried with a growing delay, and never made inside a transaction that holds a lock."""

from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.bankconnect.providers import base, registry

from .. import jobs, running
from ..constants import LEASE_SECONDS, MAX_ATTEMPTS, JobStatus
from ..models import ProviderJob
from .base import CollectTestCase


class ClaimingTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.make_family("Alpha")
        self.make_family("Bravo")
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        self.batch = batch

    def test_a_job_is_claimed_once_and_the_second_worker_gets_the_next_one(self):
        now = timezone.now()
        first = jobs._claim(now)
        second = jobs._claim(now)
        third = jobs._claim(now)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(third)
        self.assertEqual((first.status, first.attempts), (JobStatus.RUNNING, 1))

    def test_a_claim_lost_to_another_worker_is_not_taken(self):
        now = timezone.now()
        candidate = ProviderJob.objects.order_by("run_after", "created_at").first()
        # another worker takes exactly that job between our reading it and our claiming it
        ProviderJob.objects.filter(pk=candidate.pk).update(status=JobStatus.RUNNING, lease_until=now + timedelta(minutes=5), updated_at=timezone.now())
        claimed = jobs._claim(now)
        self.assertNotEqual(claimed.id, candidate.id)

    def test_a_running_job_with_a_live_lease_is_left_alone_and_one_whose_worker_died_is_taken_again(self):
        now = timezone.now()
        for job in ProviderJob.objects.all():
            ProviderJob.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING, lease_until=now + timedelta(seconds=LEASE_SECONDS))
        self.assertIsNone(jobs._claim(now))
        taken = jobs._claim(now + timedelta(seconds=LEASE_SECONDS + 1))
        self.assertIsNotNone(taken)
        self.assertEqual(taken.attempts, 1)  # counted as another attempt

    def test_a_job_waiting_to_retry_is_not_taken_before_its_time(self):
        ProviderJob.objects.update(status=JobStatus.RETRY, run_after=timezone.now() + timedelta(minutes=10))
        self.assertIsNone(jobs._claim(timezone.now()))
        self.assertIsNotNone(jobs._claim(timezone.now() + timedelta(minutes=11)))

    def test_drain_stops_at_the_limit_and_when_nothing_is_due(self):
        self.assertEqual(jobs.drain(limit=1), 1)
        self.assertEqual(jobs.drain(), 1)
        self.assertEqual(jobs.drain(), 0)

    def test_drain_stops_when_its_time_is_up(self):
        self.assertEqual(jobs.drain(seconds=0), 0)
        self.assertEqual(ProviderJob.objects.filter(status=JobStatus.QUEUED).count(), 2)

    @override_settings(SMART_COLLECTION_INLINE_JOBS=False)
    def test_a_server_with_inline_draining_switched_off_never_makes_a_call_in_a_request(self):
        self.assertEqual(jobs.drain_inline(), 0)
        self.assertEqual(ProviderJob.objects.filter(status=JobStatus.QUEUED).count(), 2)

    @override_settings(SMART_COLLECTION_INLINE_JOBS=True, SMART_COLLECTION_INLINE_SECONDS=30)
    def test_a_server_with_no_worker_drains_a_bounded_slice_itself(self):
        self.assertEqual(jobs.drain_inline(), 2)

    def test_the_command_runs_the_queue_once(self):
        from django.core.management import call_command

        call_command("run_collection_jobs")
        self.assertEqual(ProviderJob.objects.filter(status=JobStatus.SUCCEEDED).count(), 2)


class NoProviderCallInsideATransactionTests(CollectTestCase):
    """The database transaction the code under test opens is closed before a provider is called: a provider is never called while a lock is held."""

    def setUp(self):
        super().setUp()
        self.family = self.make_family("Alpha")

    def depth(self) -> int:
        return len(connection.savepoint_ids)

    def test_generation_calls_the_provider_outside_any_transaction_it_opened(self):
        connector = registry.get_connector("sandbox")
        seen = []
        real = type(connector).provision_family_collection_account

        def watch(self_, secret, request, **kw):
            seen.append(self.depth())
            return real(self_, secret, request, **kw)

        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        baseline = self.depth()
        with mock.patch.object(type(connector), "provision_family_collection_account", watch):
            jobs.drain()
        self.assertEqual(seen, [baseline])

    def test_retiring_calls_the_provider_outside_any_transaction_it_opened(self):
        from .. import lifecycle

        self.run_batch(self.approved())
        (account,) = self.live_accounts(self.family)
        lifecycle.retire(self.owner, account.id, reason="Left")
        connector = registry.get_connector("sandbox")
        seen = []
        real = type(connector).close_collection_account

        def watch(self_, secret, **kw):
            seen.append(self.depth())
            return real(self_, secret, **kw)

        baseline = self.depth()
        with mock.patch.object(type(connector), "close_collection_account", watch):
            jobs.drain()
        self.assertEqual(seen, [baseline])

    def test_the_decision_and_the_queued_call_are_written_together(self):
        from django.db import transaction

        batch = self.approved()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                running.start_processing(self.maker, batch.id)
                raise RuntimeError("the request failed after starting")
        self.assertEqual((ProviderJob.objects.count(), self.refetch(batch).status), (0, "approved"))  # neither happened


class RetryTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Alpha")

    def test_each_attempt_waits_longer_and_the_last_fails_the_job(self):
        from ..constants import BACKOFF_SECONDS

        connector = registry.get_connector("sandbox")
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        waits = []
        clock = timezone.now()
        with mock.patch.object(type(connector), "provision_family_collection_account", side_effect=base.ProviderUnavailable()):
            for attempt in range(1, MAX_ATTEMPTS + 1):
                clock += timedelta(days=1)
                job = jobs.run_next(clock)
                waits.append((job.status, round((job.run_after - timezone.now()).total_seconds() / 60)) if job.status == JobStatus.RETRY else (job.status, None))
        self.assertEqual([w[0] for w in waits], [JobStatus.RETRY] * (MAX_ATTEMPTS - 1) + [JobStatus.FAILED])
        self.assertEqual(len(BACKOFF_SECONDS), MAX_ATTEMPTS - 1)
        self.assertEqual(ProviderJob.objects.get().attempts, MAX_ATTEMPTS)

    def test_one_failing_job_does_not_stop_the_next(self):
        other = self.make_family("Bravo")
        from apps.bankconnect.providers import sandbox

        sandbox.inject_fault(base.ProviderRejected("provider_rejected", "no"), match=self.family.code, times=1)
        batch = self.approved()
        running.start_processing(self.maker, batch.id)
        self.assertEqual(jobs.drain(), 2)
        self.assertEqual(len(self.live_accounts(other)), 1)
