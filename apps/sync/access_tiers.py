"""Which sync entity types stay reachable regardless of SkulGen subscription health
(Phase C freemium policy): essential school operations, academic/financial continuity, and
premium-gated entity types, as distinguished from the default (today's behaviour, unchanged).

Unlisted entity types keep today's exact behaviour: blocked once the organization's
subscription is restricted or suspended. Only entity types explicitly classified here ever
bypass that check - the default is deliberately conservative, so nothing already reviewed and
left unlisted is loosened by this module.

A premium-gated entity type (PREMIUM_ACTIONS/PREMIUM_ENTITY_TYPES below) also bypasses this
coarse, organization-wide gate - not because it is free, but because its real authorization is
apps.billing.premium_access.require_premium_entitlement(), which judges this school's own
invoice history, independent of the organization's lifecycle status. Leaving these entity
types behind the coarse gate as well would let one school's unrelated unpaid invoice (which can
escalate the shared organization's status) wrongly block a *different*, paid school's premium
action before require_premium_entitlement() ever runs - exactly the cross-school interference
the correction brief this module implements calls out as critical. Routing them through the
fine-grained check alone, instead, is what "ordinary subscription lifecycle status does not
override our school-specific freemium policy" means in practice.
"""

from apps.billing import premium_access as _premium_access

#: Starting/configuring a term, admitting and enrolling students, and managing staff
#: records - the brief's own list of what must work before payment. Verified against the
#: actual registered handlers (apps/academics/handlers.py, apps/students/handlers.py,
#: apps/staff/*/handler.py), not assumed.
ESSENTIAL_ENTITY_TYPES = {
    "academic_session",
    "academic_term",
    "academic_class",
    "academic_progression_batch",
    "admission_applicant",
    "student_registration",
    "administrator_student_lifecycle",
    "staff_proposal",
    "administrator_staff_directory",
    "owner_staff_profile",
}

#: Reserved for sync-reached financial-continuity entity types, should any ever exist (today's
#: known financial-continuity gaps - bankconnect/mandates viewing - are REST views, not sync
#: entity types; fixed directly in their own permission modules).
FINANCIAL_CONTINUITY_ENTITY_TYPES: set[str] = set()

#: Academic continuity: a student's attempt at an already-published CBT exam
#: ("academic_cbt_attempt" - start/answer/submit) and a principal/administrator/teacher's
#: review, correction, approval or release of an already-generated report card
#: ("academic_report_card" - principalReview/principalReturn/submit/release/
#: classTeacherComment). Neither entity type ever creates a new premium output - CBT exam
#: *publishing* and report-card *generation* are the gated actions, on the separate
#: "academic_cbt_test" and "academic_report_card_batch" entity types below - so these two stay
#: usable no matter the school's subscription state, consistent with "a student must never be
#: prevented from completing an examination because of a school subscription dispute" and
#: "existing report cards must remain accessible to authorized users."
ACADEMIC_CONTINUITY_ENTITY_TYPES = {
    "academic_cbt_attempt",
    "academic_report_card",
}


#: The two approved premium capabilities (Phase C), keyed by (entity_type, action) so a
#: single entity type can carry both a free and a premium action - CBT's "academic_cbt_test"
#: handles saveDraft/publish/close, and only "publish" is premium; everything else about that
#: entity type (including every action on the separate "academic_cbt_test" draft/close path,
#: and the wholly separate "academic_cbt_attempt" entity type a student submits through) is
#: unaffected. "academic_report_card_batch" has only one action ("compile"), which is exactly
#: the "generate a new report card" capability named in the brief - gating the entity type
#: alone is precise here, with no action-level ambiguity to resolve.
PREMIUM_ACTIONS = {
    ("academic_cbt_test", "publish"): _premium_access.CBT_EXAM_PUBLISH,
}
PREMIUM_ENTITY_TYPES = {
    "academic_report_card_batch": _premium_access.REPORT_CARD_GENERATION,
}

#: Every entity type that carries at least one premium-gated action - saveDraft/close on
#: "academic_cbt_test" are not premium themselves, but the entity type as a whole still needs
#: to clear the coarse gate so the one premium action it does carry (publish) is judged solely
#: by require_premium_entitlement(), never by the organization's lifecycle status.
_PREMIUM_GATED_ENTITY_TYPES = {entity_type for (entity_type, _action) in PREMIUM_ACTIONS} | set(
    PREMIUM_ENTITY_TYPES
)


def bypasses_subscription_gate(entity_type: str) -> bool:
    return (
        entity_type in ESSENTIAL_ENTITY_TYPES
        or entity_type in FINANCIAL_CONTINUITY_ENTITY_TYPES
        or entity_type in ACADEMIC_CONTINUITY_ENTITY_TYPES
        or entity_type in _PREMIUM_GATED_ENTITY_TYPES
    )


def premium_code_for(entity_type: str, payload: dict) -> str | None:
    """The entitlement code this mutation needs, or None if it isn't premium-gated."""

    action = payload.get("action") if isinstance(payload, dict) else None
    by_action = PREMIUM_ACTIONS.get((entity_type, action))
    if by_action is not None:
        return by_action
    return PREMIUM_ENTITY_TYPES.get(entity_type)
