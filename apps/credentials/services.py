import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.identity import (
    normalize_parent_phone,
    normalize_student_admission,
    resolve_login_user,
)
from apps.accounts.models import LoginIdentity, LoginIdentityKind
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink, Student, StudentRegistration
from apps.sync.models import SyncRecord

from .models import CredentialAuditEvent, CredentialRecoveryRequest, RecoveryStatus


class CredentialManagementError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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


def _person_first_name(value: str) -> str:
    parts = [part for part in value.strip().split() if part]
    while parts and re.sub(r"[^a-z]", "", parts[0].casefold()) in _NAME_TITLES:
        parts.pop(0)
    return parts[0] if parts else ""


def _audit(*, school, user, actor, event: str, detail=None) -> None:
    CredentialAuditEvent.objects.create(
        school=school,
        user=user,
        actor=actor,
        event=event,
        detail=detail or {},
    )


def _resolve_pending(*, user, actor, note: str) -> None:
    """Resolve every pending request for this global login identity."""

    CredentialRecoveryRequest.objects.filter(
        user=user,
        status=RecoveryStatus.PENDING,
    ).update(
        status=RecoveryStatus.RESOLVED,
        resolved_at=timezone.now(),
        resolved_by=actor,
        note=note,
    )


def _login_identity(user, kind: str):
    return (
        LoginIdentity.objects.filter(user=user, kind=kind)
        .order_by("created_at", "id")
        .first()
    )


def _primary_guardian(student: Student) -> GuardianLink:
    guardian = (
        student.guardians.select_related("account_user")
        .filter(is_primary=True)
        .order_by("created_at", "id")
        .first()
    )
    if guardian is None:
        guardian = (
            student.guardians.select_related("account_user")
            .order_by("created_at", "id")
            .first()
        )
    if guardian is None:
        raise CredentialManagementError("This student has no guardian account to manage.")
    if guardian.account_user_id is None:
        raise CredentialManagementError(
            "The guardian exists, but the Parent login account has not been provisioned yet."
        )
    return guardian


def _require_parent_phone_scoped_to_school(user, school) -> None:
    if Membership.objects.filter(
        user=user,
        role=Role.PARENT,
        is_active=True,
    ).exclude(school=school).exists():
        raise CredentialManagementError(
            "This Parent account is linked to another school too. Change the shared login phone through platform support so every school is updated safely."
        )


def request_recovery(identifier: str | None) -> None:
    """Create school-office recovery work without revealing account existence."""

    raw = (identifier or "").strip()[:160]
    if not raw:
        return
    user = resolve_login_user(raw)
    if user is None or not user.is_active:
        return

    phone = normalize_parent_phone(raw)
    admission = normalize_student_admission(raw)
    kind = None
    role = None
    stored_identifier = raw
    if phone and LoginIdentity.objects.filter(
        user=user,
        kind=LoginIdentityKind.PARENT_PHONE,
        normalized_identifier=phone,
    ).exists():
        kind = LoginIdentityKind.PARENT_PHONE
        role = Role.PARENT
        stored_identifier = phone
    elif admission and LoginIdentity.objects.filter(
        user=user,
        kind=LoginIdentityKind.STUDENT_ADMISSION,
        normalized_identifier=admission,
    ).exists():
        kind = LoginIdentityKind.STUDENT_ADMISSION
        role = Role.STUDENT
        stored_identifier = admission
    if kind is None:
        return

    memberships = Membership.objects.filter(
        user=user,
        role=role,
        is_active=True,
        school__is_active=True,
    ).select_related("school")
    for membership in memberships:
        try:
            with transaction.atomic():
                _, created = CredentialRecoveryRequest.objects.get_or_create(
                    school=membership.school,
                    user=user,
                    status=RecoveryStatus.PENDING,
                    defaults={
                        "identity_kind": kind,
                        "requested_identifier": stored_identifier,
                    },
                )
                if created:
                    _audit(
                        school=membership.school,
                        user=user,
                        actor=None,
                        event="recovery_requested",
                        detail={"identityKind": kind},
                    )
        except IntegrityError:
            continue


