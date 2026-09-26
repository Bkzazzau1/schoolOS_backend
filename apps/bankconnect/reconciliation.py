"""Running the matching engine over new payments and recording what it decided.

`ingest` only stores a payment; it leaves `engine_version` empty. This module finds every credit the
engine has not yet seen and evaluates it, so a crash between the two can never leave a payment
un-reconciled forever - the next run picks it up. Each payment is decided in its own database
transaction, and the engine's verdict, its reasons and (when it matches) the allocation are written
together with a decision row saying the engine did it.

A payment is auto-allocated only when the engine is `matched`. A payment that looks like one
already received is held as `duplicate`, never dropped and never allocated: only a person can say
it is a second real payment.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.money import format_money  # noqa: F401 - re-exported: other code imports it from here
from apps.notifications.services import notify_many

from . import matching
from .constants import Direction, ReconStatus
from .models import BankTransaction, ReconciliationDecision, TransactionAllocation
from .permissions import collections_recipients

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW = timedelta(hours=24)
NOTIFY_KIND_RECEIVED = "bank_payment_received"
NOTIFY_KIND_REVIEW = "bank_payment_review"
SANDBOX_PREFIX = "[Sandbox test data] "


def _norm(text: str) -> str:
    return " ".join(matching.tokens(text))


def _sender_tail(mask: str) -> str:
    digits = "".join(ch for ch in str(mask or "") if ch.isdigit())
    return digits if len(digits) == 4 else ""


def find_original(row: BankTransaction) -> BankTransaction | None:
    """An earlier payment with the same sender, amount and narration within a day - the signature of
    a transfer that was sent twice, or reported twice. Payments with no sender name are never
    called duplicates: an amount alone is far too common."""
    if not _norm(row.sender_name):
        return None
    rivals = BankTransaction.objects.filter(
        school=row.school, direction=Direction.CREDIT, is_sandbox=row.is_sandbox,
        amount_minor=row.amount_minor, currency=row.currency,
        transaction_date__range=(row.transaction_date - DUPLICATE_WINDOW, row.transaction_date + DUPLICATE_WINDOW),
    ).exclude(id=row.id)
    for other in rivals.order_by("transaction_date", "created_at"):
        earlier = (other.transaction_date, other.created_at) < (row.transaction_date, row.created_at)
        if earlier and _norm(other.sender_name) == _norm(row.sender_name) and _norm(other.narration) == _norm(row.narration):
            return other.duplicate_of or other
    return None


def _verdict_for(row: BankTransaction, directory: matching.Directory):
    verdict = matching.decide(
        directory.candidates(
            narration=row.narration, reference=row.transaction_reference,
            sender_name=row.sender_name, sender_tail=_sender_tail(row.sender_account_mask),
        )
    )
    original = find_original(row)
    if original is None:
        return verdict, None
    when = timezone.localtime(original.transaction_date).strftime("%d %b %Y %H:%M")
    note = (
        f"Same sender, amount and narration as a payment received on {when}, within 24 hours. It is held "
        "for a person because it may be the same money sent or reported twice."
    )
    return matching.Verdict(ReconStatus.DUPLICATE, verdict.confidence, verdict.candidates, [note, *verdict.notes]), original


def _apply(row: BankTransaction, verdict, original) -> BankTransaction | None:
    """Write one verdict. Returns the updated payment, or None if someone else got there first."""
    with transaction.atomic():
        fresh = BankTransaction.objects.select_for_update().select_related("connection").get(id=row.id)
        if fresh.engine_version or fresh.reconciliation_status != ReconStatus.UNMATCHED:
            return None
        fresh.reconciliation_status = verdict.status
        fresh.reconciliation_confidence = verdict.confidence
        fresh.match_reasons = verdict.reasons()
        fresh.engine_version = matching.ENGINE_VERSION
        fresh.duplicate_of = original
        fresh.save()
        matched = verdict.status == ReconStatus.MATCHED
        decision = ReconciliationDecision.objects.create(
            school=fresh.school, transaction=fresh, action="auto_match" if matched else "engine_review",
            before={"status": ReconStatus.UNMATCHED},
            after={"status": str(verdict.status), "confidence": verdict.confidence, "engine": matching.ENGINE_VERSION},
        )
        if matched:
            TransactionAllocation.objects.create(
                school=fresh.school, transaction=fresh, student_id=verdict.top.student_id,
                purpose=fresh.connection.purpose, amount_minor=fresh.amount_minor, source="auto", decision=decision,
            )
    return fresh


@dataclass
class Summary:
    matched: list = field(default_factory=list)
    needs_review: list = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return len(self.matched) + len(self.needs_review)


def reconcile_pending(school, *, limit: int = 500) -> Summary:
    """Evaluate every credit the engine has not seen, oldest first, then tell the finance people."""
    rows = list(
        BankTransaction.objects.filter(
            school=school, direction=Direction.CREDIT, engine_version="", reconciliation_status=ReconStatus.UNMATCHED
        ).order_by("transaction_date", "created_at")[:limit]
    )
    summaries = {False: Summary(), True: Summary()}
    if not rows:
        return summaries[False]
    directory = matching.Directory(school)
    for row in rows:
        verdict, original = _verdict_for(row, directory)
        done = _apply(row, verdict, original)
        if done is None:
            continue
        summary = summaries[done.is_sandbox]
        # Anything the engine did not match - possible, ambiguous, duplicate or unmatched - is for a person.
        (summary.matched if done.reconciliation_status == ReconStatus.MATCHED else summary.needs_review).append(done)
    _notify(school, summaries)
    real, sandbox = summaries[False], summaries[True]
    return Summary(real.matched + sandbox.matched, real.needs_review + sandbox.needs_review)


def reconcile_quietly(school) -> None:
    """For callers whose own job must not fail because the engine did (a webhook, a sync): whatever
    is left is picked up by the next run."""
    try:
        reconcile_pending(school)
    except Exception:  # noqa: BLE001 - deliberately broad; logged without payment details
        logger.exception("Bank reconciliation failed for school %s", school.id)


def _notify(school, summaries: dict) -> None:
    recipients = collections_recipients(school)
    if not recipients:
        return
    for sandbox, summary in summaries.items():
        prefix = SANDBOX_PREFIX if sandbox else ""
        if summary.matched:
            total = sum(t.amount_minor for t in summary.matched)
            if len(summary.matched) == 1:
                t = summary.matched[0]
                student = t.allocations.select_related("student").first().student
                title = "Payment received"
                message = (
                    f"{format_money(t.amount_minor, t.currency)} from {t.sender_name or 'an unnamed sender'} was matched to "
                    f"{student.full_name} ({student.student_code}) for {t.connection.get_purpose_display().lower()}."
                )
            else:
                title = f"{len(summary.matched)} payments received"
                message = f"{len(summary.matched)} payments totalling {format_money(total)} were matched to students."
            notify_many(
                recipients, NOTIFY_KIND_RECEIVED, prefix + title, prefix + message,
                {"count": len(summary.matched), "transactionIds": [str(t.id) for t in summary.matched][:20]},
            )
        if summary.needs_review:
            count = len(summary.needs_review)
            notify_many(
                recipients, NOTIFY_KIND_REVIEW, prefix + ("A payment needs review" if count == 1 else f"{count} payments need review"),
                prefix + (
                    "A payment could not be matched to a student with confidence and needs a person to look at it."
                    if count == 1
                    else f"{count} payments could not be matched to a student with confidence and need a person to look at them."
                ),
                {"count": count, "transactionIds": [str(t.id) for t in summary.needs_review][:20]},
            )
