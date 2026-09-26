"""Smart Money Collection's API: the school's collection policy and its overrides, provider switches, and collection batches (prepare, review,
export, submit, approve or reject, generate, retry). Everything is looked up inside the acting person's own school - another school's object
is simply not found - and the authority each change needs is checked by the service that makes it, not only here.

No response here carries a credential or an identity number."""

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status as http
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.bankconnect import summary as collections_summary
from apps.bankconnect.constants import ConnectionStatus
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.permissions import acting_membership, permissions_of
from apps.bankconnect.serializers import serialize_connection
from apps.academics.models import AcademicSession, AcademicTerm
from apps.receivables import periods
from apps.receivables.errors import Refused
from apps.receivables.models import LIVE_STATUSES, Family, FamilyCollectionAccount
from django.db.models import Count, Q

from . import batches, exports, identity, jobs, policy, switching, running
from .constants import OPEN_STATUSES, BatchStatus, GenerationStatus
from .errors import CollectionRefused
from .models import CollectionBatchEvent, CollectionGenerationBatch, CollectionGenerationBatchItem, ProviderSwitch
from .serializers import (
    serialize_batch,
    serialize_event,
    serialize_item,
    serialize_override,
    serialize_policy,
    serialize_resolved,
    serialize_switch,
)

MAX_PAGE = 200
#: Refusals that mean "what you were looking at is out of date": the app should reload, not retry.
CONFLICT_CODES = {"stale_preview", "stale_approval", "batch_changed", "approval_mismatch", "switch_blocked"}
FORBIDDEN_CODES = {"not_preparer", "not_approver", "not_policy_manager", "not_provider_manager", "maker_cannot_approve"}


