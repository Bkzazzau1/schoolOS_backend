"""A guardian's own dispute of a published TransferAlert, and the source
school's digital clearance once a case is settled. See
TransferClearanceDispute and TransferClearance's own docstrings for why
opening a dispute always flips the alert to "disputed" first, and why a
clearance's public lookup reveals almost nothing.
"""

import secrets

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.notifications.services import notify
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink

from .models import (
    BadDebtStatus,
    ClearanceStatus,
    DisputeReason,
    DisputeStatus,
    TransferAlert,
    TransferAlertState,
    TransferClearance,
    TransferClearanceDispute,
)


def _assert_guardian_of(membership: Membership, alert: TransferAlert) -> None:
    if not membership.is_active or membership.role != Role.PARENT:
        raise Rejected("Only a guardian can open a dispute.")
    if membership.school_id != alert.source_school_id:
        raise Rejected("This case does not belong to your school.")
    student = alert.source_classification.student
    is_guardian = GuardianLink.objects.filter(student=student, account_user=membership.user).exists()
    if not is_guardian:
        raise Rejected("You are not a recorded guardian for this student.")


def serialize_dispute(item: TransferClearanceDispute) -> dict:
    return {
        "id": str(item.id),
        "transferAlertId": str(item.transfer_alert_id),
        "reason": item.reason,
        "explanation": item.explanation,
        "evidenceReferences": item.evidence_references,
        "status": item.status,
        "openedAt": item.opened_at.isoformat(),
        "reviewedAt": item.reviewed_at.isoformat() if item.reviewed_at else None,
        "resolutionNote": item.resolution_note,
    }


@transaction.atomic
def open_dispute(
    *, membership: Membership, transfer_alert_id: str, reason: str, explanation: str = "",
    evidence_references: list[str] | None = None,
) -> TransferClearanceDispute:
    try:
        alert = (
            TransferAlert.objects.select_related("source_school", "source_classification__student")
            .select_for_update()
            .get(id=transfer_alert_id)
        )
    except (TransferAlert.DoesNotExist, ValueError, TypeError):
        raise Rejected("That case does not exist.")
    _assert_guardian_of(membership, alert)
    if reason not in DisputeReason.values:
        raise Rejected("Choose a reason for this dispute.")
    if alert.state not in {TransferAlertState.ACTIVE, TransferAlertState.VERIFICATION_PENDING}:
        raise Rejected("This case is not open to a new dispute.")
    if TransferClearanceDispute.objects.filter(transfer_alert=alert, status=DisputeStatus.OPENED).exists():
        raise Rejected("A dispute is already open for this case.")

    item = TransferClearanceDispute.objects.create(
        transfer_alert=alert, opened_by=membership, reason=reason, explanation=explanation.strip(),
        evidence_references=list(evidence_references or []),
    )
    alert.state = TransferAlertState.DISPUTED
    alert.save(update_fields=["state", "updated_at"])

    for proprietor in Membership.objects.filter(school=alert.source_school, role=Role.PROPRIETOR, is_active=True):
        notify(
            proprietor, "transferverify_dispute_opened", "TransferVerify dispute opened",
            "A guardian has disputed one of your published TransferVerify cases.",
            {"disputeId": str(item.id), "transferAlertId": str(alert.id)},
        )
    return item


def _loaded_dispute_for_source(membership: Membership, dispute_id) -> TransferClearanceDispute:
    try:
        item = (
            TransferClearanceDispute.objects.select_related("transfer_alert__source_school")
            .select_for_update()
            .get(id=dispute_id)
        )
    except (TransferClearanceDispute.DoesNotExist, ValueError, TypeError):
        raise Rejected("That dispute does not exist.")
    if item.transfer_alert.source_school_id != membership.school_id:
        raise Rejected("That dispute does not belong to this school.")
    return item


@transaction.atomic
def review_dispute(*, membership: Membership, dispute_id: str, decision: str, note: str = "") -> TransferClearanceDispute:
    """Only the owner of the SOURCE school decides - the same authority
    tier as publishing and responding to a verification request."""
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can review a dispute.")
    if decision not in {DisputeStatus.ACCEPTED, DisputeStatus.REJECTED}:
        raise Rejected("Choose to accept or reject the dispute.")
    item = _loaded_dispute_for_source(membership, dispute_id)
    if item.status != DisputeStatus.OPENED:
        raise Rejected("This dispute has already been reviewed.")

    item.status = decision
    item.reviewed_by = membership
    item.reviewed_at = timezone.now()
    item.resolution_note = note.strip()
    item.save(update_fields=["status", "reviewed_by", "reviewed_at", "resolution_note", "updated_at"])

    alert = item.transfer_alert
    if alert.state == TransferAlertState.DISPUTED:
        # The classification may have been resolved by its own school while
        # the dispute was still open (see network.resolve_alert) - reflect
        # its real current state rather than assuming it is still active.
        alert.state = (
            TransferAlertState.RESOLVED
            if alert.source_classification.status == BadDebtStatus.RESOLVED
            else TransferAlertState.ACTIVE
        )
        if alert.state == TransferAlertState.RESOLVED:
            alert.resolved_at = timezone.now()
            alert.save(update_fields=["state", "resolved_at", "updated_at"])
        else:
            alert.save(update_fields=["state", "updated_at"])

    notify(
        item.opened_by, "transferverify_dispute_reviewed", "TransferVerify dispute reviewed",
        f"The school has {item.status} your TransferVerify dispute.",
        {"disputeId": str(item.id)},
    )
    return item


