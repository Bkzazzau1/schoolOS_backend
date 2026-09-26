"""The queue of provider calls for debits, and the worker that carries them out.

Mandates & Direct Debit never calls a provider while a database transaction is open. Whatever decides that a call must be made (a batch being started,
a debit that must be asked about) writes a `MandateProviderJob` in the SAME transaction as the decision, so the two can never disagree; a worker makes
the call afterwards, with no lock held.

* a job is claimed with a compare-and-set (so two workers never run the same one), and holds a lease: a worker that dies leaves a job whose lease runs
  out, and it is picked up again - and picking it up again NEVER sends a debit a second time, because the debit first asks the provider about its own
  reference (see execution.py);
* a provider that does not answer is asked again after a growing wait, up to a limit; a debit whose outcome is still unknown after that is left for a
  person, flagged, and never sent again;
* run it as `manage.py run_mandate_jobs` (once, or `--loop` as a worker). Where no worker runs, the server drains a bounded slice itself when a batch is
  started and while its progress is watched (`MANDATES_INLINE_JOBS`).
"""

import logging
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from . import execution
from .constants import BACKOFF_SECONDS, LEASE_SECONDS, MAX_ATTEMPTS, InstructionStatus, JobKind, JobStatus
from .models import MandateDebitInstruction, MandateProviderJob
from .outcomes import DONE, RETRY

logger = logging.getLogger(__name__)
_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RETRY)


def enqueue_debit(item, *, tag: str = "") -> MandateProviderJob | None:
    """Queue sending this debit. Only one debit job is live per instruction, whatever calls this."""
    if MandateProviderJob.objects.filter(instruction=item, kind=JobKind.DEBIT, status__in=_ACTIVE).exists():
        return None
    return MandateProviderJob.objects.get_or_create(
        idempotency_key=f"debit:{item.id}:{item.retry_round}:{tag}",
        defaults={"school": item.school, "kind": JobKind.DEBIT, "instruction": item, "run_after": timezone.now()},
    )[0]


def enqueue_requery(item, *, delay: bool = False) -> MandateProviderJob | None:
    """Queue asking the provider what happened to this debit. Only one is live per instruction."""
    if MandateProviderJob.objects.filter(instruction=item, kind=JobKind.REQUERY, status__in=_ACTIVE).exists():
        return None
    when = timezone.now() + (timedelta(seconds=BACKOFF_SECONDS[0]) if delay else timedelta())
    return MandateProviderJob.objects.create(
        school=item.school, kind=JobKind.REQUERY, instruction=item, run_after=when, idempotency_key=f"requery:{item.id}:{uuid.uuid4().hex[:12]}",
    )


def _claim(now) -> MandateProviderJob | None:
    """Take the next job that is due, or whose worker died. The claim is a compare-and-set on the row as it was read, so two workers can never both
    have it."""
    due = MandateProviderJob.objects.filter(
        Q(status__in=(JobStatus.QUEUED, JobStatus.RETRY), run_after__lte=now) | Q(status=JobStatus.RUNNING, lease_until__lt=now)
    ).order_by("run_after", "created_at")
    for candidate in due[:10]:
        claimed = MandateProviderJob.objects.filter(pk=candidate.pk, status=candidate.status, updated_at=candidate.updated_at).update(
            status=JobStatus.RUNNING, lease_until=now + timedelta(seconds=LEASE_SECONDS), attempts=F("attempts") + 1, updated_at=timezone.now()
        )
        if claimed:
            return MandateProviderJob.objects.select_related("instruction").get(pk=candidate.pk)
    return None


def _finish(job: MandateProviderJob, status: str, *, code: str = "", run_after=None) -> None:
    fields = {"status": status, "last_error_code": code[:40], "lease_until": None, "updated_at": timezone.now()}
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        fields["finished_at"] = timezone.now()
    if run_after is not None:
        fields["run_after"] = run_after
    MandateProviderJob.objects.filter(pk=job.pk).update(**fields)


def _wait(attempts: int):
    return timezone.now() + timedelta(seconds=BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1])


def run_next(now=None) -> MandateProviderJob | None:
    """Carry out the next due job. Returns it, or None when nothing is due."""
    now = now or timezone.now()
    job = _claim(now)
    if job is None:
        return None
    try:
        if job.kind == JobKind.DEBIT:
            outcome = execution.execute_instruction(job.instruction_id, attempt=job.attempts)
        else:
            outcome = execution.requery_instruction(job.instruction_id, attempt=job.attempts)
    except Exception:  # noqa: BLE001 - a bug in SchoolOS must not kill the worker or lose the job
        # Only that it happened is logged: never the text, which could carry a family's details.
        logger.exception("Mandate job %s (%s) failed unexpectedly", job.id, job.kind)
        if job.attempts >= MAX_ATTEMPTS:
            _finish(job, JobStatus.FAILED, code="internal_error")
        else:
            _finish(job, JobStatus.RETRY, code="internal_error", run_after=_wait(job.attempts))
    else:
        if outcome.result == DONE:
            _finish(job, JobStatus.SUCCEEDED)
        elif outcome.result == RETRY and job.attempts < MAX_ATTEMPTS:
            _finish(job, JobStatus.RETRY, code=outcome.code, run_after=_wait(job.attempts))
        else:
            _finish(job, JobStatus.FAILED, code=outcome.code or "provider_unavailable")
    return MandateProviderJob.objects.get(pk=job.pk)


def drain(*, limit: int | None = None, seconds: float | None = None, now_fn=timezone.now) -> int:
    """Run due jobs one after another until none is due, `limit` have run, or `seconds` have passed. Returns how many ran."""
    started, ran = time.monotonic(), 0
    while limit is None or ran < limit:
        if seconds is not None and time.monotonic() - started >= seconds:
            break
        if run_next(now_fn()) is None:
            break
        ran += 1
    return ran


def inline_enabled() -> bool:
    return bool(getattr(settings, "MANDATES_INLINE_JOBS", False))


def drain_inline() -> int:
    """For a server with no worker: drain a bounded slice. Called after a request has finished its own work (never inside a transaction)."""
    if not inline_enabled():
        return 0
    return drain(seconds=float(getattr(settings, "MANDATES_INLINE_SECONDS", 8)))


def unresolved(school=None):
    """Debits whose outcome is still not known after every attempt: the ones a person must look at."""
    rows = MandateDebitInstruction.objects.filter(status=InstructionStatus.UNKNOWN)
    return rows.filter(school=school) if school is not None else rows