def serialize_recovery_request(item: CredentialRecoveryRequest) -> dict:
    roles = list(
        Membership.objects.filter(
            school=item.school,
            user=item.user,
            is_active=True,
        ).values_list("role", flat=True)
    )
    name = " ".join(
        value for value in [item.user.first_name, item.user.last_name] if value
    ).strip()
    return {
        "id": str(item.id),
        "name": name,
        "identityKind": item.identity_kind,
        "requestedIdentifier": item.requested_identifier,
        "roles": roles,
        "status": item.status,
        "requestedAt": item.created_at.isoformat(),
    }


def credential_handoff(student: Student) -> dict:
    if student.account_user_id is None:
        raise CredentialManagementError("The Student login account has not been provisioned yet.")
    student_user = student.account_user
    student_identity = _login_identity(student_user, LoginIdentityKind.STUDENT_ADMISSION)
    if student_identity is None:
        raise CredentialManagementError("The Student admission login identity is missing.")

    guardian = _primary_guardian(student)
    parent_user = guardian.account_user
    parent_identity = _login_identity(parent_user, LoginIdentityKind.PARENT_PHONE)
    if parent_identity is None:
        raise CredentialManagementError("The Parent phone login identity is missing.")

    student_temporary = student.first_name.strip() if student_user.must_change_password else None
    parent_first_name = parent_user.first_name.strip() or _person_first_name(guardian.name)
    parent_temporary = parent_first_name if parent_user.must_change_password else None
    return {
        "student": {
            "name": student.full_name,
            "loginId": student_identity.identifier,
            "requiresPasswordChange": student_user.must_change_password,
            "temporaryPassword": student_temporary,
        },
        "parent": {
            "name": guardian.name,
            "loginId": parent_identity.normalized_identifier,
            "requiresPasswordChange": parent_user.must_change_password,
            "temporaryPassword": parent_temporary,
        },
        "temporaryPasswordRule": "first_name",
    }


def _reset_user_password(*, user, temporary_password: str) -> None:
    password = temporary_password.strip()
    if not password:
        raise CredentialManagementError("A first name is required before credentials can be reset.")
    user.set_password(password)
    user.must_change_password = True
    user.credential_version += 1
    user.save(update_fields=["password", "must_change_password", "credential_version"])


@transaction.atomic
def reset_student_credentials(*, student: Student, actor: Membership) -> dict:
    student = Student.objects.select_for_update().select_related("account_user").get(pk=student.pk)
    if student.account_user_id is None:
        raise CredentialManagementError("The Student login account has not been provisioned yet.")
    user = student.account_user
    _reset_user_password(user=user, temporary_password=student.first_name)
    _resolve_pending(
        user=user,
        actor=actor,
        note="Student credentials reset by school administration.",
    )
    _audit(
        school=student.school,
        user=user,
        actor=actor,
        event="student_password_reset",
        detail={"studentId": str(student.id), "admissionNumber": student.admission_number},
    )
    return credential_handoff(student)


@transaction.atomic
def reset_parent_credentials(*, student: Student, actor: Membership) -> dict:
    student = Student.objects.select_for_update().get(pk=student.pk)
    guardian = _primary_guardian(student)
    user = guardian.account_user
    first_name = user.first_name.strip() or _person_first_name(guardian.name)
    _reset_user_password(user=user, temporary_password=first_name)
    _resolve_pending(
        user=user,
        actor=actor,
        note="Parent credentials reset by school administration.",
    )
    _audit(
        school=student.school,
        user=user,
        actor=actor,
        event="parent_password_reset",
        detail={"studentId": str(student.id)},
    )
    return credential_handoff(student)


