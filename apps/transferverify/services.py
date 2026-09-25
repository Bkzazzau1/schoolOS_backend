from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.owner.jobs.access import has_duty
from apps.schools.models import Membership, Role
from apps.students.models import Student
from apps.sync.models import SyncRecord

from .associations import active_association_ids
from .models import BadDebtClassification, BadDebtEvent, BadDebtStatus, PublicationReason

BAD_DEBT_ENTITY = "transferverify_bad_debt_classification"

#: Duty a Proprietor can delegate so trusted Finance staff can classify a
#: case without being able to publish it to TransferVerify - that action
#: stays Proprietor-only (see apps.transferverify's later publish phase).
CLASSIFY_DUTY = "finance.bad_debt_classification"

_OPEN_STATUSES = {BadDebtStatus.OUTSTANDING, BadDebtStatus.RECOVERY_IN_PROGRESS, BadDebtStatus.BAD_DEBT}

#: A status may only move forward along this order, or straight to RESOLVED
#: from anywhere open - never backward, so a classification's own history
#: reads as a straight line even though a case need not visit every step.
_STATUS_ORDER = [BadDebtStatus.OUTSTANDING, BadDebtStatus.RECOVERY_IN_PROGRESS, BadDebtStatus.BAD_DEBT]


def can_classify(membership: Membership) -> bool:
    return membership.role == Role.PROPRIETOR or has_duty(membership, CLASSIFY_DUTY)


def _assert_classify_authority(membership: Membership) -> None:
    if not membership.is_active:
        raise Rejected("Only an active membership can manage bad debt classifications.")
    if not can_classify(membership):
        raise Rejected(
            "Only the owner, or someone the owner has specifically authorized, can classify a bad debt."
        )


def _assert_publish_authority(membership: Membership) -> None:
    """Publishing is never delegable, unlike classification - the one action
    that would make a school's private financial fact reachable by another
    school stays with the owner alone."""
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can publish a case to TransferVerify.")


def _membership_name(membership):
    if membership is None:
        return ""
    user = membership.user
    getter = getattr(user, "get_full_name", None)
    value = getter().strip() if callable(getter) else ""
    return value or getattr(user, "email", "") or str(user)


def _student(school, value):
    try:
        item = Student.objects.get(id=value, school=school)
    except (Student.DoesNotExist, ValueError, TypeError):
        raise Rejected("That student does not exist in this school.")
    return item


def _loaded(school, external_id, *, lock=False):
    query = BadDebtClassification.objects.select_related("student", "classified_by__user", "resolved_by__user")
    if lock:
        query = query.select_for_update()
    item = query.filter(school=school, external_id=external_id).first()
    if item is None:
        raise Rejected("This bad debt classification does not exist.")
    return item


def _next_revision(item: BadDebtClassification) -> int:
    last = item.events.first()
    return (last.revision + 1) if last else 1


def _append_event(item: BadDebtClassification, *, actor, action, detail=None):
    BadDebtEvent.objects.create(
        classification=item,
        revision=_next_revision(item),
        action=action,
        actor_membership=actor,
        detail=detail or {},
    )


def serialize_classification(item: BadDebtClassification) -> dict:
    return {
        "id": item.external_id,
        "studentId": str(item.student_id),
        "studentName": f"{item.student.first_name} {item.student.surname}".strip(),
        "status": item.status,
        "outstandingAmountMinor": item.outstanding_amount_minor,
        "currentCanonicalBalanceMinor": item.current_canonical_balance_minor,
        "reason": item.reason,
        "notes": item.notes,
        "evidenceReference": item.evidence_reference,
        "classifiedByMembershipId": str(item.classified_by_id),
        "classifiedByName": _membership_name(item.classified_by),
        "classifiedAt": item.classified_at.isoformat(),
        "lastUpdatedByMembershipId": str(item.last_updated_by_id) if item.last_updated_by_id else None,
        "resolvedByMembershipId": str(item.resolved_by_id) if item.resolved_by_id else None,
        "resolvedAt": item.resolved_at.isoformat() if item.resolved_at else None,
        "resolutionNote": item.resolution_note,
        "publishedToTransferVerify": item.is_published,
        "publishedByMembershipId": str(item.published_by_id) if item.published_by_id else None,
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
        "publicationReason": item.publication_reason,
        "publicationNote": item.publication_note,
        # Always empty until the association-membership phase exists to
        # choose from - never guessed or defaulted to "every association".
        "associationScope": item.association_scope,
        "version": item.updated_at.isoformat(),
    }


