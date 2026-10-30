"""Which sync entity types stay reachable regardless of SkulGen subscription health
(Phase C freemium policy): essential school operations and financial continuity, as
distinguished from the default (today's behaviour, unchanged) and from premium features
(gated separately, see apps.billing.premium_access - action-level, not entity-type-level,
since a single entity type can carry both a free and a premium action, e.g. CBT).

Unlisted entity types keep today's exact behaviour: blocked once the organization's
subscription is restricted or suspended. Only entity types explicitly classified here ever
bypass that check - the default is deliberately conservative, so nothing already reviewed and
left unlisted is loosened by this module.
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

#: Reserved for sync-reached financial/security-continuity entity types, should any ever
#: exist (today's known financial-continuity gaps - bankconnect/mandates viewing - are
#: REST views, not sync entity types; fixed directly in their own permission modules).
FINANCIAL_CONTINUITY_ENTITY_TYPES: set[str] = set()


def bypasses_subscription_gate(entity_type: str) -> bool:
    return entity_type in ESSENTIAL_ENTITY_TYPES or entity_type in FINANCIAL_CONTINUITY_ENTITY_TYPES


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


def premium_code_for(entity_type: str, payload: dict) -> str | None:
    """The entitlement code this mutation needs, or None if it isn't premium-gated."""

    action = payload.get("action") if isinstance(payload, dict) else None
    by_action = PREMIUM_ACTIONS.get((entity_type, action))
    if by_action is not None:
        return by_action
    return PREMIUM_ENTITY_TYPES.get(entity_type)
