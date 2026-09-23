"""Following a link: what it opens, and accepting it.

Accepting is one transaction. Either the account, the membership, the link between
the login and the staff record, the staff record's `linkedMembershipId`, the
authority the owner assigned, and the invitation's own state all change together,
or none of them do.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.notifications.services import notify_many
from apps.schools.models import Membership
from apps.staff.constants import AUTHORIZER, DIRECTORY, PROFILE
from apps.sync import records
from apps.sync.models import SyncRecord

from .models import StaffInvitation, StaffLink
from .service import InvitationError, record
from .tokens import hash_token, mask_email

User = get_user_model()
INVALID = ("invitation_invalid", "This link is not valid.", 404)
JOB = "owner_job_assignment"


def find(token: str, *, request_school=None, lock: bool = False) -> StaffInvitation:
    """The invitation for this link, or an error.

    Unknown, expired, revoked and wrong-school links all give the same 404, so
    nobody can tell them apart or probe for valid links. A link that was already
    used says so (409).
    """
    query = StaffInvitation.objects.select_related("school")
    invitation = (query.select_for_update() if lock else query).filter(token_hash=hash_token(token)).first()
    if invitation is None:
        raise InvitationError(*INVALID)
    # On a school's own web address, only that school's links work.
    if request_school is not None and request_school.id != invitation.school_id:
        raise InvitationError(*INVALID)
    if invitation.status == StaffInvitation.Status.ACCEPTED:
        raise InvitationError("already_accepted", "This invitation was already used. Sign in instead.", 409)
    if not invitation.is_usable:
        raise InvitationError(*INVALID)
    return invitation


def account_exists(email: str) -> bool:
    return User.objects.filter(email__iexact=email).exists()


def preview(token: str, *, request_school=None, request=None) -> dict:
    invitation = find(token, request_school=request_school)
    record(invitation, "previewed", request)
    directory = records.read(invitation.school, DIRECTORY, invitation.staff_id) or {}
    return {
        "schoolName": invitation.school.name,
        "staffName": directory.get("name", ""),
        "email": mask_email(invitation.email),
        "expiresAt": invitation.expires_at.isoformat(),
        "accountExists": account_exists(invitation.email),
    }


def accept(token: str, *, signed_in_user=None, password: str = "", first_name: str = "",
           last_name: str = "", request_school=None, request=None) -> dict:
    """Link the person's login to their staff record. Returns the user, membership
    and staff id. Raises InvitationError for anything the person can act on."""
    with transaction.atomic():
        invitation = find(token, request_school=request_school, lock=True)
        school, staff_id = invitation.school, invitation.staff_id
        profile = records.read(school, PROFILE, staff_id)
        if profile is None:
            raise InvitationError(*INVALID)

        user = _resolve_user(invitation, signed_in_user, password, first_name, last_name)
        membership, _ = Membership.objects.get_or_create(
            user=user, school=school, role=invitation.role, defaults={"is_active": True}
        )
        if not membership.is_active:
            membership.is_active = True
            membership.save(update_fields=["is_active"])

        # One login per staff record and one staff record per login, in the database.
        if StaffLink.objects.filter(school=school, staff_id=staff_id).exists() or StaffLink.objects.filter(
            membership=membership
        ).exists():
            raise InvitationError("already_linked", "This staff record or this account is already linked.", 409)
        StaffLink.objects.create(school=school, staff_id=staff_id, membership=membership, linked_via=invitation)

        # The staff record now says whose it is. Only the server ever writes this.
        records.write(school, PROFILE, staff_id, {**profile, "linkedMembershipId": str(membership.id)}, by=None)
        _activate_assignments(school, staff_id, membership)

        invitation.status = StaffInvitation.Status.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.accepted_membership = membership
        invitation.save(update_fields=["status", "accepted_at", "accepted_membership"])
        record(invitation, "accepted", request)

        directory = records.read(school, DIRECTORY, staff_id) or {}
        owners = Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school")
        notify_many(
            owners, "staff_invitation_accepted", "Staff member joined",
            f"{directory.get('name', 'A staff member')} accepted their invitation and can now sign in.",
            {"staffId": staff_id},
        )
    return {"user": user, "membership": membership, "staffId": staff_id}


def _resolve_user(invitation, signed_in_user, password, first_name, last_name):
    email = invitation.email
    if signed_in_user is not None and signed_in_user.is_authenticated:
        # Holding the valid link delivered to this same email, together with an
        # authenticated matching account, proves control of the email address.
        if signed_in_user.email.lower() != email.lower():
            raise InvitationError("wrong_account", "You are signed in as a different account than the one invited.", 403)
        if getattr(signed_in_user, "email_verified_at", None) is None:
            signed_in_user.email_verified_at = timezone.now()
            signed_in_user.save(update_fields=["email_verified_at"])
        return signed_in_user
    if account_exists(email):
        raise InvitationError("sign_in_required", "An account with this email already exists. Sign in to accept.", 401)
    candidate = User(email=email.lower(), first_name=first_name.strip()[:150], last_name=last_name.strip()[:150])
    try:
        validate_password(password, candidate)
    except ValidationError as problem:
        raise InvitationError("invalid_password", "Choose a stronger password.", 400, details=list(problem.messages))
    return User.objects.create_user(
        email,
        password,
        first_name=candidate.first_name,
        last_name=candidate.last_name,
        email_verified_at=timezone.now(),
    )


def _activate_assignments(school, staff_id: str, membership) -> None:
    """Authority and jobs the owner gave this person before they had a login become
    active now, tied to this login."""
    for entity_type, field in ((AUTHORIZER, "staffId"), (JOB, "registeredStaffId")):
        for row in SyncRecord.objects.filter(school=school, entity_type=entity_type, deleted=False):
            p = row.payload
            if p.get(field) == staff_id and p.get("status") == "pendingActivation":
                records.write(school, entity_type, row.entity_id,
                              {**p, "status": "active", "membershipId": str(membership.id)}, by=None)
