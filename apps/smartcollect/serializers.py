"""What the app is allowed to see of policy, batches and switches. Every field is named: a column added to a model later is not exposed until
someone decides it should be. Nothing here can carry a credential or an identity number."""

from apps.bankconnect import permissions as authority
from apps.bankconnect.providers import registry
from apps.receivables import periods

from . import batches, policy, running
from .constants import (
    ArrearsPolicy,
    BatchStatus,
    Eligibility,
    EligibilityPolicy,
    ExpiryKind,
    ReasonPolicy,
    ReuseScope,
    SettlementAction,
    SwitchPolicy,
    AccountMode,
)


def _iso(value):
    return value.isoformat() if value else None


def _person(membership) -> dict | None:
    if membership is None:
        return None
    user = getattr(membership, "user", None)
    return {"membershipId": str(membership.id), "name": (user.get_full_name() or user.email) if user else "", "role": membership.role}


OPTIONS = {
    "account_mode": AccountMode, "reuse_scope": ReuseScope, "settlement_action": SettlementAction, "arrears_policy": ArrearsPolicy,
    "eligibility_policy": EligibilityPolicy, "provider_switch_policy": SwitchPolicy, "override_reason_policy": ReasonPolicy, "expiry_kind": ExpiryKind,
}


def options() -> dict:
    """The choices for every policy field, with the words to show, so a screen never hard-codes them."""
    return {name: [{"value": v, "label": label} for v, label in enum.choices] for name, enum in OPTIONS.items()}


def serialize_policy(row) -> dict:
    values = policy.school_values(row)
    return {
        "values": values,
        "providerSwitchPolicy": row.default_provider_switch_policy,
        "overrideReasonPolicy": row.override_reason_policy,
        "labels": policy.LABELS,
        "options": options(),
        "problems": policy.problems_of(values),
        "updatedAt": _iso(row.updated_at),
    }


def serialize_override(o) -> dict:
    target = {"session": o.session, "term": o.term, "batch": o.batch, "family": o.family}[o.scope]
    label = {
        "session": lambda t: t.name, "term": lambda t: f"{t.session.name} · {t.name}", "batch": lambda t: t.title or str(t.id)[:8],
        "family": lambda t: t.display_name,
    }[o.scope](target)
    return {
        "id": str(o.id), "scope": o.scope, "targetId": str(target.id), "targetLabel": label, "values": o.values, "reason": o.reason,
        "expiryKind": o.expiry_kind, "expiresOn": _iso(o.expires_on),
        "expiryTerm": o.expiry_term.name if o.expiry_term_id else None, "expirySession": o.expiry_session.name if o.expiry_session_id else None,
        "consumedAt": _iso(o.consumed_at), "createdBy": _person(o.created_by), "createdAt": _iso(o.created_at),
        "removedAt": _iso(o.removed_at), "removalReason": o.removal_reason, "active": o.removed_at is None and o.consumed_at is None,
    }


def serialize_resolved(resolved) -> dict:
    return {"values": resolved.values, "sources": resolved.sources, "problems": resolved.problems, "description": policy.describe(resolved)}


def serialize_item(i) -> dict:
    return {
        "id": str(i.id), "familyId": str(i.family_id), "familyCode": i.family.code, "familyName": i.family.display_name,
        "selected": i.selected, "previousArrearsMinor": i.previous_arrears_minor, "currentDueMinor": i.current_due_minor, "creditMinor": i.credit_minor,
        "proposedCollectionMinor": i.proposed_collection_minor, "arrearsPolicy": i.arrears_policy,
        "arrearsBreakdown": i.arrears_breakdown, "customArrearsReceivableIds": i.custom_arrears_receivable_ids,
        "eligibilityStatus": i.eligibility_status, "eligibilityLabel": Eligibility(i.eligibility_status).label, "eligibilityNote": i.eligibility_note,
        "eligibilityOverride": i.eligibility_override, "overrideReason": i.override_reason, "overrideBy": _person(i.override_by),
        "manualApprovalNeeded": i.eligibility_status == Eligibility.MANUAL_APPROVAL, "manualApprovedBy": _person(i.manual_approved_by),
        "policy": (i.policy_snapshot or {}).get("values", {}), "policySources": (i.policy_snapshot or {}).get("sources", {}),
        "missingDetails": (i.policy_snapshot or {}).get("missing", []),
        "generationStatus": i.generation_status, "attemptCount": i.attempt_count, "providerAccountReference": i.provider_account_reference,
        "errorCode": i.error_code, "errorMessage": i.safe_error_message, "accountId": str(i.account_id) if i.account_id else None,
        "completedAt": _iso(i.completed_at),
    }


