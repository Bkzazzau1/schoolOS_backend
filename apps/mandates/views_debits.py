"""Direct-debit batches (prepare, review, approve, run), the debits themselves, and the overview. Maker and checker are different duties, and the
server checks both again whatever the app offered."""

from django.db.models import Count, Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.academics.models import AcademicSession, AcademicTerm

from . import debit_batches, execution, jobs, serializers
from .constants import LIVE_MANDATE, BatchStatus, ConnectionStatus, DebitOutcome, InstructionStatus, MandateStatus
from .errors import MandateRefused
from .http import body, is_uuid, mandate_errors
from .models import (
    DirectDebitMandate,
    MandateBatchEvent,
    MandateDebitBatch,
    MandateDebitInstruction,
    MandateProviderConnection,
    MandateTransaction,
)
from .permissions import NEED_PREPARE, NEED_VIEW, acting_membership, permissions_of


def _detail(batch, membership) -> dict:
    items = list(MandateDebitInstruction.objects.filter(batch=batch))
    by_eligibility: dict = {}
    for i in items:
        by_eligibility[i.eligibility_status] = by_eligibility.get(i.eligibility_status, 0) + 1
    return {
        "batch": serializers.batch(batch), "permissions": permissions_of(membership),
        "summary": {
            "families": len(items), "byEligibility": by_eligibility, "selected": sum(1 for i in items if i.selected),
            "outstandingMinor": batch.total_outstanding_minor, "proposedMinor": batch.total_amount_minor,
        },
    }


class BatchesView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        rows = MandateDebitBatch.objects.select_related("session", "term", "prepared_by__user", "submitted_by__user", "approved_by__user", "rejected_by__user").filter(school=membership.school)
        if request.query_params.get("status"):
            rows = rows.filter(status=request.query_params["status"])
        return Response({"batches": [serializers.batch(b) for b in rows[:100]], "permissions": permissions_of(membership)})

    @mandate_errors
    def post(self, request, school_id):
        """Start a draft for a session and term, and build its preview. Nothing is sent to any provider."""
        membership = acting_membership(request, school_id, need=NEED_PREPARE)
        d = body(request)
        session = AcademicSession.objects.filter(school=membership.school, id=d.get("sessionId")).first() if is_uuid(d.get("sessionId")) else None
        term = AcademicTerm.objects.filter(session__school=membership.school, id=d.get("termId")).first() if is_uuid(d.get("termId")) else None
        if d.get("termId") and term is None:
            raise MandateRefused("That term was not found.", "term_not_found")
        batch = debit_batches.create_batch(membership, session=session, term=term, title=d.get("title") or "")
        batch = debit_batches.get_batch(membership, batch.id)
        return Response(_detail(batch, membership), status=status.HTTP_201_CREATED)