def _sync_record(*, school, entity_type, entity_id, payload, actor=None):
    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=school, entity_type=entity_type, entity_id=entity_id)
        .first()
    )
    if record is None:
        return SyncRecord.objects.create(
            school=school, entity_type=entity_type, entity_id=entity_id,
            payload=payload, version=1, deleted=False, updated_by=actor,
        )
    if record.payload == payload and not record.deleted:
        return record
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])
    return record


def replace_current_sync_payload(*, school, entity_type, entity_id, payload, actor=None):
    SyncRecord.objects.filter(school=school, entity_type=entity_type, entity_id=entity_id).update(
        payload=payload, deleted=False, updated_by=actor
    )


def publish_classification_sync(item: BadDebtClassification, *, actor=None):
    item = _loaded(item.school, item.external_id)
    return _sync_record(
        school=item.school,
        entity_type=BAD_DEBT_ENTITY,
        entity_id=item.external_id,
        payload=serialize_classification(item),
        actor=actor,
    )


@transaction.atomic
def classify(*, membership: Membership, payload: dict, publish_sync: bool = True) -> BadDebtClassification:
    """Opens a new classification. There must be no other open (unresolved)
    classification for this student - resolve the existing one first, so a
    student's bad-debt history is always a single straight line, never a
    fork."""
    _assert_classify_authority(membership)
    school = membership.school
    external_id = payload["id"]
    if BadDebtClassification.objects.filter(school=school, external_id=external_id).exists():
        raise Rejected("A classification with this id already exists.")
    student = _student(school, payload["studentId"])
    if BadDebtClassification.objects.filter(student=student, status__in=_OPEN_STATUSES).exists():
        raise Rejected("This student already has an open bad debt classification. Resolve it before opening another.")

    now = timezone.now()
    item = BadDebtClassification.objects.create(
        school=school,
        external_id=external_id,
        student=student,
        status=payload.get("status") or BadDebtStatus.OUTSTANDING,
        outstanding_amount_minor=payload["outstandingAmountMinor"],
        reason=payload.get("reason", ""),
        notes=payload.get("notes", ""),
        evidence_reference=payload.get("evidenceReference", ""),
        classified_by=membership,
        classified_at=now,
    )
    _append_event(item, actor=membership, action="classified", detail={"status": item.status})
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item


@transaction.atomic
def update_classification(*, membership: Membership, external_id: str, payload: dict, publish_sync: bool = True) -> BadDebtClassification:
    """Edits the details of a still-open classification (amount, reason,
    notes, evidence) without changing its status."""
    _assert_classify_authority(membership)
    item = _loaded(membership.school, external_id, lock=True)
    if item.status == BadDebtStatus.RESOLVED:
        raise Rejected("This classification is resolved and cannot be edited. Open a new one instead.")
    if item.is_published:
        raise Rejected("This case has been published to TransferVerify. Withdraw the publication before editing it.")

    item.outstanding_amount_minor = payload.get("outstandingAmountMinor", item.outstanding_amount_minor)
    item.reason = payload.get("reason", item.reason)
    item.notes = payload.get("notes", item.notes)
    item.evidence_reference = payload.get("evidenceReference", item.evidence_reference)
    item.last_updated_by = membership
    item.save(update_fields=["outstanding_amount_minor", "reason", "notes", "evidence_reference", "last_updated_by", "updated_at"])
    _append_event(item, actor=membership, action="updated")
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item


@transaction.atomic
def advance_status(*, membership: Membership, external_id: str, status: str, publish_sync: bool = True) -> BadDebtClassification:
    """Moves a classification forward: outstanding -> recovery_in_progress ->
    bad_debt. Only BAD_DEBT status will ever be eligible for the (later,
    separate) Proprietor publish-to-TransferVerify action."""
    _assert_classify_authority(membership)
    item = _loaded(membership.school, external_id, lock=True)
    if item.status == BadDebtStatus.RESOLVED:
        raise Rejected("This classification is already resolved.")
    if status not in _STATUS_ORDER:
        raise Rejected("That is not a status this action can move to.")
    if _STATUS_ORDER.index(status) <= _STATUS_ORDER.index(item.status):
        raise Rejected("A classification can only move forward, never back to an earlier status.")

    item.status = status
    item.last_updated_by = membership
    item.save(update_fields=["status", "last_updated_by", "updated_at"])
    _append_event(item, actor=membership, action="status_advanced", detail={"status": status})
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item