def serialize_batch(b, membership=None, *, detail: bool = False) -> dict:
    connector = registry.get_connector(b.provider)
    data = {
        "id": str(b.id), "title": b.title, "status": b.status, "statusLabel": BatchStatus(b.status).label, "version": b.version,
        "snapshotHash": b.snapshot_hash, "approvedSnapshotHash": b.approved_snapshot_hash,
        "session": {"id": str(b.session_id), "name": b.session.name}, "term": {"id": str(b.term_id), "name": b.term.name} if b.term_id else None,
        "period": periods.label(b.session, b.term),
        "provider": {"connectionId": str(b.provider_connection_id), "code": b.provider, "name": connector.info.display_name if connector else b.provider,
                     "environment": b.environment, "isActive": b.provider_connection.is_active_provider},
        "policy": (b.policy_snapshot or {}).get("values", {}), "policySources": (b.policy_snapshot or {}).get("sources", {}),
        "policyProblems": (b.policy_snapshot or {}).get("problems", []),
        "preparedBy": _person(b.prepared_by), "preparedAt": _iso(b.prepared_at), "submittedBy": _person(b.submitted_by), "submittedAt": _iso(b.submitted_at),
        "approvedBy": _person(b.approved_by), "approvedAt": _iso(b.approved_at), "rejectedBy": _person(b.rejected_by), "rejectedAt": _iso(b.rejected_at),
        "rejectionReason": b.rejection_reason, "startedAt": _iso(b.started_at), "completedAt": _iso(b.completed_at), "previewBuiltAt": _iso(b.preview_built_at),
        "totals": {
            "families": b.family_count, "selected": b.selected_count, "previousArrearsMinor": b.total_previous_arrears_minor,
            "currentDueMinor": b.total_current_due_minor, "collectionMinor": b.total_collection_minor,
            "successful": b.success_count, "failed": b.failed_count,
        },
    }
    if membership is not None:
        makers = batches.makers_of(b) if b.status == BatchStatus.PENDING_APPROVAL else set()
        editable = b.status in (BatchStatus.DRAFT, BatchStatus.REJECTED)
        data["can"] = {
            "edit": editable and authority.can_prepare(membership),
            "submit": editable and authority.can_prepare(membership),
            "approve": b.status == BatchStatus.PENDING_APPROVAL and authority.can_approve(membership) and membership.id not in makers,
            "reject": b.status == BatchStatus.PENDING_APPROVAL and authority.can_approve(membership) and membership.id not in makers,
            "start": b.status == BatchStatus.APPROVED and (authority.can_prepare(membership) or authority.can_approve(membership)),
            "retry": b.status in (BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED) and authority.can_prepare(membership),
            "cancel": b.status in (BatchStatus.DRAFT, BatchStatus.PENDING_APPROVAL, BatchStatus.REJECTED, BatchStatus.APPROVED, BatchStatus.FAILED)
            and authority.can_prepare(membership),
            "overridePolicy": authority.can_manage_policy(membership),
        }
        data["isMaker"] = membership.id in batches.makers_of(b)
    if detail:
        data["counts"] = _bucket_counts(b)
        data["progress"] = running.progress(b)
    return data


def _bucket_counts(b) -> dict:
    counts: dict = {}
    for status, selected in b.items.values_list("eligibility_status", "selected"):
        row = counts.setdefault(status, {"total": 0, "selected": 0})
        row["total"] += 1
        row["selected"] += 1 if selected else 0
    return counts


def serialize_event(e) -> dict:
    return {"id": str(e.id), "kind": e.kind, "version": e.version, "actor": _person(e.actor), "detail": e.detail, "at": _iso(e.at)}


def serialize_switch(s) -> dict:
    return {
        "id": str(s.id), "status": s.status, "scheduledFor": _iso(s.scheduled_for), "readyAt": _iso(s.ready_at), "appliedAt": _iso(s.applied_at),
        "current": (s.review or {}).get("current"), "target": (s.review or {}).get("target"), "affected": (s.review or {}).get("affected"),
        "batches": (s.review or {}).get("batches"), "warnings": (s.review or {}).get("warnings", []), "blockers": s.blockers,
        "switchPolicy": s.switch_policy, "note": s.note, "createdBy": _person(s.created_by), "createdAt": _iso(s.created_at),
        "appliedBy": _person(s.applied_by), "cancelledAt": _iso(s.cancelled_at), "cancelReason": s.cancel_reason,
        "canApply": s.status == "ready_to_switch", "reviewedAt": (s.review or {}).get("reviewedAt"),
    }
