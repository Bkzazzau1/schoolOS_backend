"""Durable background work on an asset: verifying an upload, building a thumbnail, purging a retired file's bytes.

The same shape as `apps/mandates/jobs.py`: a job row is written in the same transaction as the decision that
requires it; the work itself - reading from storage, hashing, encoding an image - always runs outside any
transaction and with no row lock held, via `run_next`/`drain`, driven by `manage.py run_media_jobs` (or, where no
worker runs, `drain_inline` after a request that just enqueued something).
"""

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import audit
from .constants import BACKOFF_SECONDS, LEASE_SECONDS, MAX_ATTEMPTS, JobKind, JobStatus, Status
from .models import MediaJob

logger = logging.getLogger(__name__)


def enqueue(asset, kind: str, *, tag: str = "") -> MediaJob:
    """Called inside the same transaction as whatever made this work necessary. A repeat enqueue of the same
    (asset, kind, tag) is a no-op - the unique idempotency key makes a duplicate simply not insert a second row."""
    key = f"{asset.id}:{kind}:{tag}" if tag else f"{asset.id}:{kind}"
    job, _ = MediaJob.objects.get_or_create(
        idempotency_key=key,
        defaults={"school": asset.school, "asset": asset, "kind": kind, "run_after": timezone.now()},
    )
    return job


def _wait(attempts: int) -> "timezone.datetime":
    index = min(attempts - 1, len(BACKOFF_SECONDS) - 1)
    return timezone.now() + timedelta(seconds=BACKOFF_SECONDS[max(index, 0)])


def _claim(now) -> MediaJob | None:
    due = MediaJob.objects.filter(
        Q(status__in=(JobStatus.QUEUED, JobStatus.RETRY), run_after__lte=now)
        | Q(status=JobStatus.RUNNING, lease_until__lt=now)
    ).order_by("run_after", "created_at")
    for candidate in due[:10]:
        claimed = MediaJob.objects.filter(pk=candidate.pk, status=candidate.status, updated_at=candidate.updated_at).update(
            status=JobStatus.RUNNING,
            lease_until=now + timedelta(seconds=LEASE_SECONDS),
            attempts=candidate.attempts + 1,
            updated_at=timezone.now(),
        )
        if claimed:
            return MediaJob.objects.select_related("asset", "school").get(pk=candidate.pk)
    return None


def _finish(job: MediaJob, status: str, *, code: str = "", run_after=None) -> None:
    job.status = status
    job.last_error_code = code
    if run_after is not None:
        job.run_after = run_after
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        job.finished_at = timezone.now()
        job.lease_until = None
    job.save(update_fields=["status", "last_error_code", "run_after", "finished_at", "lease_until", "updated_at"])


def run_next(now=None) -> MediaJob | None:
    from . import uploads  # deferred: uploads imports jobs to enqueue, so the reverse import stays lazy

    job = _claim(now or timezone.now())
    if job is None:
        return None
    try:
        if job.kind == JobKind.VERIFY:
            uploads.run_verify(job.asset)
        elif job.kind == JobKind.THUMBNAIL:
            uploads.run_thumbnail(job.asset)
        elif job.kind == JobKind.PURGE:
            uploads.run_purge(job.asset)
    except Exception as error:  # noqa: BLE001 - a job must never crash the worker loop; it retries or fails instead
        logger.exception("media job %s failed", job.kind)
        code = getattr(error, "code", "") or "unexpected_error"
        if job.attempts < MAX_ATTEMPTS:
            _finish(job, JobStatus.RETRY, code=code, run_after=_wait(job.attempts))
        else:
            _finish(job, JobStatus.FAILED, code=code)
            asset = job.asset
            if asset.status not in (Status.AVAILABLE, Status.RETIRED):
                asset.status, asset.failure_code = Status.FAILED, code
                asset.save(update_fields=["status", "failure_code", "updated_at"])
            audit.record(job.school, "media_job_failed", obj=job, kind=job.kind, code=code)
    else:
        _finish(job, JobStatus.SUCCEEDED)
    return MediaJob.objects.get(pk=job.pk)


def drain(*, limit: int | None = None, seconds: float | None = None) -> int:
    started, done = timezone.now(), 0
    while True:
        if limit is not None and done >= limit:
            break
        if seconds is not None and (timezone.now() - started).total_seconds() >= seconds:
            break
        if run_next() is None:
            break
        done += 1
    return done


def inline_enabled() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "MEDIA_INLINE_JOBS", False))


def drain_inline() -> int:
    """Called after a request has finished its own work, never inside a transaction."""
    from django.conf import settings

    if not inline_enabled():
        return 0
    return drain(seconds=float(getattr(settings, "MEDIA_INLINE_SECONDS", 8)))
