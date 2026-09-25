"""One school asking another, factual question about a candidate match it
found through apps.transferverify.network.match_by_phone: "does this alert
still concern the student we are admitting, and what is its status?" The
source school gives one plain answer - never a negotiation, never an
accusation. See TransferVerificationRequest's own docstring for the state
machine.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.notifications.services import notify
from apps.schools.models import Membership, Role

from .associations import active_association_ids
from .models import (
    TransferAlert,
    TransferAlertState,
    TransferVerificationRequest,
    TransferVerificationRequestStatus,
)

#: Who may search for a candidate and ask a source school about one - the
#: same roles apps.transferverify.network.match_by_phone already requires.
_REQUESTING_ROLES = {Role.PROPRIETOR, Role.ADMINISTRATOR}


def _visible_to(school, alert: TransferAlert) -> bool:
    """An alert is only ever visible to a school that actively shares one of
    the associations it was published to - the same rule
    apps.transferverify.network.match_by_phone already enforces."""
    return bool(active_association_ids(school) & set(alert.association_scope))


def serialize_request(item: TransferVerificationRequest) -> dict:
    return {
        "id": str(item.id),
        "transferAlertId": str(item.transfer_alert_id),
        "requestingSchoolId": str(item.requesting_school_id),
        "requestingSchoolName": item.requesting_school.name,
        "sourceSchoolName": item.transfer_alert.source_school.name,
        "status": item.status,
        "note": item.note,
        "requestedAt": item.requested_at.isoformat(),
        "respondedAt": item.responded_at.isoformat() if item.responded_at else None,
        "responseNote": item.response_note,
        "responseStatusSnapshot": item.response_status_snapshot,
    }


@transaction.atomic
def send_request(*, membership: Membership, transfer_alert_id: str, note: str = "") -> TransferVerificationRequest:
    if not membership.is_active or membership.role not in _REQUESTING_ROLES:
        raise Rejected("Only the owner or an administrator can ask a source school about a case.")
    try:
        alert = TransferAlert.objects.select_related("source_school").select_for_update().get(id=transfer_alert_id)
    except (TransferAlert.DoesNotExist, ValueError, TypeError):
        raise Rejected("That case does not exist.")
    if alert.source_school_id == membership.school_id:
        raise Rejected("A school cannot ask itself about its own case.")
    if not _visible_to(membership.school, alert):
        raise Rejected("This case is not discoverable to your school.")
    if alert.state not in {TransferAlertState.ACTIVE, TransferAlertState.VERIFICATION_PENDING, TransferAlertState.DISPUTED}:
        raise Rejected("This case is no longer open for verification.")
    if TransferVerificationRequest.objects.filter(
        transfer_alert=alert, requesting_school=membership.school, status=TransferVerificationRequestStatus.PENDING
    ).exists():
        raise Rejected("You already have an open request for this case.")

    item = TransferVerificationRequest.objects.create(
        transfer_alert=alert, requesting_school=membership.school, requested_by=membership, note=note.strip()
    )
    if alert.state == TransferAlertState.ACTIVE:
        alert.state = TransferAlertState.VERIFICATION_PENDING
        alert.save(update_fields=["state", "updated_at"])

    for proprietor in Membership.objects.filter(school=alert.source_school, role=Role.PROPRIETOR, is_active=True):
        notify(
            proprietor, "transferverify_request_received", "TransferVerify request received",
            f"{membership.school.name} asked to verify one of your published TransferVerify cases.",
            {"transferVerificationRequestId": str(item.id), "transferAlertId": str(alert.id)},
        )
    return item


def _loaded_for_source(membership: Membership, request_id) -> TransferVerificationRequest:
    try:
        item = (
            TransferVerificationRequest.objects.select_related("transfer_alert", "requesting_school")
            .select_for_update()
            .get(id=request_id)
        )
    except (TransferVerificationRequest.DoesNotExist, ValueError, TypeError):
        raise Rejected("That verification request does not exist.")
    if item.transfer_alert.source_school_id != membership.school_id:
        raise Rejected("That verification request does not belong to this school.")
    return item


@transaction.atomic
def respond_to_request(*, membership: Membership, request_id: str, decision: str, note: str = "") -> TransferVerificationRequest:
    """Only the owner of the SOURCE school answers - this reveals more about
    that school's own case than the candidate match already did, so it stays
    as tightly held as the publish action itself."""
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can respond to a verification request.")
    if decision not in {TransferVerificationRequestStatus.CONFIRMED, TransferVerificationRequestStatus.REJECTED}:
        raise Rejected("Choose to confirm or reject the request.")
    item = _loaded_for_source(membership, request_id)
    if item.status != TransferVerificationRequestStatus.PENDING:
        raise Rejected("This request has already been answered.")

    item.status = decision
    item.responded_at = timezone.now()
    item.responded_by = membership
    item.response_note = note.strip()
    item.response_status_snapshot = item.transfer_alert.source_classification.status
    item.save(update_fields=["status", "responded_at", "responded_by", "response_note", "response_status_snapshot", "updated_at"])

    alert = item.transfer_alert
    if alert.state == TransferAlertState.VERIFICATION_PENDING:
        alert.state = TransferAlertState.ACTIVE
        alert.save(update_fields=["state", "updated_at"])

    notify(
        item.requested_by, "transferverify_request_answered", "TransferVerify request answered",
        f"{membership.school.name} {item.status} your TransferVerify request.",
        {"transferVerificationRequestId": str(item.id)},
    )
    return item


@transaction.atomic
def cancel_request(*, membership: Membership, request_id: str) -> TransferVerificationRequest:
    try:
        item = (
            TransferVerificationRequest.objects.select_related("transfer_alert")
            .select_for_update()
            .get(id=request_id)
        )
    except (TransferVerificationRequest.DoesNotExist, ValueError, TypeError):
        raise Rejected("That verification request does not exist.")
    if item.requesting_school_id != membership.school_id:
        raise Rejected("That verification request does not belong to this school.")
    if item.status != TransferVerificationRequestStatus.PENDING:
        raise Rejected("Only a pending request can be cancelled.")

    item.status = TransferVerificationRequestStatus.CANCELLED
    item.save(update_fields=["status", "updated_at"])
    alert = item.transfer_alert
    if alert.state == TransferAlertState.VERIFICATION_PENDING:
        alert.state = TransferAlertState.ACTIVE
        alert.save(update_fields=["state", "updated_at"])
    return item


def requests_sent_by(school) -> list[TransferVerificationRequest]:
    return list(
        TransferVerificationRequest.objects.select_related("transfer_alert__source_school", "requesting_school")
        .filter(requesting_school=school)
        .order_by("-requested_at")
    )


def requests_received_by(school) -> list[TransferVerificationRequest]:
    return list(
        TransferVerificationRequest.objects.select_related("transfer_alert__source_school", "requesting_school")
        .filter(transfer_alert__source_school=school)
        .order_by("-requested_at")
    )