@transaction.atomic
def resolve(*, membership: Membership, external_id: str, note: str = "", publish_sync: bool = True) -> BadDebtClassification:
    """Closes a classification - the family paid, the balance was corrected,
    or the school otherwise considers the matter settled. Never deleted;
    resolved is a terminal, auditable state (mirrors how a published
    TransferAlert is later resolved, not removed)."""
    _assert_classify_authority(membership)
    item = _loaded(membership.school, external_id, lock=True)
    if item.status == BadDebtStatus.RESOLVED:
        return item

    now = timezone.now()
    item.status = BadDebtStatus.RESOLVED
    item.resolved_by = membership
    item.resolved_at = now
    item.resolution_note = note
    item.last_updated_by = membership
    item.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note", "last_updated_by", "updated_at"])
    _append_event(item, actor=membership, action="resolved", detail={"note": note} if note else {})
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item


def _validated_association_scope(school, association_ids) -> list[str]:
    """Only associations this school currently has an ACTIVE membership in -
    never guessed, never defaulted to "every association this school has
    ever touched"."""
    if not association_ids:
        return []
    unique = sorted({str(value) for value in association_ids})
    allowed = active_association_ids(school)
    unknown = [value for value in unique if value not in allowed]
    if unknown:
        raise Rejected("Choose only associations this school is an active member of.")
    return unique


@transaction.atomic
def publish_to_transferverify(
    *, membership: Membership, external_id: str, reason: str, note: str = "",
    association_ids: list[str] | None = None, publish_sync: bool = True,
) -> BadDebtClassification:
    """The one action that turns a school's private classification into
    something TransferVerify is meant to eventually make discoverable to
    other schools - Proprietor-only, requires explicit confirmation from the
    caller (the review screen showing student/guardian/amount/reason before
    this is called), and only ever from a BAD_DEBT-status case. association_ids
    may be left empty (a school with no association yet still proves the
    authority chain); given, every one of them must be an association this
    school is an ACTIVE member of - see apps.transferverify.associations."""
    _assert_publish_authority(membership)
    item = _loaded(membership.school, external_id, lock=True)
    if item.status != BadDebtStatus.BAD_DEBT:
        raise Rejected("Only a case classified as bad debt can be published to TransferVerify.")
    if item.is_published:
        raise Rejected("This case has already been published to TransferVerify.")
    if reason not in PublicationReason.values:
        raise Rejected("Choose a reason for publishing this case.")
    scope = _validated_association_scope(membership.school, association_ids)

    item.published_at = timezone.now()
    item.published_by = membership
    item.publication_reason = reason
    item.publication_note = note
    item.association_scope = scope
    item.save(
        update_fields=[
            "published_at", "published_by", "publication_reason", "publication_note", "association_scope", "updated_at",
        ]
    )
    _append_event(item, actor=membership, action="published", detail={"reason": reason, "associationScope": scope})
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item


@transaction.atomic
def withdraw_publication(*, membership: Membership, external_id: str, publish_sync: bool = True) -> BadDebtClassification:
    """The owner pulls a published case back - it stops being eligible for
    any future TransferVerify discovery. The classification itself, and its
    full history, is untouched; only the publication is undone."""
    _assert_publish_authority(membership)
    item = _loaded(membership.school, external_id, lock=True)
    if not item.is_published:
        raise Rejected("This case has not been published to TransferVerify.")

    item.published_at = None
    item.published_by = None
    item.publication_reason = ""
    item.publication_note = ""
    item.save(update_fields=["published_at", "published_by", "publication_reason", "publication_note", "updated_at"])
    _append_event(item, actor=membership, action="publication_withdrawn")
    if publish_sync:
        publish_classification_sync(item, actor=membership)
    return item