class CollectView(APIView):
    """Base of every view here. A refusal is a normal outcome with a stable `code` and words a person can act on, never a 500."""

    def handle_exception(self, exc):
        if isinstance(exc, CollectionRefused):
            code = http.HTTP_409_CONFLICT if exc.code in CONFLICT_CODES else (http.HTTP_403_FORBIDDEN if exc.code in FORBIDDEN_CODES else http.HTTP_400_BAD_REQUEST)
            return Response({"code": exc.code, "message": exc.message, **exc.extra}, status=code)
        if isinstance(exc, Refused):
            return Response({"code": exc.code, "message": exc.message}, status=http.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def paging(request, default: int = 50) -> tuple[int, int]:
    try:
        return min(max(int(request.query_params.get("limit", default)), 1), MAX_PAGE), max(int(request.query_params.get("offset", 0)), 0)
    except ValueError:
        raise CollectionRefused("limit and offset must be whole numbers.", "invalid_paging")


def _truthy(value) -> bool:
    return str(value).lower() in ("1", "true", "yes")


def _session(membership, value):
    session = AcademicSession.objects.filter(school=membership.school, id=str(value or "")).first() if _uuid(value) else None
    if session is None:
        raise CollectionRefused("Choose one of the school's sessions.", "session_required")
    return session


def _term(membership, value, session=None):
    if not value:
        return None
    term = AcademicTerm.objects.select_related("session").filter(session__school=membership.school, id=str(value)).first() if _uuid(value) else None
    if term is None or (session is not None and term.session_id != session.id):
        raise CollectionRefused("That term is not one of this session's terms.", "term_not_in_session")
    return term


def _family(membership, value) -> Family:
    family = Family.objects.filter(school=membership.school, id=str(value)).first() if _uuid(value) else None
    if family is None:
        raise NotFound("That family was not found.")
    return family


def _uuid(value) -> bool:
    from uuid import UUID

    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


class PeriodsView(CollectView):
    """GET the school's sessions and their terms (newest first) and which are current, for choosing what a batch or an override is for."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        current_session, current_term = periods.current_period(membership.school)
        rows = AcademicSession.objects.filter(school=membership.school).prefetch_related("terms").order_by("-starts_on")
        return Response({
            "sessions": [
                {
                    "id": str(s.id), "name": s.name, "status": s.status, "startsOn": s.starts_on.isoformat(), "endsOn": s.ends_on.isoformat(),
                    "terms": [
                        {"id": str(t.id), "name": t.name, "sequence": t.sequence, "status": t.status, "startsOn": t.starts_on.isoformat(), "endsOn": t.ends_on.isoformat()}
                        for t in sorted(s.terms.all(), key=lambda t: t.sequence)
                    ],
                }
                for s in rows
            ],
            "current": {"sessionId": str(current_session.id) if current_session else None, "termId": str(current_term.id) if current_term else None},
        })


# -- the policy ---------------------------------------------------------------------------------------


class PolicyView(CollectView):
    """GET the school's default policy (and the choices for every field); PATCH to change it."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response({"policy": serialize_policy(policy.school_policy(membership.school)), "permissions": permissions_of(membership)})

    def patch(self, request, school_id):
        membership = acting_membership(request, school_id)
        row = policy.update_school_policy(membership, body(request).get("values"))
        return Response({"policy": serialize_policy(row)})


class PolicyEffectiveView(CollectView):
    """GET the policy that applies to a session, term, batch or family, with where each field came from."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        q = request.query_params
        term = _term(membership, q.get("term"))
        session = _session(membership, q.get("session")) if q.get("session") else (term.session if term else None)
        family = _family(membership, q.get("family")) if q.get("family") else None
        batch = batches.get_batch(membership, q.get("batch")) if q.get("batch") else None
        return Response({"effective": serialize_resolved(policy.resolve(membership.school, session=session, term=term, batch=batch, family=family))})


class PolicyOverridesView(CollectView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        rows = policy.overrides_of(membership.school, scope=request.query_params.get("scope") or None, include_history=_truthy(request.query_params.get("history")))
        limit, offset = paging(request, default=100)
        rows = rows.select_related("batch")[offset: offset + limit]
        return Response({"overrides": [serialize_override(o) for o in rows]})

    def post(self, request, school_id):
        """POST {scope, sessionId|termId|batchId|familyId, values, reason, expiryKind, expiresOn}."""
        membership = acting_membership(request, school_id)
        data = body(request)
        term = _term(membership, data.get("termId")) if data.get("scope") == "term" else None
        session = _session(membership, data.get("sessionId")) if data.get("scope") == "session" else None
        family = _family(membership, data.get("familyId")) if data.get("scope") == "family" else None
        batch = batches.get_batch(membership, data.get("batchId")) if data.get("scope") == "batch" else None
        expiry_term = _term(membership, data.get("expiryTermId")) if data.get("expiryTermId") else None
        expiry_session = _session(membership, data.get("expirySessionId")) if data.get("expirySessionId") else None
        created = policy.set_override(
            membership, scope=data.get("scope"), session=session, term=term, batch=batch, family=family, values=data.get("values"),
            reason=data.get("reason"), expiry_kind=data.get("expiryKind"), expires_on=data.get("expiresOn"), expiry_term=expiry_term,
            expiry_session=expiry_session,
        )
        created = policy.overrides_of(membership.school, include_history=True).get(pk=created.pk)
        return Response({"override": serialize_override(created)}, status=http.HTTP_201_CREATED)


class PolicyOverrideRemoveView(CollectView):
    def post(self, request, school_id, override_id):
        membership = acting_membership(request, school_id)
        removed = policy.remove_override(membership, override_id, reason=body(request).get("reason"))
        return Response({"override": serialize_override(policy.overrides_of(membership.school, include_history=True).get(pk=removed.pk))})


# -- switching provider -------------------------------------------------------------------------------


class SwitchesView(CollectView):
    """GET the open switch (refreshed) and the history; POST {toConnectionId, scheduledFor, note} to schedule one."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        current = switching.refresh(membership.school)
        history = ProviderSwitch.objects.select_related("from_connection", "to_connection", "created_by__user", "applied_by__user").filter(school=membership.school)[:20]
        return Response({
            "open": serialize_switch(current) if current else None, "history": [serialize_switch(s) for s in history if not current or s.id != current.id],
            "permissions": permissions_of(membership),
        })

    def post(self, request, school_id):
        membership = acting_membership(request, school_id)
        data = body(request)
        created = switching.schedule(membership, to_connection_id=data.get("toConnectionId"), scheduled_for=data.get("scheduledFor"), note=data.get("note"))
        return Response({"switch": serialize_switch(created)}, status=http.HTTP_201_CREATED)


class SwitchDetailView(CollectView):
    def get(self, request, school_id, switch_id):
        membership = acting_membership(request, school_id)
        switch = switching.get_switch(membership, switch_id)
        if switch.status in ("scheduled", "ready_to_switch"):
            switch = switching._advance(switch)
        return Response({"switch": serialize_switch(switch), "review": switch.review})


class SwitchActionView(CollectView):
    action = None

    def post(self, request, school_id, switch_id):
        membership = acting_membership(request, school_id)
        if self.action == "apply":
            done = switching.apply(membership, switch_id)
        else:
            done = switching.cancel(membership, switch_id, reason=body(request).get("reason"))
        return Response({"switch": serialize_switch(done)})


# -- batches ------------------------------------------------------------------------------------------


def _batch_response(membership, batch_id, status=http.HTTP_200_OK, **extra):
    fresh = batches.get_batch(membership, batch_id)
    return Response({"batch": serialize_batch(fresh, membership, detail=True), **extra}, status=status)


class BatchesView(CollectView):
    """GET the school's batches (?status=, ?mine=1, ?awaiting=1 for those waiting for this person's approval); POST {sessionId, termId, title, policy, reason}."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        q = request.query_params
        rows = CollectionGenerationBatch.objects.select_related("session", "term", "provider_connection", "prepared_by__user", "submitted_by__user",
                                                                 "approved_by__user", "rejected_by__user").filter(school=membership.school)
        if q.get("status"):
            rows = rows.filter(status__in=q["status"].split(","))
        if _truthy(q.get("mine")):
            rows = rows.filter(prepared_by=membership)
        if _truthy(q.get("awaiting")):
            rows = rows.filter(status=BatchStatus.PENDING_APPROVAL).exclude(prepared_by=membership).exclude(submitted_by=membership)
        if _truthy(q.get("open")):
            rows = rows.filter(status__in=OPEN_STATUSES)
        limit, offset = paging(request, default=30)
        page = list(rows[offset: offset + limit + 1])
        return Response({
            "batches": [serialize_batch(b, membership) for b in page[:limit]], "hasMore": len(page) > limit, "permissions": permissions_of(membership),
        })

    def post(self, request, school_id):
        membership = acting_membership(request, school_id)
        data = body(request)
        session = _session(membership, data.get("sessionId"))
        term = _term(membership, data.get("termId"), session)
        created = batches.create_batch(membership, session=session, term=term, title=data.get("title"), policy_values=data.get("policy"), reason=data.get("reason"))
        return _batch_response(membership, created.id, http.HTTP_201_CREATED)


class BatchDetailView(CollectView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        return _batch_response(membership, batch_id)


class BatchItemsView(CollectView):
    """GET a batch's families (?bucket=eligibility, ?selected=1|0, ?overridden=1, ?generation=status, ?q=, paged), with how many are in each bucket."""

    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        batch = batches.get_batch(membership, batch_id)
        q = request.query_params
        rows = CollectionGenerationBatchItem.objects.select_related("family", "override_by__user", "manual_approved_by__user").filter(batch=batch)
        if q.get("bucket"):
            rows = rows.filter(eligibility_status__in=q["bucket"].split(","))
        if q.get("selected") in ("0", "1", "true", "false"):
            rows = rows.filter(selected=_truthy(q["selected"]))
        if _truthy(q.get("overridden")):
            rows = rows.filter(eligibility_override=True)
        if q.get("generation"):
            rows = rows.filter(generation_status__in=q["generation"].split(","))
        if q.get("q"):
            term = q["q"].strip()
            rows = rows.filter(Q(family__display_name__icontains=term) | Q(family__code__icontains=term))
        limit, offset = paging(request, default=50)
        page = list(rows[offset: offset + limit + 1])
        return Response({
            "items": [serialize_item(i) for i in page[:limit]], "hasMore": len(page) > limit, "version": batch.version, "snapshotHash": batch.snapshot_hash,
            "buckets": {r["eligibility_status"]: {"total": r["n"], "selected": r["s"]} for r in batch.items.values("eligibility_status").annotate(
                n=Count("id"), s=Count("id", filter=Q(selected=True)))},
        })


class BatchEventsView(CollectView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        batch = batches.get_batch(membership, batch_id)
        return Response({"events": [serialize_event(e) for e in CollectionBatchEvent.objects.select_related("actor__user").filter(batch=batch)]})


class BatchExportView(CollectView):
    """GET ?type=pdf|xlsx (&selected=1 for only the selected families). Reads what is stored: no provider is called. (Not `format`: that word
    is the framework's own for choosing a response renderer.)"""

    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        batch = batches.get_batch(membership, batch_id)
        try:
            data, content_type, name = exports.build(batch, request.query_params.get("type"), only_selected=_truthy(request.query_params.get("selected")))
        except ValueError:
            raise CollectionRefused("Choose pdf or xlsx.", "invalid_format")
        response = HttpResponse(data, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response


class BatchProgressView(CollectView):
    """GET how generation is going. Where no worker runs, watching it also drives it a little way."""

    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        batch = batches.get_batch(membership, batch_id)
        if batch.status == BatchStatus.PROCESSING:
            jobs.drain_inline()
            running.refresh_progress(batch.id)
            batch = batches.get_batch(membership, batch_id)
        return Response({"progress": running.progress(batch), "batch": serialize_batch(batch, membership)})


class BatchFailedView(CollectView):
    def get(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        batch = batches.get_batch(membership, batch_id)
        failed = CollectionGenerationBatchItem.objects.select_related("family").filter(batch=batch, selected=True, generation_status=GenerationStatus.FAILED)
        return Response({"items": [serialize_item(i) for i in failed], "count": failed.count()})


class BatchActionView(CollectView):
    """POST an action on a batch."""

    action = None

    def post(self, request, school_id, batch_id):
        membership = acting_membership(request, school_id)
        data = body(request)
        version = data.get("expectedVersion")
        a = self.action
        extra = {}
        if a == "preview":
            batches.refresh_preview(membership, batch_id, expected_version=version)
        elif a == "selection":
            batches.set_selection(
                membership, batch_id, select=data.get("select") or [], deselect=data.get("deselect") or [],
                select_all_eligible=bool(data.get("selectAllEligible")), deselect_all=bool(data.get("deselectAll")), expected_version=version,
            )
        elif a == "title":
            batches.rename(membership, batch_id, data.get("title"))
        elif a == "policy":
            batches.set_batch_policy(membership, batch_id, values=data.get("values"), reason=data.get("reason"), expected_version=version)
        elif a == "submit":
            batches.submit(membership, batch_id, expected_hash=data.get("expectedHash") or "", expected_version=version)
        elif a == "approve":
            batches.approve(membership, batch_id, expected_hash=data.get("expectedHash") or "", manual_item_ids=data.get("manualApprovals") or [])
        elif a == "reject":
            batches.reject(membership, batch_id, reason=data.get("reason"))
        elif a == "cancel":
            batches.cancel(membership, batch_id, reason=data.get("reason"))
        elif a == "start":
            running.start_processing(membership, batch_id)
            jobs.drain_inline()
            running.refresh_progress(batch_id)
        elif a == "retry":
            _, retried = running.retry_failed(membership, batch_id, item_ids=data.get("itemIds"))
            extra["retried"] = retried
            if retried:
                jobs.drain_inline()
                running.refresh_progress(batch_id)
            else:
                extra["approvalNeeded"] = True
        return _batch_response(membership, batch_id, **extra)


class BatchItemActionView(CollectView):
    action = None

    def post(self, request, school_id, batch_id, item_id):
        membership = acting_membership(request, school_id)
        data = body(request)
        version = data.get("expectedVersion")
        if self.action == "override":
            batches.set_eligibility_override(membership, batch_id, item_id, reason=data.get("reason"), expected_version=version)
        elif self.action == "override-clear":
            batches.clear_eligibility_override(membership, batch_id, item_id, expected_version=version)
        else:
            batches.choose_arrears(membership, batch_id, item_id, receivable_ids=data.get("receivableIds"), expected_version=version)
        return _batch_response(membership, batch_id)


# -- a family's payer -------------------------------------------------------------------------------


@method_decorator(sensitive_post_parameters("bvn", "nin"), name="dispatch")
class PayerIdentityView(CollectView):
    """GET whether an identity number is on file; PUT {bvn, nin} to record one. Write-only: a number is never returned."""

    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        return Response({"identity": identity.status_of(_family(membership, family_id))})

    def put(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        data = body(request)
        identity.save(membership, family, bvn=data.get("bvn"), nin=data.get("nin"))
        return Response({"identity": identity.status_of(family)})

    post = put


# -- the overview -----------------------------------------------------------------------------------


class DashboardView(CollectView):
    """Smart Money Collection at a glance: the active provider, the connected ones, a planned switch, the policy, the current period, how
    many families have accounts, batches waiting for someone, failed generations, and what has arrived and been reconciled."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        school = membership.school
        connections = list(CollectionProviderConnection.objects.filter(school=school).exclude(status=ConnectionStatus.REVOKED).order_by("created_at"))
        active = next((c for c in connections if c.is_active_provider), None)
        session, term = periods.current_period(school)
        by_status = {r["status"]: r["n"] for r in FamilyCollectionAccount.objects.filter(school=school).values("status").annotate(n=Count("id"))}
        live_families = FamilyCollectionAccount.objects.filter(school=school, status__in=LIVE_STATUSES).values("family_id").distinct().count()
        active_families = Family.objects.filter(school=school, status="active", merged_into__isnull=True).count()
        rows = CollectionGenerationBatch.objects.select_related("session", "term", "provider_connection", "prepared_by__user").filter(school=school)
        counts = {r["status"]: r["n"] for r in rows.values("status").annotate(n=Count("id"))}
        awaiting = rows.filter(status=BatchStatus.PENDING_APPROVAL).exclude(prepared_by=membership).exclude(submitted_by=membership)
        failed_items = CollectionGenerationBatchItem.objects.filter(batch__school=school, selected=True, generation_status=GenerationStatus.FAILED).count()
        money = collections_summary.build(school, recent=8)
        switch = switching.refresh(school)
        return Response({
            "activeProvider": serialize_connection(active) if active else None,
            "connectedProviders": [serialize_connection(c) for c in connections],
            "scheduledSwitch": serialize_switch(switch) if switch else None,
            "policy": serialize_policy(policy.school_policy(school)),
            "currentPeriod": {
                "session": {"id": str(session.id), "name": session.name} if session else None,
                "term": {"id": str(term.id), "name": term.name} if term else None,
            },
            "accounts": {"byStatus": by_status, "familiesWithLiveAccount": live_families, "activeFamilies": active_families,
                         "familiesWithoutAccount": max(active_families - live_families, 0)},
            "batches": {
                "byStatus": counts, "drafts": counts.get(BatchStatus.DRAFT, 0), "rejected": counts.get(BatchStatus.REJECTED, 0),
                "pendingApproval": counts.get(BatchStatus.PENDING_APPROVAL, 0), "processing": counts.get(BatchStatus.PROCESSING, 0),
                "recent": [serialize_batch(b, membership) for b in rows[:5]],
            },
            "pendingApprovals": [serialize_batch(b, membership) for b in awaiting[:10]],
            "failedGenerations": {"families": failed_items, "batches": counts.get(BatchStatus.PARTIALLY_SUCCESSFUL, 0) + counts.get(BatchStatus.FAILED, 0)},
            "recentTransactions": money["recent"], "reconciliation": money["reconciliation"], "collected": {"today": money["today"], "thisWeek": money["thisWeek"],
                                                                                                             "thisTerm": money["thisTerm"]},
            "permissions": permissions_of(membership),
        })
