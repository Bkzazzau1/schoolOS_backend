import re
import uuid

from django.db import IntegrityError, transaction

from .models import LoginIdentity, LoginIdentityKind, User


class LoginIdentityConflict(Exception):
    pass


def normalize_parent_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("234") and len(digits) == 13:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 11:
        return f"+234{digits[1:]}"
    if len(digits) == 10 and digits[:1] in {"7", "8", "9"}:
        return f"+234{digits}"
    return None


def normalize_student_admission(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).upper()


def normalize_login_identity(kind: str, value: str | None) -> str:
    if kind == LoginIdentityKind.PARENT_PHONE:
        normalized = normalize_parent_phone(value)
        return normalized or ""
    if kind == LoginIdentityKind.STUDENT_ADMISSION:
        return normalize_student_admission(value)
    return (value or "").strip()


def resolve_login_user(identifier: str | None):
    raw = (identifier or "").strip()
    if not raw:
        return None

    if "@" in raw:
        user = User.objects.filter(email__iexact=raw).first()
        if user is not None:
            return user

    phone = normalize_parent_phone(raw)
    if phone:
        identity = (
            LoginIdentity.objects.select_related("user")
            .filter(
                kind=LoginIdentityKind.PARENT_PHONE,
                normalized_identifier=phone,
            )
            .first()
        )
        if identity is not None:
            return identity.user

    admission = normalize_student_admission(raw)
    if admission:
        identity = (
            LoginIdentity.objects.select_related("user")
            .filter(
                kind=LoginIdentityKind.STUDENT_ADMISSION,
                normalized_identifier=admission,
            )
            .first()
        )
        if identity is not None:
            return identity.user
    return None


@transaction.atomic
def bind_login_identity(*, user: User, kind: str, identifier: str) -> LoginIdentity:
    normalized = normalize_login_identity(kind, identifier)
    if not normalized:
        raise LoginIdentityConflict("The login identifier is not valid.")

    current = (
        LoginIdentity.objects.select_for_update()
        .filter(kind=kind, normalized_identifier=normalized)
        .first()
    )
    if current is not None:
        if current.user_id != user.id:
            raise LoginIdentityConflict("That login identifier already belongs to another account.")
        return current

    try:
        with transaction.atomic():
            return LoginIdentity.objects.create(
                user=user,
                kind=kind,
                identifier=identifier.strip(),
                normalized_identifier=normalized,
            )
    except IntegrityError:
        current = LoginIdentity.objects.select_related("user").get(
            kind=kind,
            normalized_identifier=normalized,
        )
        if current.user_id != user.id:
            raise LoginIdentityConflict("That login identifier already belongs to another account.")
        return current


def _internal_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}@accounts.schoolos.invalid"


def create_bootstrap_user(
    *,
    first_name: str,
    last_name: str,
    initial_password: str,
    prefix: str,
    preferred_email: str = "",
) -> User:
    """Create a school-provisioned account without exposing synthetic email.

    A guardian's real email is reused only when it is free. Student accounts do
    not require email. The generated `.invalid` address is an internal database
    key and is never presented as the person's sign-in identity.
    """

    clean_email = preferred_email.strip().lower()
    if clean_email and User.objects.filter(email__iexact=clean_email).exists():
        clean_email = ""
    user = User.objects.create_user(
        email=clean_email or _internal_email(prefix),
        password=initial_password,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        must_change_password=True,
    )
    return user