def disputes_for_school(school) -> list[TransferClearanceDispute]:
    return list(
        TransferClearanceDispute.objects.select_related("transfer_alert__source_school")
        .filter(transfer_alert__source_school=school)
        .order_by("-opened_at")
    )


def disputes_opened_by(membership: Membership) -> list[TransferClearanceDispute]:
    return list(
        TransferClearanceDispute.objects.select_related("transfer_alert__source_school")
        .filter(opened_by=membership)
        .order_by("-opened_at")
    )


def case_status_for_guardian(membership: Membership) -> list[dict]:
    """Every published TransferVerify case this school has against one of
    this guardian's own recorded children - read-only, and only ever this
    school's own case, matching the role matrix exactly: a guardian sees
    their own case status, never another school's internal notes."""
    if not membership.is_active or membership.role != Role.PARENT:
        raise Rejected("Only a guardian can view this.")
    student_ids = GuardianLink.objects.filter(
        account_user=membership.user, student__school=membership.school
    ).values_list("student_id", flat=True)
    alerts = TransferAlert.objects.select_related("source_classification__student").filter(
        source_school=membership.school, source_classification__student_id__in=list(student_ids)
    )
    results = []
    for alert in alerts:
        student = alert.source_classification.student
        has_open_dispute = TransferClearanceDispute.objects.filter(
            transfer_alert=alert, status=DisputeStatus.OPENED
        ).exists()
        has_active_clearance = TransferClearance.objects.filter(
            source_alert=alert, status=ClearanceStatus.ACTIVE
        ).exists()
        results.append(
            {
                "transferAlertId": str(alert.id),
                "studentName": f"{student.first_name} {student.surname}".strip(),
                "state": alert.state,
                "status": alert.snapshot_status,
                "reason": alert.snapshot_reason,
                "publishedAt": alert.published_at.isoformat(),
                "hasOpenDispute": has_open_dispute,
                "hasActiveClearance": has_active_clearance,
            }
        )
    return results


# --- Clearance --------------------------------------------------------------


def serialize_clearance(item: TransferClearance) -> dict:
    return {
        "id": str(item.id),
        "issuingSchoolName": item.issuing_school.name,
        "verificationToken": item.verification_token,
        "status": item.status,
        "issuedAt": item.issued_at.isoformat(),
        "revokedAt": item.revoked_at.isoformat() if item.revoked_at else None,
    }


@transaction.atomic
def issue_clearance(*, membership: Membership, external_id: str, note: str = "") -> TransferClearance:
    """Owner-only, and only once the source classification itself is
    resolved - a clearance is the school's own written confirmation the
    matter is settled, never issued ahead of that."""
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can issue a clearance.")
    try:
        alert = (
            TransferAlert.objects.select_related("source_classification")
            .select_for_update()
            .get(source_school=membership.school, source_classification__external_id=external_id)
        )
    except (TransferAlert.DoesNotExist, ValueError, TypeError):
        raise Rejected("This case has not been published to TransferVerify.")
    if alert.source_classification.status != BadDebtStatus.RESOLVED:
        raise Rejected("A clearance can only be issued once this case is resolved.")
    if alert.state == TransferAlertState.DISPUTED:
        raise Rejected("An open dispute must be reviewed before a clearance can be issued.")
    if TransferClearance.objects.filter(source_alert=alert, status=ClearanceStatus.ACTIVE).exists():
        raise Rejected("An active clearance has already been issued for this case.")

    return TransferClearance.objects.create(
        network_identity=alert.network_identity, issuing_school=membership.school, source_alert=alert,
        verification_token=secrets.token_urlsafe(24), issued_by=membership,
    )


@transaction.atomic
def revoke_clearance(*, membership: Membership, clearance_id: str) -> TransferClearance:
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can revoke a clearance.")
    try:
        item = TransferClearance.objects.select_for_update().get(id=clearance_id, issuing_school=membership.school)
    except (TransferClearance.DoesNotExist, ValueError, TypeError):
        raise Rejected("That clearance does not exist for this school.")
    if item.status != ClearanceStatus.ACTIVE:
        raise Rejected("This clearance has already been revoked.")
    item.status = ClearanceStatus.REVOKED
    item.revoked_at = timezone.now()
    item.revoked_by = membership
    item.save(update_fields=["status", "revoked_at", "revoked_by"])
    return item


def clearances_for_school(school) -> list[TransferClearance]:
    return list(TransferClearance.objects.filter(issuing_school=school).order_by("-issued_at"))


def verify_clearance(token: str) -> dict:
    """Public, unauthenticated, and deliberately minimal - never a ledger, a
    fingerprint, or guardian contact detail (see TransferClearance's own
    docstring)."""
    cleaned = (token or "").strip()
    if not cleaned:
        return {"valid": False, "message": "No verification code was given."}
    clearance = TransferClearance.objects.filter(verification_token=cleaned).first()
    if clearance is None or not clearance.is_active:
        return {"valid": False, "message": "This is not a valid SchoolOS Transfer Clearance."}
    return {"valid": True, "message": "Valid SchoolOS Transfer Clearance."}
