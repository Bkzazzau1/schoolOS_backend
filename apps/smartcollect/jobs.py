"""The queue of provider calls, and the worker that carries them out.

Smart Money Collection never calls a provider while a database transaction is open. Whatever decides that a call must be made (an
approved batch being started, a family settling under a "close it" policy) writes a `ProviderJob` in the SAME transaction as the decision,
so the two can never disagree; a worker makes the call afterwards, with no lock held.

* a job is claimed with a compare-and-set (so two workers never run the same one), and holds a lease: a worker that dies leaves a job whose
  lease runs out, and it is picked up again;
* every job is safe to repeat - the connectors look before they create, and account generation is idempotent by key - so "picked up
  again" and "tried again" never make a second account;
* a provider that does not answer is tried again after a growing wait, up to a limit, and then the item is failed for a person to retry;
* run it as `manage.py run_collection_jobs` (once, or `--loop` as a worker). Where no worker runs, the server drains a bounded slice itself
  when a batch is started and while its progress is watched (`SMART_COLLECTION_INLINE_JOBS`).
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from . import generation, lifecycle
from .constants import BACKOFF_SECONDS, LEASE_SECONDS, MAX_ATTEMPTS, JobKind, JobStatus
from .models import ProviderJob
from .outcomes import DONE, RETRY

logger = logging.getLogger(__name__)


def enqueue_provision(item) -> ProviderJob:
    job, _ = ProviderJob.objects.get_or_create(
        idempotency_key=f"provision:{item.id}:{item.retry_round}",
        defaults={"school": item.school, "kind": JobKind.PROVISION, "item": item, "run_after": timezone.now()},
    )
    return job


def enqueue_retire(account, reason: str = "") -> ProviderJob:
    job, created = ProviderJob.objects.get_or_create(
        idempotency_key=f"retire:{account.id}",
        defaults={"school": account.school, "kind": JobKind.RETIRE, "account": account, "run_after": timezone.now()},
    )
    if not created and job.status in (JobStatus.FAILED, JobStatus.SUCCEEDED):
        # Retiring was asked for again (after a failure, or an account brought back and closed once more).
        ProviderJob.objects.filter(pk=job.pk).update(status=JobStatus.QUEUED, run_after=timezone.now(), attempts=0, last_error_code="", finished_at=None)
    return job


def _claim(now) -> ProviderJob | None:
    """Take the next job that is due, or whose worker died. The claim is a compare-and-set on the row as it was read, so two workers can
    never both have it."""
    due = ProviderJob.objects.filter(
        Q(status__in=(JobStatus.QUEUED, JobStatus.RETRY), run_after__lte=now) | Q(status=JobStatus.RUNNING, lease_until__lt=now)
    ).order_by("run_after", "created_at")
    for candidate in due[:10]:
        claimed = ProviderJob.objects.filter(pk=candidate.pk, status=candidate.status, updated_at=candidate.updated_at).update(
            status=JobStatus.RUNNING, lease_until=now + timedelta(seconds=LEASE_SECONDS), attempts=F("attempts") + 1, updated_at=timezone.now()
        )
        if claimed:
            return ProviderJob.objects.select_related("item", "account").get(pk=candidate.pk)
    return None


def _finish(job: ProviderJob, status: str, *, code: str = "", run_after=None) -> None:
    fields = {"status": status, "last_error_code": code[:40], "lease_until": None, "updated_at": timezone.now()}
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        fields["finished_at"] = timezone.now()
    if run_after is not None:
        fields["run_after"] = run_after
    ProviderJob.objects.filter(pk=job.pk).update(**fields)


def run_next(now=None) -> ProviderJob | None:
    """Carry out the next due job. Returns it, or None when nothing is due."""
    now = now or timezone.now()
    job = _claim(now)
    if job is None:
        return None
    try:
        if job.kind == JobKind.PROVISION:
            outcome = generation.execute_item(job.item_id, attempt=job.attempts)
        else:
            outcome = lifecycle.execute_retire(job.account_id, attempt=job.attempts)
    except Exception:  # noqa: BLE001 - a bug in SchoolOS must not kill the worker or lose the job
        # Only that it happened is logged: never the text, which could carry a family's details.
        logger.exception("Collection job %s (%s) failed unexpectedly", job.id, job.kind)
        if job.kind == JobKind.PROVISION:
            generation.record_unexpected_failure(job.item_id, attempt=job.attempts)
        if job.attempts >= MAX_ATTEMPTS:
            _finish(job, JobStatus.FAILED, code="internal_error")
        else:
            _finish(job, JobStatus.RETRY, code="internal_error", run_after=timezone.now() + timedelta(seconds=BACKOFF_SECONDS[min(job.attempts, len(BACKOFF_SECONDS)) - 1]))
    else:
        if outcome.result == DONE:
            _finish(job, JobStatus.SUCCEEDED)
        elif outcome.result == RETRY and job.attempts < MAX_ATTEMPTS:
            _finish(job, JobStatus.RETRY, code=outcome.code, run_after=timezone.now() + timedelta(seconds=BACKOFF_SECONDS[min(job.attempts, len(BACKOFF_SECONDS)) - 1]))
        else:
            _finish(job, JobStatus.FAILED, code=outcome.code or "provider_unavailable")
    return ProviderJob.objects.get(pk=job.pk)


def drain(*, limit: int | None = None, seconds: float | None = None, now_fn=timezone.now) -> int:
    """Run due jobs one after another until none is due, `limit` have run, or `seconds` have passed. Returns how many ran."""
    started = time.monotonic()
    ran = 0
    while limit is None or ran < limit:
        if seconds is not None and time.monotonic() - started >= seconds:
            break
        if run_next(now_fn()) is None:
            break
        ran += 1
    return ran


def inline_enabled() -> bool:
    return bool(getattr(settings, "SMART_COLLECTION_INLINE_JOBS", False))


def drain_inline() -> int:
    """For a server with no worker: drain a bounded slice. Called after a request has finished its own work (never inside a transaction)."""
    if not inline_enabled():
        return 0
    return drain(seconds=float(getattr(settings, "SMART_COLLECTION_INLINE_SECONDS", 8)))


def pending_count(*, batch=None) -> int:
    query = ProviderJob.objects.filter(status__in=(JobStatus.QUEUED, JobStatus.RETRY, JobStatus.RUNNING))
    if batch is not None:
        query = query.filter(item__batch=batch)
    return query.count()
