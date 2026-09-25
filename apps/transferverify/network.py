"""The discovery layer: bridging a school's own Student to a platform-level
NetworkStudentIdentity, turning a published BadDebtClassification into a real
TransferAlert, and phone-candidate matching across schools that share an
active association. See apps.transferverify.models for why these are
platform-level, not tenant-scoped, and for the tenant-isolation reasoning
behind every query here.
"""

from django.db import transaction
from django.utils import timezone

from apps.accounts.identity import normalize_parent_phone
from apps.core.errors import Rejected
from apps.students.models import GuardianLink

from .associations import active_association_ids
from .models import (
    NetworkSearchAudit,
    NetworkStudentIdentity,
    StudentNetworkEnrollment,
    TransferAlert,
    TransferAlertState,
)


def _guardian_phones(student) -> list[str]:
    """Every distinct, normalized guardian phone this school has on file for
    this student. A candidate lookup signal only - see match_by_phone."""
    numbers: list[str] = []
    for phone in GuardianLink.objects.filter(student=student).values_list("phone", flat=True):
        normalized = normalize_parent_phone(phone)
        if normalized and normalized not in numbers:
            numbers.append(normalized)
    return numbers


@transaction.atomic
def enrollment_for(*, school, student, membership) -> StudentNetworkEnrollment:
    """This school's own network enrollment for this student - creating a
    brand new NetworkStudentIdentity the first time this student ever
    touches TransferVerify anywhere. Keeps the guardian phone current while
    preserving the prior number in history, never overwriting it silently."""
    existing = StudentNetworkEnrollment.objects.filter(school=school, student=student).first()
    phones = _guardian_phones(student)
    current_phone = phones[0] if phones else ""

    if existing is not None:
        if current_phone and current_phone != existing.guardian_phone_e164:
            history = list(existing.guardian_phone_history)
            if existing.guardian_phone_e164:
                history.append({"phone": existing.guardian_phone_e164, "changedAt": timezone.now().isoformat()})
            existing.guardian_phone_history = history
            existing.guardian_phone_e164 = current_phone
            existing.save(update_fields=["guardian_phone_e164", "guardian_phone_history", "updated_at"])
        return existing

    identity = NetworkStudentIdentity.objects.create()
    return StudentNetworkEnrollment.objects.create(
        network_identity=identity, school=school, student=student,
        guardian_phone_e164=current_phone, linked_by=membership,
    )


@transaction.atomic
def publish_alert(*, classification, membership, association_ids: list[str]) -> TransferAlert:
    """The one place a school's private classification actually becomes a
    real, cross-tenant TransferAlert - called only from
    apps.transferverify.services.publish_to_transferverify, after that
    function's own Proprietor-only, BAD_DEBT-only checks already passed."""
    enrollment = enrollment_for(school=classification.school, student=classification.student, membership=membership)
    alert = TransferAlert.objects.filter(source_classification=classification).first()
    if alert is None:
        alert = TransferAlert(source_classification=classification, source_school=classification.school)
    alert.network_identity = enrollment.network_identity
    alert.association_scope = list(association_ids)
    alert.state = TransferAlertState.ACTIVE
    alert.snapshot_status = classification.status
    alert.snapshot_outstanding_amount_minor = classification.outstanding_amount_minor
    alert.snapshot_reason = classification.publication_reason
    alert.snapshot_note = classification.publication_note
    alert.published_by = membership
    alert.published_at = classification.published_at
    alert.withdrawn_at = None
    alert.resolved_at = None
    alert.save()
    return alert


def withdraw_alert(*, classification) -> None:
    alert = TransferAlert.objects.filter(source_classification=classification).first()
    if alert is None:
        return
    alert.state = TransferAlertState.WITHDRAWN
    alert.withdrawn_at = timezone.now()
    alert.save(update_fields=["state", "withdrawn_at", "updated_at"])


def resolve_alert(*, classification) -> None:
    alert = TransferAlert.objects.filter(source_classification=classification).first()
    if alert is None or alert.state in {TransferAlertState.WITHDRAWN, TransferAlertState.RESOLVED}:
        return
    alert.state = TransferAlertState.RESOLVED
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["state", "resolved_at", "updated_at"])


def serialize_alert_candidate(alert: TransferAlert) -> dict:
    """Deliberately minimal - status and reason only, no raw amount, no
    guardian contact detail. Fuller detail is only ever available through an
    explicit TransferVerificationRequest the source school responds to (a
    later phase), matching the brief's tiered-disclosure principle."""
    return {
        "transferAlertId": str(alert.id),
        "confidence": "candidate_match",
        "matchedOn": "guardian_phone",
        "sourceSchoolName": alert.source_school.name,
        "status": alert.snapshot_status,
        "reason": alert.snapshot_reason,
        "publishedAt": alert.published_at.isoformat(),
    }


def match_by_phone(*, membership, phone: str) -> list[dict]:
    """A candidate-only lookup, narrowed to network identities another
    school has ever recorded this guardian phone against (current or
    historical), then filtered to alerts published into an association this
    searching school actively shares with the source school. Phone alone
    never confirms identity - every result is tagged candidate_match, never
    higher, until a stronger match exists (biometric, a later phase)."""
    normalized = normalize_parent_phone(phone)
    if not normalized:
        raise Rejected("Enter a valid guardian phone number.")

    searching_school = membership.school
    my_association_ids = active_association_ids(searching_school)

    candidate_identity_ids = set(
        StudentNetworkEnrollment.objects.filter(guardian_phone_e164=normalized)
        .exclude(school=searching_school)
        .values_list("network_identity_id", flat=True)
    )
    for enrollment in StudentNetworkEnrollment.objects.exclude(school=searching_school).only(
        "network_identity_id", "guardian_phone_history"
    ):
        if any(entry.get("phone") == normalized for entry in enrollment.guardian_phone_history):
            candidate_identity_ids.add(enrollment.network_identity_id)

    results: list[dict] = []
    if candidate_identity_ids and my_association_ids:
        alerts = TransferAlert.objects.select_related("source_school").filter(
            network_identity_id__in=candidate_identity_ids,
            state__in=[TransferAlertState.ACTIVE, TransferAlertState.VERIFICATION_PENDING, TransferAlertState.DISPUTED],
        )
        for alert in alerts:
            if my_association_ids & set(alert.association_scope):
                results.append(serialize_alert_candidate(alert))

    NetworkSearchAudit.objects.create(
        searching_school=searching_school, searching_membership=membership, result_count=len(results)
    )
    return results
