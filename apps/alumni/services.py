from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.access import services as access_services
from apps.core.permissions import require_membership
from apps.notifications.services import notify
from apps.schools.models import Membership, Role

from .models import (
    AlumniProfile,
    AlumniVerificationEvent,
    AlumniVerificationStatus,
)

IDENTITY_FIELDS = {
    "original_student_reference",
    "admission_number",
    "graduation_year",
    "graduation_set",
}


class AlumniError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def require_alumni_manager(request, school_id):
    membership = require_membership(
        request.user,
        school_id,
        roles=[Role.PROPRIETOR, Role.ADMINISTRATOR, Role.PRINCIPAL],
        membership_id=request.query_params.get("membership"),
    )
    activity = {
        Role.PROPRIETOR: "owner.alumni",
        Role.ADMINISTRATOR: "administrator.alumni",
        Role.PRINCIPAL: "principal.alumni",
    }[membership.role]
    if not access_services.has_activity(membership, activity):
        raise PermissionDenied("Alumni management is not available to you.")
    return membership


def _clean_text(value):
    return value.strip() if isinstance(value, str) else value


def _check_admission_number(profile, admission_number: str):
    if not admission_number:
        return
    query = AlumniProfile.objects.filter(
        school=profile.school,
        admission_number__iexact=admission_number,
    ).exclude(membership=profile.membership)
    if query.exists():
        raise AlumniError("That admission number already belongs to another alumni profile.")


def _event(profile, event, *, actor=None, note=""):
    AlumniVerificationEvent.objects.create(
        profile=profile,
        actor=actor,
        event=event,
        note=note.strip(),
    )


@transaction.atomic
def save_self_profile(membership: Membership, data: dict) -> AlumniProfile:
    if membership.role != Role.ALUMNI:
        raise AlumniError("An Alumni membership is required.")

    profile, created = AlumniProfile.objects.select_for_update().get_or_create(
        membership=membership,
        defaults={"school": membership.school},
    )
    if profile.school_id != membership.school_id:
        raise AlumniError("The alumni profile belongs to a different school.")

    previous_status = profile.verification_status
    clean = {key: _clean_text(value) for key, value in data.items()}
    _check_admission_number(profile, clean.get("admission_number", profile.admission_number))

    identity_changed = created or any(
        field in clean and getattr(profile, field) != clean[field]
        for field in IDENTITY_FIELDS
    )

    requested_directory_visibility = clean.pop(
        "directory_visible",
        profile.directory_visible,
    )
    for field, value in clean.items():
        setattr(profile, field, value)

    if identity_changed:
        profile.verification_status = AlumniVerificationStatus.PENDING
        profile.verified_by = None
        profile.verified_at = None
        profile.reviewed_at = None
        profile.verification_note = ""
        profile.submitted_at = timezone.now()
        profile.directory_visible = False
    else:
        if requested_directory_visibility and profile.verification_status != AlumniVerificationStatus.VERIFIED:
            raise AlumniError("Your profile must be verified before it can appear in the alumni directory.")
        profile.directory_visible = requested_directory_visibility
        if profile.submitted_at is None:
            profile.submitted_at = timezone.now()

    profile.save()
    if identity_changed:
        _event(
            profile,
            AlumniVerificationEvent.Event.SUBMITTED
            if created or previous_status == AlumniVerificationStatus.PENDING
            else AlumniVerificationEvent.Event.RESUBMITTED,
            actor=membership,
        )
    return profile


