"""Sending the invitation email.

It is sent from the school's own official address, and contains the school, the
person's name and role, who invited them, when the link expires, and the link. It
never contains a salary, a NIN or anything else personal.

The link is never logged or stored. A failed send is recorded as an event (with the
kind of error, not the link), so the owner can see it and resend.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone

from apps.domains.services import link_host
from apps.staff.constants import DIRECTORY, SYSTEM_ROLES
from apps.sync import records

from .models import InvitationEvent, StaffInvitation


class NotConfiguredBackend(BaseEmailBackend):
    """Used in production until EMAIL_URL is set, so nothing is silently dropped."""

    def send_messages(self, email_messages):
        raise ImproperlyConfigured("Email is not configured. Set EMAIL_URL.")


class LinkUnavailable(Exception):
    """The school has no web address to put in the link."""


def link_url(school, token: str) -> str:
    host = link_host(school) or settings.INVITATION_FALLBACK_HOST
    if not host:
        raise LinkUnavailable("This school has no web address yet.")
    return f"{settings.INVITATION_LINK_SCHEME}://{host}/invite/{token}/"


def sender_for(school) -> str:
    if school.official_email:
        name = school.official_sender_name or school.name
        return f'"{name}" <{school.official_email}>'
    return settings.DEFAULT_FROM_EMAIL


def _compose(invitation: StaffInvitation, url: str) -> tuple[str, str]:
    school = invitation.school
    directory = records.read(school, DIRECTORY, invitation.staff_id) or {}
    name = directory.get("name", "there")
    role = SYSTEM_ROLES.get(invitation.role, "staff member")
    expires = timezone.localtime(invitation.expires_at).strftime("%d %B %Y")
    subject = f"Welcome to {school.name}: complete your registration"
    body = (
        f"Hello {name},\n\n"
        f"{school.name} has appointed you as {role}. Please complete your registration: "
        "add your details, your bank account for salary, and the documents we need.\n\n"
        f"Open this link to start:\n{url}\n\n"
        f"It works until {expires}. If you have the SchoolOS app it opens there; "
        "otherwise it opens a page in your browser.\n\n"
        "If you were not expecting this, you can ignore this email.\n"
    )
    return subject, body


def send_invitation(invitation_id, token: str) -> None:
    """Email the link. Records `sent` or `failed`; never raises."""
    invitation = StaffInvitation.objects.select_related("school").filter(id=invitation_id).first()
    if invitation is None or not invitation.is_usable:
        return
    try:
        subject, body = _compose(invitation, link_url(invitation.school, token))
        EmailMultiAlternatives(subject, body, sender_for(invitation.school), [invitation.email]).send()
    except Exception as error:  # noqa: BLE001 - recorded, and the owner can resend
        InvitationEvent.objects.create(
            invitation=invitation, event="failed", detail={"error": type(error).__name__, "message": str(error)[:200]}
        )
        return
    invitation.sent_at = timezone.now()
    invitation.save(update_fields=["sent_at"])
    InvitationEvent.objects.create(invitation=invitation, event="sent")
