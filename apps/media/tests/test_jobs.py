"""Verifying an upload and building its thumbnail run from the durable job queue, not inline in the view (that is
only ever a convenience `drain_inline` calls after the request is otherwise done) - proven here by turning that
convenience off and driving the queue by hand, the way `manage.py run_media_jobs` would."""

from django.test import override_settings
from django.utils import timezone

from apps.media import jobs
from apps.media.constants import JobStatus, Status
from apps.media.models import MediaJob
from apps.media.storage import MediaStorageError, use_storage

from .base import MediaTestCase, make_png
from .test_storage import FakeRemoteStorage


@override_settings(MEDIA_INLINE_JOBS=False)
class JobQueueTests(MediaTestCase):
    def _uploaded(self, data=None):
        data = data or make_png()
        created = self.initiate(owner_type="gallery_media_album", owner_id=self.album_id, category="gallery_photo", data=data, who=self.teacher)
        asset_id = created.json()["asset"]["id"]
        self.put_body(f"assets/{asset_id}/upload/", data, "image/png", who=self.teacher)
        completed = self.post(f"assets/{asset_id}/complete/", who=self.teacher)
        self.assertEqual(completed.json()["asset"]["status"], Status.UPLOADED)  # not verified yet: no inline draining
        return asset_id

    def test_with_no_worker_and_no_inline_draining_the_asset_sits_uploaded_until_the_queue_is_run(self):
        asset_id = self._uploaded()
        job = MediaJob.objects.get(asset_id=asset_id, kind="verify")
        self.assertEqual(job.status, JobStatus.QUEUED)

    def test_running_the_queue_verifies_then_thumbnails_then_makes_it_available(self):
        asset_id = self._uploaded()
        jobs.drain()
        after_verify = self.get(f"assets/{asset_id}/", who=self.teacher).json()["asset"]
        self.assertIn(after_verify["status"], (Status.AVAILABLE,))  # drain() runs every due job in one call, verify then thumbnail
        self.assertTrue(after_verify["hasThumbnail"])

    def test_a_duplicate_enqueue_of_the_same_work_is_a_no_op_not_a_second_job(self):
        asset_id = self._uploaded()
        from apps.media.models import MediaAsset

        asset = MediaAsset.objects.get(id=asset_id)
        jobs.enqueue(asset, "verify")
        jobs.enqueue(asset, "verify")
        self.assertEqual(MediaJob.objects.filter(asset_id=asset_id, kind="verify").count(), 1)

    def test_a_job_whose_lease_expired_can_be_reclaimed_by_another_worker(self):
        asset_id = self._uploaded()
        job = MediaJob.objects.get(asset_id=asset_id, kind="verify")
        job.status, job.lease_until, job.attempts = JobStatus.RUNNING, timezone.now() - timezone.timedelta(seconds=1), 1
        job.save()
        jobs.run_next()
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatus.SUCCEEDED)

    def test_a_transient_storage_error_retries_with_backoff_rather_than_failing_the_asset_outright(self):
        asset_id = self._uploaded()
        from apps.media.models import MediaAsset

        class FlakyOnce(FakeRemoteStorage):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def read_bytes(self, storage_key, *, max_bytes):
                self.calls += 1
                if self.calls == 1:
                    raise MediaStorageError("storage_unavailable", "not reachable yet")
                return super().read_bytes(storage_key, max_bytes=max_bytes)

        asset = MediaAsset.objects.get(id=asset_id)
        flaky = FlakyOnce()
        flaky.objects[asset.storage_key] = open(_local_path(asset), "rb").read()
        with use_storage(flaky):
            job = jobs.run_next()
            self.assertEqual(job.status, JobStatus.RETRY)
            self.assertGreater(job.run_after, timezone.now())
            asset.refresh_from_db()
            self.assertEqual(asset.status, Status.UPLOADED)  # not failed: a transient error is retried, not given up on
            job.run_after = timezone.now()
            job.save(update_fields=["run_after"])
            jobs.run_next()
        asset.refresh_from_db()
        self.assertEqual(asset.status, Status.VERIFIED)

    def test_a_permanent_failure_like_a_checksum_mismatch_marks_the_asset_failed_and_the_job_succeeded(self):
        asset_id = self._uploaded()
        from apps.media.models import MediaAsset

        MediaAsset.objects.filter(id=asset_id).update(checksum_sha256="0" * 64)
        jobs.drain()
        asset = MediaAsset.objects.get(id=asset_id)
        self.assertEqual(asset.status, Status.FAILED)
        self.assertEqual(asset.failure_code, "checksum_mismatch")
        job = MediaJob.objects.get(asset_id=asset_id, kind="verify")
        self.assertEqual(job.status, JobStatus.SUCCEEDED)  # the job did its work correctly; it is the file that failed

    def test_a_retired_assets_bytes_are_purged_by_its_own_job(self):
        asset_id = self._uploaded()
        jobs.drain()
        from apps.media.models import MediaAsset
        from apps.media.storage import get_storage

        asset = MediaAsset.objects.get(id=asset_id)
        storage = get_storage()
        self.assertIsNotNone(storage.verify_existence(asset.storage_key))
        self.post(f"assets/{asset_id}/retire/", who=self.teacher)
        jobs.drain()
        self.assertIsNone(storage.verify_existence(asset.storage_key))


def _local_path(asset):
    from apps.media.storage import get_storage

    return get_storage().absolute_path(asset.storage_key)