@transaction.atomic
def transition_student(manager: Membership, data: dict) -> AlumniProfile:
    student = (
        Membership.objects.select_for_update()
        .select_related("user", "school")
        .filter(
            id=data["studentMembershipId"],
            school=manager.school,
            role=Role.STUDENT,
            is_active=True,
        )
        .first()
    )
    if student is None:
        raise AlumniError("That active Student membership was not found in this school.")

    alumni_membership, _ = Membership.objects.get_or_create(
        user=student.user,
        school=manager.school,
        role=Role.ALUMNI,
        defaults={"is_active": True},
    )
    if not alumni_membership.is_active:
        alumni_membership.is_active = True
        alumni_membership.save(update_fields=["is_active"])

    profile, _ = AlumniProfile.objects.select_for_update().get_or_create(
        membership=alumni_membership,
        defaults={"school": manager.school},
    )
    admission_number = data.get("admissionNumber", "").strip()
    _check_admission_number(profile, admission_number)

    profile.original_student_reference = data.get("originalStudentReference", "").strip()
    profile.admission_number = admission_number
    profile.graduation_year = data["graduationYear"]
    profile.graduation_set = data.get("graduationSet", "").strip()
    profile.verification_status = AlumniVerificationStatus.PENDING
    profile.directory_visible = False
    profile.verified_by = None
    profile.verified_at = None
    profile.reviewed_at = None
    profile.verification_note = ""
    profile.submitted_at = timezone.now()
    profile.save()
    _event(
        profile,
        AlumniVerificationEvent.Event.TRANSITIONED,
        actor=manager,
        note=f"Created from Student membership {student.id}.",
    )

    notify(
        alumni_membership,
        "alumni_identity_created",
        "Alumni profile created",
        "Your school created your Alumni identity. Review your profile and submit any corrections for verification.",
        {"membershipId": str(alumni_membership.id)},
    )
    return profile


def _verification_evidence(profile: AlumniProfile):
    if profile.graduation_year is None:
        raise AlumniError("Graduation year is required before verification.")
    if not profile.admission_number.strip() and not profile.original_student_reference.strip():
        raise AlumniError("Add an admission number or former student reference before verification.")


@transaction.atomic
def verify_profile(manager: Membership, alumni_membership_id, note: str = "") -> AlumniProfile:
    profile = (
        AlumniProfile.objects.select_for_update()
        .select_related("membership__user")
        .filter(
            school=manager.school,
            membership_id=alumni_membership_id,
            membership__role=Role.ALUMNI,
            membership__is_active=True,
        )
        .first()
    )
    if profile is None:
        raise AlumniError("That Alumni profile was not found in this school.")
    _verification_evidence(profile)

    now = timezone.now()
    profile.verification_status = AlumniVerificationStatus.VERIFIED
    profile.verified_by = manager
    profile.verified_at = now
    profile.reviewed_at = now
    profile.verification_note = note.strip()
    profile.save()
    _event(
        profile,
        AlumniVerificationEvent.Event.VERIFIED,
        actor=manager,
        note=note,
    )

    notify(
        profile.membership,
        "alumni_verified",
        "Alumni profile verified",
        "Your Alumni identity has been verified by the school. You can now choose whether to appear in the Alumni Directory.",
        {"membershipId": str(profile.membership_id)},
    )
    return profile


@transaction.atomic
def reject_profile(manager: Membership, alumni_membership_id, note: str) -> AlumniProfile:
    profile = (
        AlumniProfile.objects.select_for_update()
        .select_related("membership__user")
        .filter(
            school=manager.school,
            membership_id=alumni_membership_id,
            membership__role=Role.ALUMNI,
            membership__is_active=True,
        )
        .first()
    )
    if profile is None:
        raise AlumniError("That Alumni profile was not found in this school.")

    profile.verification_status = AlumniVerificationStatus.REJECTED
    profile.directory_visible = False
    profile.verified_by = None
    profile.verified_at = None
    profile.reviewed_at = timezone.now()
    profile.verification_note = note.strip()
    profile.save()
    _event(
        profile,
        AlumniVerificationEvent.Event.REJECTED,
        actor=manager,
        note=note,
    )

    notify(
        profile.membership,
        "alumni_verification_rejected",
        "Alumni profile needs correction",
        "Your Alumni verification needs correction. Open My Alumni Profile to review the school's note and update your identity details.",
        {"membershipId": str(profile.membership_id)},
    )
    return profile


def transition_candidates(school):
    active_alumni_user_ids = Membership.objects.filter(
        school=school,
        role=Role.ALUMNI,
        is_active=True,
    ).values_list("user_id", flat=True)
    return (
        Membership.objects.filter(
            school=school,
            role=Role.STUDENT,
            is_active=True,
        )
        .exclude(user_id__in=active_alumni_user_ids)
        .select_related("user")
        .order_by("user__last_name", "user__first_name", "user__email")
    )