@transaction.atomic
def change_parent_phone(*, student: Student, actor: Membership, new_phone: str | None) -> dict:
    student = Student.objects.select_for_update().get(pk=student.pk)
    guardian = _primary_guardian(student)
    user = guardian.account_user
    _require_parent_phone_scoped_to_school(user, student.school)

    normalized = normalize_parent_phone(new_phone)
    if normalized is None:
        raise CredentialManagementError(
            "Enter a valid Nigerian Parent phone number, for example 0803 123 4567."
        )

    conflict = (
        LoginIdentity.objects.select_for_update()
        .filter(
            kind=LoginIdentityKind.PARENT_PHONE,
            normalized_identifier=normalized,
        )
        .exclude(user=user)
        .exists()
    )
    if conflict:
        raise CredentialManagementError(
            "That phone number already belongs to another SchoolOS Parent account."
        )

    identities = list(
        LoginIdentity.objects.select_for_update()
        .filter(user=user, kind=LoginIdentityKind.PARENT_PHONE)
        .order_by("created_at", "id")
    )
    old_login = identities[0].normalized_identifier if identities else guardian.phone
    if identities:
        primary = identities[0]
        primary.identifier = normalized
        primary.normalized_identifier = normalized
        primary.save(update_fields=["identifier", "normalized_identifier"])
        if len(identities) > 1:
            LoginIdentity.objects.filter(id__in=[item.id for item in identities[1:]]).delete()
    else:
        LoginIdentity.objects.create(
            user=user,
            kind=LoginIdentityKind.PARENT_PHONE,
            identifier=normalized,
            normalized_identifier=normalized,
        )

    linked_guardians = GuardianLink.objects.filter(
        account_user=user,
        student__school=student.school,
    )
    student_ids = list(linked_guardians.values_list("student_id", flat=True))
    if GuardianLink.objects.filter(
        student_id__in=student_ids,
        phone=normalized,
    ).exclude(account_user=user).exists():
        raise CredentialManagementError(
            "That phone is already attached to another guardian for one of these children."
        )
    linked_guardians.update(phone=normalized)

    registrations = list(
        StudentRegistration.objects.filter(
            school=student.school,
            student_id__in=student_ids,
        ).select_related("source_applicant")
    )
    for registration in registrations:
        registration.guardian_phone = normalized
        registration.save(update_fields=["guardian_phone", "updated_at"])
        if registration.source_applicant_id:
            registration.source_applicant.guardian_phone = normalized
            registration.source_applicant.save(update_fields=["guardian_phone", "updated_at"])
        record = SyncRecord.objects.select_for_update().filter(
            school=student.school,
            entity_type="student_registration",
            entity_id=registration.registration_id,
            deleted=False,
        ).first()
        if record is not None:
            record.payload = {
                **record.payload,
                "guardianPhone": normalized,
                "parentLoginId": normalized,
            }
            record.updated_by = actor
            record.version += 1
            record.save(update_fields=["payload", "updated_by", "version"])

    user.credential_version += 1
    user.save(update_fields=["credential_version"])
    _resolve_pending(
        user=user,
        actor=actor,
        note="Parent login phone changed by school administration.",
    )
    _audit(
        school=student.school,
        user=user,
        actor=actor,
        event="parent_phone_changed",
        detail={
            "studentId": str(student.id),
            "oldLoginId": old_login,
            "newLoginId": normalized,
        },
    )
    return credential_handoff(student)


@transaction.atomic
def dismiss_recovery(*, recovery: CredentialRecoveryRequest, actor: Membership) -> None:
    recovery = CredentialRecoveryRequest.objects.select_for_update().get(pk=recovery.pk)
    if recovery.status != RecoveryStatus.PENDING:
        return
    recovery.status = RecoveryStatus.DISMISSED
    recovery.resolved_at = timezone.now()
    recovery.resolved_by = actor
    recovery.note = "Dismissed by school administration."
    recovery.save(update_fields=["status", "resolved_at", "resolved_by", "note"])
    _audit(
        school=recovery.school,
        user=recovery.user,
        actor=actor,
        event="recovery_dismissed",
        detail={"requestId": str(recovery.id)},
    )