class BatchDetailView(APIView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        return Response(_detail(debit_batches.get_batch(membership, batch_id), membership))


class BatchItemsView(APIView):
    """The families in a batch: what each owes, what is proposed, whether it is eligible and selected."""

    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        batch = debit_batches.get_batch(membership, batch_id)
        rows = MandateDebitInstruction.objects.select_related("family", "payer__guardian", "mandate").filter(batch=batch)
        q = request.query_params
        if q.get("eligibility"):
            rows = rows.filter(eligibility_status=q["eligibility"])
        if q.get("selected") in ("1", "0"):
            rows = rows.filter(selected=q["selected"] == "1")
        if q.get("status"):
            rows = rows.filter(status=q["status"])
        return Response({"items": [serializers.item(i) for i in rows], "version": batch.version, "snapshotHash": batch.snapshot_hash})


class BatchEventsView(APIView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        batch = debit_batches.get_batch(membership, batch_id)
        rows = MandateBatchEvent.objects.select_related("actor__user").filter(batch=batch).order_by("id")
        return Response({"events": [
            {"id": e.id, "kind": e.kind, "version": e.version, "detail": e.detail, "at": e.at.isoformat(), "actor": e.actor.user.get_full_name() if e.actor_id and e.actor.user_id else ""}
            for e in rows
        ]})


class BatchProgressView(APIView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        jobs.drain_inline()  # where no worker runs, watching progress drives the queue a little
        batch = debit_batches.get_batch(membership, batch_id)
        return Response({"progress": execution.progress(batch), "batch": serializers.batch(batch)})


class BatchFailedView(APIView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        batch = debit_batches.get_batch(membership, batch_id)
        rows = MandateDebitInstruction.objects.select_related("family", "payer__guardian", "mandate").filter(
            batch=batch, status__in=[InstructionStatus.FAILED, InstructionStatus.UNKNOWN]
        )
        return Response({"items": [serializers.item(i) for i in rows]})


def _selection(m, batch_id, d):
    return debit_batches.set_selection(
        m, batch_id, select=d.get("select") or [], deselect=d.get("deselect") or [], select_all_eligible=bool(d.get("selectAllEligible")),
        deselect_all=bool(d.get("deselectAll")), expected_version=d.get("version"),
    )


def _start(m, batch_id, d):
    batch = execution.start(m, batch_id)
    jobs.drain_inline()
    return debit_batches.get_batch(m, batch.id)


BATCH_ACTIONS = {
    "preview": lambda m, b, d: debit_batches.refresh(m, b, expected_version=d.get("version")),
    "selection": _selection,
    "title": lambda m, b, d: debit_batches.rename(m, b, d.get("title") or ""),
    "submit": lambda m, b, d: debit_batches.submit(m, b, expected_hash=str(d.get("snapshotHash") or ""), expected_version=d.get("version")),
    "approve": lambda m, b, d: debit_batches.approve(m, b, expected_hash=str(d.get("snapshotHash") or "")),
    "reject": lambda m, b, d: debit_batches.reject(m, b, reason=d.get("reason") or ""),
    "cancel": lambda m, b, d: debit_batches.cancel(m, b, reason=d.get("reason") or ""),
    "start": _start,
}


class BatchActionView(APIView):
    action = None

    @mandate_errors
    def post(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        d = body(request)
        debit_batches.get_batch(membership, batch_id)  # 404 for another school's batch, before anything else
        batch = BATCH_ACTIONS[self.action](membership, batch_id, d)
        return Response(_detail(debit_batches.get_batch(membership, batch.id), membership))


class BatchRetryView(APIView):
    @mandate_errors
    def post(self, request, school_id, batch_id):
        """Try failed debits again under the same approval, only where nothing approved has changed."""
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        result = execution.retry(membership, batch_id, item_ids=body(request).get("itemIds"))
        jobs.drain_inline()
        batch = debit_batches.get_batch(membership, batch_id)
        return Response({**_detail(batch, membership), "retry": result})


class ItemAmountView(APIView):
    """Lower one family's debit. A figure the ledger does not allow is refused."""

    @mandate_errors
    def post(self, request, school_id, batch_id, item_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        d = body(request)
        batch = debit_batches.set_amount(membership, batch_id, item_id, amount_minor=d.get("amountMinor"), reason=d.get("reason") or "", expected_version=d.get("version"))
        return Response(_detail(batch, membership))


# -- transactions and the overview ---------------------------------------------------------------------


class TransactionsView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        rows = MandateTransaction.objects.select_related("family", "mandate", "provider_connection").filter(school=membership.school)
        q = request.query_params
        if q.get("status"):
            rows = rows.filter(status=q["status"])
        if q.get("provider"):
            rows = rows.filter(provider=q["provider"])
        if q.get("family") and is_uuid(q["family"]):
            rows = rows.filter(family_id=q["family"])
        return Response({"transactions": [serializers.transaction_row(t) for t in rows[:200]]})


class TransactionCheckView(APIView):
    """Ask the provider again what happened to one debit."""

    @mandate_errors
    def post(self, request, school_id, transaction_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        from .permissions import can_approve, can_prepare

        if not (can_prepare(membership) or can_approve(membership)):
            raise MandateRefused("Only someone who prepares or approves direct-debit batches can check a debit.", "not_operator")
        tx = MandateTransaction.objects.select_related("family", "mandate", "provider_connection", "instruction").filter(school=membership.school, id=transaction_id).first()
        if tx is None:
            raise MandateRefused("That debit was not found.", "not_found")
        outcome = execution.requery_instruction(tx.instruction_id, force=True)
        tx.refresh_from_db()
        return Response({"transaction": serializers.transaction_row(tx), "outcome": outcome.result})


class OverviewView(APIView):
    """Mandates & Direct Debit at a glance: providers (there is no "active" one), mandates by state, batches waiting, debits that need a person."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        school = membership.school
        mandates = DirectDebitMandate.objects.filter(school=school)
        by_provider = {row["provider"]: row["n"] for row in mandates.filter(status=MandateStatus.ACTIVE).values("provider").annotate(n=Count("id"))}
        conns = [c for c in MandateProviderConnection.objects.filter(school=school)]
        transactions = MandateTransaction.objects.filter(school=school, status=DebitOutcome.SUCCESS).aggregate(total=Sum("amount_minor"), n=Count("id"))
        batches = MandateDebitBatch.objects.filter(school=school)
        return Response({
            "providers": [{**serializers.connection(c), "activeMandates": by_provider.get(c.provider, 0)} for c in conns if c.status != ConnectionStatus.REVOKED],
            "mandates": {
                "total": mandates.count(), "live": mandates.filter(status__in=LIVE_MANDATE).count(), "active": mandates.filter(status=MandateStatus.ACTIVE).count(),
                "waitingForPayer": mandates.filter(status__in=[MandateStatus.PENDING_CONSENT, MandateStatus.PENDING_ACTIVATION]).count(),
                "settingUp": mandates.filter(status__in=[MandateStatus.ACTIVATING, MandateStatus.PENDING_PROVIDER_SETUP]).count(),
                "failed": mandates.filter(status=MandateStatus.FAILED).count(),
            },
            "batches": {
                "waitingForApproval": batches.filter(status=BatchStatus.PENDING_APPROVAL).count(), "debiting": batches.filter(status=BatchStatus.PROCESSING).count(),
                "withFailures": batches.filter(status__in=[BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED]).count(),
            },
            "debits": {
                "collectedMinor": transactions["total"] or 0, "count": transactions["n"],
                "unknown": MandateDebitInstruction.objects.filter(school=school, status=InstructionStatus.UNKNOWN).count(),
                "failed": MandateDebitInstruction.objects.filter(school=school, status=InstructionStatus.FAILED).count(),
            },
            "permissions": permissions_of(membership),
        })
