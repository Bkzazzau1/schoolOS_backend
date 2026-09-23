"""Email verification for SchoolOS accounts.

A short numeric code is convenient across Android, Windows and tablets. Only a
keyed digest is stored, codes expire quickly, attempts are bounded, and mail
errors never invalidate an otherwise successful account registration.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

CODE_TTL = timedelta(minutes=15)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_ATTEMPTS = 5


def _digest(user_id, code: str) -> str:
    material = f"{user_id}:{code}".encode("utf-8")
    key = settings.SECRET_KEY.encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _send_code_email(user, code: str) -> bool:
    subject = "Verify your SchoolOS email"
    body = (
        f"Hello {user.first_name or 'there'},\n\n"
        f"Your SchoolOS verification code is: {code}\n\n"
        "The code expires in 15 minutes. If you did not request this, you can ignore this email.\n"
    )
    try:
        delivered = EmailMultiAlternatives(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        ).send()
        return delivered > 0
    except Exception:  # noqa: BLE001 - signup remains valid if mail is temporarily unavailable
        return False


def issue_email_verification(user, *, enforce_cooldown: bool = False) -> dict:
    """Replace the current verification code and try to send it."""

    if user.email_verified_at is not None:
        return {"verified": True, "sent": False, "expiresAt": None}

    now = timezone.now()
    if (
        enforce_cooldown
        and user.email_verification_sent_at is not None
        and user.email_verification_sent_at + RESEND_COOLDOWN > now
    ):
        raise ValidationError(
            {"message": "A verification code was sent recently. Please wait a moment before requesting another."}
        )

    code = _new_code()
    expires_at = now + CODE_TTL
    user.email_verification_code_hash = _digest(user.id, code)
    user.email_verification_expires_at = expires_at
    user.email_verification_attempts = 0
    user.save(
        update_fields=[
            "email_verification_code_hash",
            "email_verification_expires_at",
            "email_verification_attempts",
        ]
    )

    sent = _send_code_email(user, code)
    # Only a successfully delivered attempt starts the short resend cooldown.
    # If SMTP is unavailable, the owner can retry immediately (subject to the
    # endpoint's broader per-user rate limit).
    user.email_verification_sent_at = now if sent else None
    user.save(update_fields=["email_verification_sent_at"])

    return {
        "verified": False,
        "sent": sent,
        "expiresAt": expires_at.isoformat(),
    }


def confirm_email_verification(user, code) -> dict:
    """Verify one code, locking the account so attempts cannot race.

    Incorrect-attempt state is committed before the validation error is raised;
    otherwise raising inside an atomic block would roll the counter back.
    """

    if not isinstance(code, str) or len(code.strip()) != 6 or not code.strip().isdigit():
        raise ValidationError({"message": "Enter the 6-digit verification code."})

    error_message = None
    verified_at = None

    with transaction.atomic():
        locked_user = type(user).objects.select_for_update().get(pk=user.pk)
        if locked_user.email_verified_at is not None:
            verified_at = locked_user.email_verified_at
        else:
            now = timezone.now()
            if (
                not locked_user.email_verification_code_hash
                or locked_user.email_verification_expires_at is None
                or locked_user.email_verification_expires_at <= now
            ):
                error_message = "This verification code has expired. Request a new code."
            elif locked_user.email_verification_attempts >= MAX_ATTEMPTS:
                error_message = "Too many incorrect attempts. Request a new verification code."
            else:
                locked_user.email_verification_attempts += 1
                expected = _digest(locked_user.id, code.strip())
                if not hmac.compare_digest(
                    expected,
                    locked_user.email_verification_code_hash,
                ):
                    locked_user.save(update_fields=["email_verification_attempts"])
                    remaining = MAX_ATTEMPTS - locked_user.email_verification_attempts
                    error_message = (
                        "Too many incorrect attempts. Request a new verification code."
                        if remaining <= 0
                        else f"That verification code is not correct. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
                    )
                else:
                    locked_user.email_verified_at = now
                    locked_user.email_verification_code_hash = ""
                    locked_user.email_verification_expires_at = None
                    locked_user.email_verification_attempts = 0
                    locked_user.save(
                        update_fields=[
                            "email_verified_at",
                            "email_verification_code_hash",
                            "email_verification_expires_at",
                            "email_verification_attempts",
                        ]
                    )
                    verified_at = now

    if error_message is not None:
        raise ValidationError({"message": error_message})
    return {
        "verified": True,
        "verifiedAt": verified_at.isoformat() if verified_at is not None else None,
    }
