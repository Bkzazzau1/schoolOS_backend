"""The association layer: a school's Proprietor requesting to join a
SchoolProprietorAssociation, and an association administrator approving or
suspending member schools. Platform-level (not tenant-scoped) - see the
models' own docstrings for why this is a real Django app surface with a live
REST API rather than a sync entity.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role

from .models import (
    AssociationAdministrator,
    AssociationMembershipStatus,
    AssociationStatus,
    SchoolAssociationMembership,
    SchoolProprietorAssociation,
)


def _assert_proprietor(membership: Membership) -> None:
    if not membership.is_active or membership.role != Role.PROPRIETOR:
        raise Rejected("Only the owner can manage this school's association memberships.")


def _assert_association_admin(user, association: SchoolProprietorAssociation) -> AssociationAdministrator:
    admin = AssociationAdministrator.objects.filter(association=association, user=user, is_active=True).first()
    if admin is None:
        raise Rejected("You are not an administrator of this association.")
    return admin


def _association(association_id) -> SchoolProprietorAssociation:
    try:
        return SchoolProprietorAssociation.objects.get(id=association_id)
    except (SchoolProprietorAssociation.DoesNotExist, ValueError, TypeError):
        raise Rejected("That association does not exist.")


def _membership_row(membership_id) -> SchoolAssociationMembership:
    try:
        return (
            SchoolAssociationMembership.objects.select_related("association", "school")
            .select_for_update()
            .get(id=membership_id)
        )
    except (SchoolAssociationMembership.DoesNotExist, ValueError, TypeError):
        raise Rejected("That membership request does not exist.")


def serialize_association(association: SchoolProprietorAssociation) -> dict:
    return {
        "id": str(association.id),
        "name": association.name,
        "registrationReference": association.registration_reference,
        "geographicScope": association.geographic_scope,
        "description": association.description,
        "status": association.status,
        "isOpenForMembership": association.is_open_for_membership,
    }


def serialize_school_membership(item: SchoolAssociationMembership) -> dict:
    return {
        "id": str(item.id),
        "associationId": str(item.association_id),
        "associationName": item.association.name,
        "schoolId": str(item.school_id),
        "schoolName": item.school.name,
        "status": item.status,
        "requestedByMembershipId": str(item.requested_by_id),
        "requestedAt": item.requested_at.isoformat(),
        "decidedAt": item.decided_at.isoformat() if item.decided_at else None,
        "decisionNote": item.decision_note,
        "suspendedAt": item.suspended_at.isoformat() if item.suspended_at else None,
        "exitedAt": item.exited_at.isoformat() if item.exited_at else None,
    }


def open_associations() -> list[SchoolProprietorAssociation]:
    """The catalog a Proprietor browses to decide where to request joining."""
    return list(SchoolProprietorAssociation.objects.filter(status=AssociationStatus.ACTIVE).order_by("name"))


def _school_memberships_qs(school):
    return SchoolAssociationMembership.objects.select_related("association", "school").filter(school=school)


def school_memberships(school) -> list[SchoolAssociationMembership]:
    return list(_school_memberships_qs(school).order_by("-requested_at"))


def active_association_ids(school) -> set[str]:
    """Every association id this school currently has an ACTIVE membership
    in - the only ids a publish action may put in association_scope."""
    return {
        str(association_id)
        for association_id in _school_memberships_qs(school)
        .filter(status=AssociationMembershipStatus.ACTIVE)
        .values_list("association_id", flat=True)
    }


@transaction.atomic
def request_to_join(*, membership: Membership, association_id: str) -> SchoolAssociationMembership:
    _assert_proprietor(membership)
    association = _association(association_id)
    if not association.is_open_for_membership:
        raise Rejected("This association is not currently accepting members.")

    existing = SchoolAssociationMembership.objects.select_for_update().filter(
        association=association, school=membership.school
    ).first()
    if existing is not None:
        if existing.status in {AssociationMembershipStatus.PENDING, AssociationMembershipStatus.ACTIVE}:
            raise Rejected("This school already has a request or an active membership with this association.")
        # A previously exited/rejected/suspended school may ask again - reopen the same row
        # rather than fight the one-row-per-(association, school) constraint with a new one.
        existing.status = AssociationMembershipStatus.PENDING
        existing.requested_by = membership
        existing.requested_at = timezone.now()
        existing.decided_at = None
        existing.decided_by = None
        existing.decision_note = ""
        existing.suspended_at = None
        existing.exited_at = None
        existing.save()
        return existing

    return SchoolAssociationMembership.objects.create(
        association=association, school=membership.school, requested_by=membership
    )


@transaction.atomic
def exit_membership(*, membership: Membership, membership_id: str) -> SchoolAssociationMembership:
    """The Proprietor voluntarily leaves an association their school belongs
    to - distinct from an association administrator suspending them."""
    _assert_proprietor(membership)
    item = _membership_row(membership_id)
    if item.school_id != membership.school_id:
        raise Rejected("That association membership does not exist for this school.")
    if item.status not in {AssociationMembershipStatus.PENDING, AssociationMembershipStatus.ACTIVE}:
        raise Rejected("This membership has already ended.")
    item.status = AssociationMembershipStatus.EXITED
    item.exited_at = timezone.now()
    item.save(update_fields=["status", "exited_at", "updated_at"])
    return item


def association_members(*, user, association_id: str, status: str | None = None) -> list[SchoolAssociationMembership]:
    association = _association(association_id)
    _assert_association_admin(user, association)
    qs = SchoolAssociationMembership.objects.select_related("school", "association").filter(association=association)
    if status:
        qs = qs.filter(status=status)
    return list(qs.order_by("-requested_at"))


def _membership_row_in(association_id, membership_id) -> SchoolAssociationMembership:
    item = _membership_row(membership_id)
    if str(item.association_id) != str(association_id):
        raise Rejected("That membership request does not belong to this association.")
    return item


@transaction.atomic
def approve_membership(*, user, association_id: str, membership_id: str, note: str = "") -> SchoolAssociationMembership:
    item = _membership_row_in(association_id, membership_id)
    admin = _assert_association_admin(user, item.association)
    if item.status != AssociationMembershipStatus.PENDING:
        raise Rejected("Only a pending request can be approved.")
    item.status = AssociationMembershipStatus.ACTIVE
    item.decided_at = timezone.now()
    item.decided_by = admin
    item.decision_note = note
    item.save(update_fields=["status", "decided_at", "decided_by", "decision_note", "updated_at"])
    return item


@transaction.atomic
def reject_membership(*, user, association_id: str, membership_id: str, note: str = "") -> SchoolAssociationMembership:
    item = _membership_row_in(association_id, membership_id)
    admin = _assert_association_admin(user, item.association)
    if item.status != AssociationMembershipStatus.PENDING:
        raise Rejected("Only a pending request can be rejected.")
    item.status = AssociationMembershipStatus.EXITED
    item.decided_at = timezone.now()
    item.decided_by = admin
    item.decision_note = note
    item.exited_at = timezone.now()
    item.save(update_fields=["status", "decided_at", "decided_by", "decision_note", "exited_at", "updated_at"])
    return item


@transaction.atomic
def suspend_membership(*, user, association_id: str, membership_id: str, note: str = "") -> SchoolAssociationMembership:
    item = _membership_row_in(association_id, membership_id)
    admin = _assert_association_admin(user, item.association)
    if item.status != AssociationMembershipStatus.ACTIVE:
        raise Rejected("Only an active membership can be suspended.")
    item.status = AssociationMembershipStatus.SUSPENDED
    item.suspended_at = timezone.now()
    item.decided_by = admin
    item.decision_note = note
    item.save(update_fields=["status", "suspended_at", "decided_by", "decision_note", "updated_at"])
    return item
