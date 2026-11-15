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

from apps.accounts.identity import create_staff_bootstrap_user
from apps.core.identity import normalize_phone
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
        if records.read(school, PROFILE, staff_id) is None:
            raise InvitationError(*INVALID)

        user = _resolve_user(invitation, signed_in_user, password, first_name, last_name)
        result = _finish_linking(
            school, staff_id, user, invitation.role,
            joined_message="accepted their invitation and can now sign in.",
            linked_via=invitation,
        )

        invitation.status = StaffInvitation.Status.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.accepted_membership = result["membership"]
        invitation.save(update_fields=["status", "accepted_at", "accepted_membership"])
        record(invitation, "accepted", request)
    return result


def provision_staff_login(school, staff_id: str, *, email: str, phone: str, role: str,
                           first_name: str = "", last_name: str = "") -> dict | None:
    """Create a staff member's login immediately, their own phone number as a
    temporary password they must change on first sign-in - the same bootstrap
    rule already used for student/parent accounts, skipping the emailed
    registration link entirely.

    The password is the same local 11-digit form (08031234567) used everywhere
    else a phone number is shown or typed in this app (apps.core.identity.
    normalize_phone - not apps.accounts.identity.normalize_parent_phone's
    +234 form, which nobody would think to type as "their phone number").

    Returns None - the caller should fall back to emailing a registration link
    instead (see apps.invitations.listeners) - when there is nothing to do yet
    (no profile, already linked), or when it would not be safe to create the
    account outright: the phone number does not look like a real Nigerian
    number, or the email already belongs to an existing account (nobody is
    signed in here to prove they own it, unlike accept() above).
    """

    if StaffLink.objects.filter(school=school, staff_id=staff_id).exists():
        return None
    if account_exists(email):
        return None
    normalized_phone = normalize_phone(phone)
    if normalized_phone is None:
        return None
    with transaction.atomic():
        if records.read(school, PROFILE, staff_id) is None:
            return None
        user = create_staff_bootstrap_user(
            email=email, first_name=first_name, last_name=last_name, initial_password=normalized_phone,
        )
        return _finish_linking(school, staff_id, user, role, joined_message="can now sign in.")


def _finish_linking(school, staff_id: str, user, role: str, *, joined_message: str, linked_via=None) -> dict:
    """Shared tail of linking a login to a staff record - the same whether the
    login was just obtained by accepting an emailed link (accept(), above) or
    provisioned directly with a phone-number bootstrap password
    (provision_staff_login(), above)."""

    membership, _ = Membership.objects.get_or_create(
        user=user, school=school, role=role, defaults={"is_active": True}
    )
    if not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active"])

    # One login per staff record and one staff record per login, in the database.
    if StaffLink.objects.filter(school=school, staff_id=staff_id).exists() or StaffLink.objects.filter(
        membership=membership
    ).exists():
        raise InvitationError("already_linked", "This staff record or this account is already linked.", 409)
    StaffLink.objects.create(school=school, staff_id=staff_id, membership=membership, linked_via=linked_via)

    # The staff record now says whose it is. Only the server ever writes this.
    profile = records.read(school, PROFILE, staff_id) or {}
    records.write(school, PROFILE, staff_id, {**profile, "linkedMembershipId": str(membership.id)}, by=None)
    _activate_assignments(school, staff_id, membership)

    directory = records.read(school, DIRECTORY, staff_id) or {}
    owners = Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school")
    notify_many(
        owners, "staff_invitation_accepted", "Staff member joined",
        f"{directory.get('name', 'A staff member')} {joined_message}",
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
            signed_in_user.email_verification_code_hash = ""
            signed_in_user.email_verification_expires_at = None
            signed_in_user.email_verification_sent_at = None
            signed_in_user.email_verification_attempts = 0
            signed_in_user.save(
                update_fields=[
                    "email_verified_at",
                    "email_verification_code_hash",
                    "email_verification_expires_at",
                    "email_verification_sent_at",
                    "email_verification_attempts",
                ]
            )
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
