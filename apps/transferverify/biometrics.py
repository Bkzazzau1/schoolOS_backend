"""The biometric layer's real, tenant-aware data path: consent, protected
template storage, revocation, and the confirmed_match authority rule (§19-20
of the brief: confirmed_match requires a fingerprint match AND a phone
match together - never a fingerprint alone).

Design decision (confirmed with the product owner before building this):
SchoolOS has no fingerprint capture hardware or vendor biometric SDK
integrated today, and Flutter has no cross-platform API that produces a
portable fingerprint template - a device's own fingerprint sensor (via
local_auth) only unlocks that one device, it cannot hand a template to a
server for cross-school comparison. Real capture needs a commercial
biometric SDK, which is a vendor/contract decision for later. Until then:
this module's models and consent/storage/revocation path are real and
fully tested; _templates_plausibly_match is an explicitly labeled
placeholder (exact-byte-equality, not a biometric algorithm) that exists
only so evaluate_confidence's authority rule can be proven end-to-end. No
REST endpoint exposes template capture or matching yet, and nothing in
Flutter calls this module - there is no legitimate way to submit a real
template until a vendor SDK is chosen, and building a capture screen ahead
of that would show something that looks like it works when it does not.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected

from .models import (
    BiometricConsent,
    BiometricConsentStatus,
    BiometricFinger,
    StudentBiometricTemplate,
)
from .network import enrollment_for


class MatchConfidence:
    NO_MATCH = "no_match"
    CANDIDATE_MATCH = "candidate_match"
    STRONG_MATCH = "strong_match"
    CONFIRMED_MATCH = "confirmed_match"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


def evaluate_confidence(*, phone_matched: bool, template_matched: bool | None) -> str:
    """The one place that decides how confident a cross-school identity
    match is. Never auto-merges two NetworkStudentIdentity records by
    itself - that stays a human decision downstream (a later phase) - it
    only ever names how strong the signal is, honestly:

    - template_matched is None means no biometric signal was available or
      attempted (today: always, since no capture hardware exists) - phone
      alone can only ever reach candidate_match, matching
      apps.transferverify.network.match_by_phone exactly.
    - A fingerprint template matches but the phone does not (or none was
      given): strong_match - stronger than phone alone, still not enough to
      confirm identity by itself.
    - Both the phone and a fingerprint template match: confirmed_match -
      the only tier ever allowed to treat two records as the same person
      with real confidence.
    - A fingerprint template was compared and disagrees with an otherwise-
      matching phone: manual_review_required - a genuine conflict is never
      silently resolved either way.
    - Neither signal matches: no_match.
    """
    if template_matched is True:
        return MatchConfidence.CONFIRMED_MATCH if phone_matched else MatchConfidence.STRONG_MATCH
    if template_matched is False:
        return MatchConfidence.MANUAL_REVIEW_REQUIRED if phone_matched else MatchConfidence.NO_MATCH
    return MatchConfidence.CANDIDATE_MATCH if phone_matched else MatchConfidence.NO_MATCH


def _templates_plausibly_match(stored: bytes, candidate: bytes) -> bool:
    """NOT a real biometric comparison algorithm - see this module's own
    docstring. Exact-byte-equality only, so the confirmed_match authority
    chain in evaluate_confidence can be proven and tested end-to-end before
    a real vendor SDK exists. A real deployment MUST replace this with an
    actual biometric matching engine before any result from this function
    is ever trusted for a real admission decision."""
    return bool(stored) and bool(candidate) and stored == candidate


def match_confidence_for_identity(
    *, network_identity, phone_matched: bool, candidate_finger: str | None = None, candidate_template_data: bytes | None = None
) -> str:
    """Combines the phone signal apps.transferverify.network.match_by_phone
    already computes with an optional freshly-captured template compared
    against every active template this identity has on file for that same
    finger. See evaluate_confidence and _templates_plausibly_match."""
    template_matched = None
    if candidate_finger is not None and candidate_template_data:
        active_templates = list(
            StudentBiometricTemplate.objects.filter(
                network_identity=network_identity, revoked_at__isnull=True, finger=candidate_finger
            )
        )
        # Nothing active on file for this finger is "no signal" (None), not a
        # mismatch - a revoked template must never count as a disagreement.
        if active_templates:
            template_matched = any(
                _templates_plausibly_match(template.template_data, candidate_template_data) for template in active_templates
            )
    return evaluate_confidence(phone_matched=phone_matched, template_matched=template_matched)


def _assert_active(membership) -> None:
    if not membership.is_active:
        raise Rejected("Only an active membership can manage biometric consent and templates.")


@transaction.atomic
def grant_consent(*, membership, student, guardian_name: str, note: str = "") -> BiometricConsent:
    _assert_active(membership)
    name = guardian_name.strip()
    if not name:
        raise Rejected("Enter the guardian's name.")
    existing = BiometricConsent.objects.filter(
        school=membership.school, student=student, status=BiometricConsentStatus.GRANTED
    ).first()
    if existing is not None:
        return existing
    return BiometricConsent.objects.create(
        school=membership.school, student=student, guardian_name=name, note=note.strip(), recorded_by=membership
    )


@transaction.atomic
def withdraw_consent(*, membership, student) -> BiometricConsent:
    _assert_active(membership)
    consent = BiometricConsent.objects.select_for_update().filter(
        school=membership.school, student=student, status=BiometricConsentStatus.GRANTED
    ).first()
    if consent is None:
        raise Rejected("There is no active consent to withdraw.")
    now = timezone.now()
    consent.status = BiometricConsentStatus.WITHDRAWN
    consent.withdrawn_at = now
    consent.withdrawn_by = membership
    consent.save(update_fields=["status", "withdrawn_at", "withdrawn_by"])
    # A school must never keep using a template after consent for it is
    # pulled - withdrawing consent revokes every template captured under it.
    StudentBiometricTemplate.objects.filter(consent=consent, revoked_at__isnull=True).update(
        revoked_at=now, revoked_by=membership
    )
    return consent


@transaction.atomic
def capture_template(
    *, membership, student, finger: str, template_data: bytes, template_version: str,
    quality_score: int | None = None, source_device: str = "",
) -> StudentBiometricTemplate:
    """Registers one protected template for this school's own student -
    requires active guardian consent recorded first, and reuses the same
    NetworkStudentIdentity bridge apps.transferverify.network already uses
    for phone matching, so a template and a guardian phone for the same
    real child always resolve to one identity."""
    _assert_active(membership)
    if finger not in BiometricFinger.values:
        raise Rejected("Choose a valid finger.")
    if not template_data:
        raise Rejected("A template is required.")
    if not template_version.strip():
        raise Rejected("A template version is required.")
    consent = BiometricConsent.objects.filter(
        school=membership.school, student=student, status=BiometricConsentStatus.GRANTED
    ).first()
    if consent is None:
        raise Rejected("Guardian consent must be recorded before capturing a biometric template.")

    enrollment = enrollment_for(school=membership.school, student=student, membership=membership)
    return StudentBiometricTemplate.objects.create(
        network_identity=enrollment.network_identity, enrolled_school=membership.school, consent=consent,
        finger=finger, template_data=template_data, template_version=template_version.strip(),
        quality_score=quality_score, source_device=source_device.strip(), captured_by=membership,
    )


@transaction.atomic
def revoke_template(*, membership, template_id) -> StudentBiometricTemplate:
    _assert_active(membership)
    try:
        template = StudentBiometricTemplate.objects.select_for_update().get(
            id=template_id, enrolled_school=membership.school
        )
    except (StudentBiometricTemplate.DoesNotExist, ValueError, TypeError):
        raise Rejected("That biometric template does not exist for this school.")
    if template.revoked_at is not None:
        raise Rejected("This template has already been revoked.")
    template.revoked_at = timezone.now()
    template.revoked_by = membership
    template.save(update_fields=["revoked_at", "revoked_by"])
    return template
