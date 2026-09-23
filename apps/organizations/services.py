import uuid

from django.db import transaction
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.schools.models import Membership, Role, School, SchoolType

from .models import (
    Organization,
    OrganizationAuditEvent,
    OrganizationMembership,
    OrganizationRole,
)


def _unique_slug(value: str, *, fallback: str) -> str:
    """Return a human-readable slug with enough entropy for concurrent creates.

    Both Organization.slug and School.slug use Django's default 50-character
    SlugField, so the readable prefix is capped before the random suffix is
    appended.
    """

    base = slugify(value).strip("-") or fallback
    base = base[:40].rstrip("-")
    return f"{base}-{uuid.uuid4().hex[:8]}"


def serialize_school_membership(membership: Membership) -> dict:
    school = membership.school
    return {
        "id": str(membership.id),
        "schoolId": str(school.id),
        "schoolName": school.name,
        "role": membership.role,
        "organizationId": str(school.organization_id) if school.organization_id else None,
    }


def serialize_organization_membership(membership: OrganizationMembership) -> dict:
    return {
        "id": str(membership.id),
        "organizationId": str(membership.organization_id),
        "organizationName": membership.organization.name,
        "role": membership.role,
    }


def _has_managed_school(actor) -> bool:
    return OrganizationMembership.objects.filter(
        user=actor,
        is_active=True,
        organization__is_active=True,
        organization__schools__is_active=True,
    ).exists()


@transaction.atomic
def create_organization(*, actor, name: str) -> tuple[Organization, OrganizationMembership]:
    """Create an account and make the signed-in person its first owner."""

    # Self-service signup creates the first organization before verification.
    # Once an account already owns/administers an organization, verification is
    # required before expanding into another commercial account.
    if (
        getattr(actor, "email_verified_at", None) is None
        and OrganizationMembership.objects.filter(
            user=actor,
            is_active=True,
            organization__is_active=True,
        ).exists()
    ):
        raise PermissionDenied(
            "Verify your email address before creating another organization."
        )

    clean_name = name.strip()
    if len(clean_name) < 2:
        raise ValidationError({"message": "Enter an organization name."})
    if len(clean_name) > 200:
        raise ValidationError({"message": "The organization name is too long."})

    organization = Organization.objects.create(
        name=clean_name,
        slug=_unique_slug(clean_name, fallback="organization"),
        created_by=actor,
    )
    membership = OrganizationMembership.objects.create(
        user=actor,
        organization=organization,
        role=OrganizationRole.OWNER,
    )
    OrganizationAuditEvent.objects.create(
        organization=organization,
        actor=actor,
        action="organization_created",
        target_type="organization",
        target_id=str(organization.id),
        detail={"name": organization.name, "ownerMembershipId": str(membership.id)},
    )

    # Local import keeps the account domain independent at import time while the
    # transaction still guarantees organization + owner + subscription are born
    # together.
    from apps.billing.services import ensure_organization_subscription

    ensure_organization_subscription(organization)
    return organization, membership


@transaction.atomic
def provision_school(
    *,
    actor,
    organization_id,
    name: str,
    school_type: str,
    location: str,
) -> tuple[School, Membership]:
    """Create one isolated school tenant and the creator's proprietor access.

    Authorization is re-read inside the transaction. The school and proprietor
    membership therefore either both exist or neither exists. Access defaults
    do not need rows: SchoolOS stores only per-school overrides, so a new school
    automatically starts from the built-in role defaults.
    """

    try:
        organization = Organization.objects.select_for_update().get(
            id=organization_id,
            is_active=True,
        )
    except Organization.DoesNotExist as exc:
        raise PermissionDenied("You do not have access to this organization.") from exc

    organization_membership = (
        OrganizationMembership.objects.select_for_update()
        .filter(
            user=actor,
            organization=organization,
            is_active=True,
            role__in=[OrganizationRole.OWNER, OrganizationRole.ADMINISTRATOR],
        )
        .first()
    )
    if organization_membership is None:
        raise PermissionDenied(
            "Your account role cannot create schools for this organization."
        )

    # A new owner may create one first school immediately. Once any school exists
    # under an organization they manage, verified email is required before the
    # account expands further, including through another organization.
    if getattr(actor, "email_verified_at", None) is None and _has_managed_school(actor):
        raise PermissionDenied(
            "Verify your email address before creating another school."
        )

    current_school_count = School.objects.filter(
        organization=organization,
        is_active=True,
    ).count()
    from apps.billing.services import require_school_provisioning

    subscription = require_school_provisioning(
        organization,
        current_school_count=current_school_count,
    )

    clean_name = name.strip()
    clean_location = location.strip()
    if len(clean_name) < 3:
        raise ValidationError({"message": "Enter the school name."})
    if len(clean_name) > 200:
        raise ValidationError({"message": "The school name is too long."})
    if school_type not in SchoolType.values:
        raise ValidationError({"message": "Choose a valid school type."})
    if len(clean_location) < 2:
        raise ValidationError({"message": "Enter the school location."})
    if len(clean_location) > 250:
        raise ValidationError({"message": "The school location is too long."})

    school = School.objects.create(
        organization=organization,
        name=clean_name,
        slug=_unique_slug(clean_name, fallback="school"),
        school_type=school_type,
        location=clean_location,
    )
    proprietor_membership = Membership.objects.create(
        user=actor,
        school=school,
        role=Role.PROPRIETOR,
    )

    OrganizationAuditEvent.objects.create(
        organization=organization,
        actor=actor,
        action="school_created",
        target_type="school",
        target_id=str(school.id),
        detail={
            "schoolName": school.name,
            "schoolType": school.school_type,
            "location": school.location,
            "proprietorMembershipId": str(proprietor_membership.id),
            "organizationMembershipId": str(organization_membership.id),
            "subscriptionId": str(subscription.id),
            "planCode": subscription.plan.code if subscription.plan_id else None,
        },
    )

    return school, proprietor_membership
