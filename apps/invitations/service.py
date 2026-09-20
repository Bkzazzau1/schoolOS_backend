"""Making, revoking and describing invitations."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.validation import is_email

from . import mail
from .models import InvitationEvent, StaffInvitation, StaffLink
from .tokens import hash_token, new_token


class InvitationError(Exception):
    """Something the caller can fix or should be told, with a stable code."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def record(invitation, event: str, request=None, **detail) -> None:
    InvitationEvent.objects.create(
        invitation=invitation, event=event, detail=detail,
        ip=(request.META.get("REMOTE_ADDR") or None) if request is not None else None,
        user_agent=((request.META.get("HTTP_USER_AGENT") or "")[:200]) if request is not None else "",
    )


def create_invitation(school, staff_id: str, email: str, role: str, *, created_by=None) -> tuple[StaffInvitation, str]:
    """Send a person their registration link.

    Any earlier pending invitation for the same staff record is revoked first, so
    only the newest link works. Returns the invitation and the link's secret; the
    secret exists only in memory and in the email that is sent once the transaction
    commits.
    """
    email = (email or "").strip().lower()
    if not is_email(email):
        raise InvitationError("invalid_email", "Enter a valid email address for the staff member.")
    if StaffLink.objects.filter(school=school, staff_id=staff_id).exists():
        raise InvitationError("already_linked", "This staff member already has a login.", 409)
    token = new_token()
    with transaction.atomic():
        for old in StaffInvitation.objects.select_for_update().filter(
            school=school, staff_id=staff_id, status=StaffInvitation.Status.PENDING
        ):
            old.status = StaffInvitation.Status.REVOKED
            old.save(update_fields=["status"])
            record(old, "revoked", reason="replaced by a newer invitation")
        invitation = StaffInvitation.objects.create(
            school=school, staff_id=staff_id, email=email, role=role or "staff",
            token_hash=hash_token(token), created_by=created_by,
            expires_at=timezone.now() + timedelta(days=settings.INVITATION_TTL_DAYS),
        )
        record(invitation, "created")
        # Sent after the transaction commits, so a rolled-back change never emails a link.
        transaction.on_commit(lambda: mail.send_invitation(invitation.id, token))
    return invitation, token


def revoke(school, staff_id: str, *, by=None) -> bool:
    """Cancel the pending invitation. False if there was none."""
    with transaction.atomic():
        pending = StaffInvitation.objects.select_for_update().filter(
            school=school, staff_id=staff_id, status=StaffInvitation.Status.PENDING
        ).first()
        if pending is None:
            return False
        pending.status = StaffInvitation.Status.REVOKED
        pending.save(update_fields=["status"])
        record(pending, "revoked", by=str(by.id) if by else None)
        return True


def describe(school, staff_id: str) -> dict:
    """The newest invitation for a staff record, for the owner. Never the link."""
    latest = StaffInvitation.objects.filter(school=school, staff_id=staff_id).first()
    linked = StaffLink.objects.filter(school=school, staff_id=staff_id).exists()
    if latest is None:
        return {"status": "none", "linked": linked}
    status = latest.status
    if status == StaffInvitation.Status.PENDING and latest.is_expired:
        status = "expired"
    last = latest.events.last()
    return {
        "status": status,
        "linked": linked,
        "email": latest.email,
        "sentAt": latest.sent_at.isoformat() if latest.sent_at else None,
        "expiresAt": latest.expires_at.isoformat(),
        "acceptedAt": latest.accepted_at.isoformat() if latest.accepted_at else None,
        "lastEvent": {"event": last.event, "at": last.at.isoformat(), "detail": last.detail} if last else None,
    }
