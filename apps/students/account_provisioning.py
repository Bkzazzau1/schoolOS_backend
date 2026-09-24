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


def _split_person_name(value: str) -> tuple[str, str]:
    parts = [part for part in value.strip().split() if part]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


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
    elif guardian.account_user_id:
        user = guardian.account_user
    else:
        clean_email = guardian.email.strip().lower()
        user = User.objects.filter(email__iexact=clean_email).first() if clean_email else None
        if user is None:
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

    _ensure_membership(user=user, school=registration.school, role=Role.PARENT)
    if guardian.account_user_id != user.id:
        guardian.account_user = user
        guardian.save(update_fields=["account_user", "updated_at"])
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
    Initial password = the person's first name. New school-provisioned accounts
    carry ``must_change_password``; an existing/reused account is never reset.
    """

    _student_account(student)
    _, parent_login = _parent_account(registration, guardian)
    return {
        "studentLoginId": student.admission_number,
        "parentLoginId": parent_login,
    }
