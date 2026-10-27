"""Forgotten-password recovery for email-login (Staff/Proprietor) accounts.

A Student or Parent account signs in with an admission ID or phone number and
has no verified channel of its own, so it recovers through the school office
(see ``services.request_recovery``). An account whose login identity is an
email address has a channel of its own: a short code sent to that same
address, the same shape email verification already uses
(``apps.accounts.email_verification``). Only a keyed digest is stored, codes
expire quickly, and attempts are bounded.

The request step never discloses whether an account exists, and never fires
for a Student/Parent login identity: that recovery stays school-office-only,
on purpose, even if the underlying account also happens to carry an email
address.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import LoginIdentity, User
from apps.schools.models import Membership

from .models import CredentialAuditEvent

CODE_TTL = timedelta(minutes=15)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_ATTEMPTS = 5


def _digest(user_id, code: str) -> str:
    material = f"{user_id}:{code}".encode("utf-8")
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _is_email_login_account(user: User) -> bool:
    """True for a Staff/Proprietor-style account: active, and never provisioned
    with a Student admission or Parent phone login identity."""

    return user.is_active and not LoginIdentity.objects.filter(user=user).exists()


def _send_code_email(user: User, code: str) -> bool:
    subject = "Reset your SchoolOS password"
    body = (
        f"Hello {user.first_name or 'there'},\n\n"
        f"Your SchoolOS password reset code is: {code}\n\n"
        "The code expires in 15 minutes. If you did not request this, you can "
        "ignore this email; your password will not change.\n"
    )
    try:
        delivered = EmailMultiAlternatives(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        ).send()
        return delivered > 0
    except Exception:  # noqa: BLE001 - the request endpoint never discloses mail failures
        return False


def _audit_every_school(user: User, event: str, detail=None) -> None:
    school_ids = Membership.objects.filter(user=user, is_active=True).values_list(
        "school_id", flat=True
    )
    for school_id in school_ids:
        CredentialAuditEvent.objects.create(
            school_id=school_id, user=user, actor=None, event=event, detail=detail or {}
        )


def request_password_reset(email) -> None:
    """Email a code to a real email-login account. Always returns silently."""

    raw = (email or "").strip().casefold()[:160]
    if not raw:
        return
    user = User.objects.filter(email=raw).first()
    if user is None or not _is_email_login_account(user):
        return

    now = timezone.now()
    if user.password_reset_sent_at is not None and user.password_reset_sent_at + RESEND_COOLDOWN > now:
        return

    code = _new_code()
    user.password_reset_code_hash = _digest(user.id, code)
    user.password_reset_expires_at = now + CODE_TTL
    user.password_reset_attempts = 0
    user.save(
        update_fields=[
            "password_reset_code_hash",
            "password_reset_expires_at",
            "password_reset_attempts",
        ]
    )

    sent = _send_code_email(user, code)
    # Only a successfully delivered attempt starts the short resend cooldown, so
    # the person can try again right away if mail is temporarily unavailable.
    user.password_reset_sent_at = now if sent else None
    user.save(update_fields=["password_reset_sent_at"])
    if sent:
        _audit_every_school(user, "password_reset_requested")


def confirm_password_reset(email, code, new_password) -> dict:
    """Verify one code and set the new password, locking the row so attempts
    cannot race. Incorrect-attempt state is committed before the validation
    error is raised; otherwise raising inside an atomic block would roll the
    counter back."""

    raw_email = (email or "").strip().casefold()[:160]
    if not raw_email:
        raise ValidationError({"message": "Enter the email address the code was sent to."})
    if not isinstance(code, str) or len(code.strip()) != 6 or not code.strip().isdigit():
        raise ValidationError({"message": "Enter the 6-digit reset code."})
    if not isinstance(new_password, str) or not new_password:
        raise ValidationError({"newPassword": "Enter a new password."})

    # Built up inside the atomic block below, then raised (if anything failed)
    # only after it exits, so a counter increment is never rolled back by the
    # very failure it is meant to record.
    errors: dict | None = None
    reset_at = None
    reset_user = None

    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=raw_email).first()
        if user is None or not _is_email_login_account(user):
            # Same message as a wrong code: never discloses whether the account exists.
            errors = {"message": "That reset code is not correct or has expired."}
        elif (
            not user.password_reset_code_hash
            or user.password_reset_expires_at is None
            or user.password_reset_expires_at <= timezone.now()
        ):
            errors = {"message": "That reset code is not correct or has expired."}
        elif user.password_reset_attempts >= MAX_ATTEMPTS:
            errors = {"message": "Too many incorrect attempts. Request a new reset code."}
        else:
            now = timezone.now()
            user.password_reset_attempts += 1
            expected = _digest(user.id, code.strip())
            if not hmac.compare_digest(expected, user.password_reset_code_hash):
                user.save(update_fields=["password_reset_attempts"])
                remaining = MAX_ATTEMPTS - user.password_reset_attempts
                errors = {
                    "message": (
                        "Too many incorrect attempts. Request a new reset code."
                        if remaining <= 0
                        else f"That reset code is not correct. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
                    )
                }
            else:
                try:
                    validate_password(new_password, user=user)
                except DjangoValidationError as exc:
                    user.save(update_fields=["password_reset_attempts"])
                    errors = {"newPassword": list(exc.messages)}
                else:
                    user.set_password(new_password)
                    user.must_change_password = False
                    user.credential_version += 1
                    user.password_reset_code_hash = ""
                    user.password_reset_expires_at = None
                    user.password_reset_sent_at = None
                    user.password_reset_attempts = 0
                    user.save(
                        update_fields=[
                            "password",
                            "must_change_password",
                            "credential_version",
                            "password_reset_code_hash",
                            "password_reset_expires_at",
                            "password_reset_sent_at",
                            "password_reset_attempts",
                        ]
                    )
                    reset_at = now
                    reset_user = user

    if errors is not None:
        raise ValidationError(errors)
    _audit_every_school(reset_user, "password_reset_completed")
    return {"reset": True, "resetAt": reset_at.isoformat()}
