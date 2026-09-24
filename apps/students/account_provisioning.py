import re

from django.db import transaction

from apps.accounts.identity import (
    LoginIdentityConflict,
    bind_login_identity,
    create_bootstrap_user,
    normalize_parent_phone,
    normalize_student_admission,
)
from apps.accounts.models import LoginIdentity, LoginIdentityKind, User
from apps.core.errors import Rejected
from apps.schools.models import Membership, Role

from .models import GuardianLink, Student, StudentRegistration
from .parent_sync import publish_parent_family_link
from .student_sync import publish_student_class_link


_NAME_TITLES = {
    "alhaji",
    "hajiya",
    "hajia",
    "mallam",
    "malam",
    "mr",
    "mrs",
    "miss",
    "ms",
    "dr",
    "engr",
    "engineer",
    "prof",
    "professor",
}


def _name_parts(value: str) -> list[str]:
    parts = [part for part in value.strip().split() if part]
    while parts and re.sub(r"[^a-z]", "", parts[0].casefold()) in _NAME_TITLES:
        parts.pop(0)
    return parts


def _split_person_name(value: str) -> tuple[str, str]:
    parts = _name_parts(value)
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _normalized_person_name(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9]", "", part.casefold())
        for part in _name_parts(value)
        if re.sub(r"[^a-z0-9]", "", part.casefold())
    )


def _user_name(user: User) -> str:
    return " ".join(value for value in [user.first_name, user.last_name] if value).strip()


def _require_same_parent(user: User, guardian_name: str) -> None:
    """Refuse silently attaching a recycled/shared phone to a different person."""

    stored = _normalized_person_name(_user_name(user))
    supplied = _normalized_person_name(guardian_name)
    if stored and supplied and stored != supplied:
        raise Rejected(
            "This phone number already belongs to a different SchoolOS parent account. "
            "Verify the guardian name or phone number before completing registration."
        )


def _ensure_membership(*, user, school, role):
    membership, _ = Membership.objects.get_or_create(
        user=user,
        school=school,
        role=role,
        defaults={"is_active": True},
    )
    if not membership.is_active:
        membership.is_active = True
        membership.save(update_fields=["is_active"])
    return membership


def _student_account(student: Student):
    admission_login = normalize_student_admission(student.admission_number)
    if not admission_login:
        raise Rejected("A student admission ID is required before the login account can be created.")

    if student.account_user_id:
        user = student.account_user
    else:
        existing_identity = (
            LoginIdentity.objects.select_related("user")
            .filter(
                kind=LoginIdentityKind.STUDENT_ADMISSION,
                normalized_identifier=admission_login,
            )
            .first()
        )
        if existing_identity is not None:
            other_student = Student.objects.filter(
                account_user=existing_identity.user,
            ).exclude(pk=student.pk).exists()
            if other_student:
                raise Rejected(
                    "This admission ID is already being used to sign in as another student."
                )
            user = existing_identity.user
        else:
            initial_password = student.first_name.strip()
            if not initial_password:
                raise Rejected("Student first name is required to create the initial password.")
            user = create_bootstrap_user(
                first_name=student.first_name,
                last_name=student.surname,
                initial_password=initial_password,
                prefix="student",
            )

        student.account_user = user
        student.save(update_fields=["account_user", "updated_at"])

    try:
        bind_login_identity(
            user=user,
            kind=LoginIdentityKind.STUDENT_ADMISSION,
            identifier=student.admission_number,
        )
    except LoginIdentityConflict as exc:
        raise Rejected(str(exc)) from exc

    _ensure_membership(user=user, school=student.school, role=Role.STUDENT)
    return user


def _parent_account(registration: StudentRegistration, guardian: GuardianLink):
    normalized_phone = normalize_parent_phone(guardian.phone)
    if normalized_phone is None:
        raise Rejected(
            "A valid guardian phone number is required before the parent account can be created."
        )

    identity = (
        LoginIdentity.objects.select_related("user")
        .filter(
            kind=LoginIdentityKind.PARENT_PHONE,
            normalized_identifier=normalized_phone,
        )
        .first()
    )
    if identity is not None:
        user = identity.user
        _require_same_parent(user, guardian.name)
    elif guardian.account_user_id:
        user = guardian.account_user
        _require_same_parent(user, guardian.name)
    else:
        clean_email = guardian.email.strip().lower()
        user = User.objects.filter(email__iexact=clean_email).first() if clean_email else None
        if user is not None:
            _require_same_parent(user, guardian.name)
        else:
            first_name, last_name = _split_person_name(guardian.name)
            if not first_name:
                raise Rejected("Guardian first name is required to create the initial password.")
            user = create_bootstrap_user(
                first_name=first_name,
                last_name=last_name,
                initial_password=first_name,
                prefix="parent",
                preferred_email=clean_email,
            )

    try:
        bind_login_identity(
            user=user,
            kind=LoginIdentityKind.PARENT_PHONE,
            identifier=guardian.phone,
        )
    except LoginIdentityConflict as exc:
        raise Rejected(str(exc)) from exc

    parent_membership = _ensure_membership(
        user=user,
        school=registration.school,
        role=Role.PARENT,
    )
    if guardian.account_user_id != user.id:
        guardian.account_user = user
        guardian.save(update_fields=["account_user", "updated_at"])
    publish_parent_family_link(parent_membership, actor=registration.created_by)
    return user, normalized_phone


@transaction.atomic
def provision_registration_accounts(
    registration: StudentRegistration,
    student: Student,
    guardian: GuardianLink,
) -> dict:
    """Provision login identities exactly once as part of canonical activation.

    Student login ID = admission number.
    Parent login ID = normalized guardian phone.
    Initial password = the person's first name. Honorifics such as Alhaji/Hajiya
    are not treated as the parent's first name. New school-provisioned accounts
    carry ``must_change_password``; an existing/reused account is never reset.
    """

    _student_account(student)
    publish_student_class_link(student, actor=registration.created_by)
    _, parent_login = _parent_account(registration, guardian)
    return {
        "studentLoginId": student.admission_number,
        "parentLoginId": parent_login,
    }
