"""The collection policy: the school's defaults, the layers over them, and how they are worked out for one family.

The layers, nearest first:  family  >  batch  >  term  >  session  >  the school's default.
An override holds ONLY the fields it explicitly changes; every other field keeps inheriting from the layer beneath it. Working out a
family's policy therefore produces both the values AND where each came from (`sources`), so a screen can say "Using school default" or
"Override for this term" and an approver can see exactly why an account will behave as it does.

Rules the code enforces:
* the provider is never a policy field - a family (or a term, or a batch) cannot choose a different provider than the school's active one;
* an override may carry an expiry (once, end of term, end of session, a date, or until removed) and an expired one simply stops applying;
* the school decides when an override needs a reason (never / always / for sensitive ones), and every override is audited;
* an override is never edited or deleted: replacing or removing it leaves the old one as history.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.bankconnect import permissions as authority
from apps.receivables import periods

from . import audit
from .constants import (
    GRACE_ACTIONS,
    SCOPE_ORDER,
    AccountMode,
    ArrearsPolicy,
    EligibilityPolicy,
    ExpiryKind,
    OverrideScope,
    ReasonPolicy,
    ReuseScope,
    SettlementAction,
    SwitchPolicy,
)
from .errors import CollectionRefused
from .models import CollectionPolicyOverride, SchoolCollectionPolicy

MAX_REASON = 300

# -- the fields -------------------------------------------------------------------------------------

CHOICES = {
    "account_mode": AccountMode, "reuse_scope": ReuseScope, "settlement_action": SettlementAction,
    "arrears_policy": ArrearsPolicy, "eligibility_policy": EligibilityPolicy,
}
INTEGERS = {"reuse_count": (1, 20), "grace_period_hours": (1, 24 * 365)}
DATES = ("reuse_until",)
#: What a session, term, batch or family may override.
OVERRIDABLE = ("account_mode", "reuse_scope", "reuse_count", "reuse_until", "settlement_action", "grace_period_hours", "arrears_policy", "eligibility_policy")
#: Only the school changes these: they are about the school's arrangement, not one family's.
SCHOOL_ONLY = {"provider_switch_policy": SwitchPolicy, "override_reason_policy": ReasonPolicy}
#: Overriding one of these, or anything for a single family, needs a reason wherever the school asks for reasons on sensitive overrides.
SENSITIVE_FIELDS = ("eligibility_policy", "arrears_policy")

LABELS = {
    "account_mode": "Account type", "reuse_scope": "How long an account is reused", "reuse_count": "How many terms or sessions",
    "reuse_until": "Reused until", "settlement_action": "When a family has paid", "grace_period_hours": "Waiting period (hours)",
    "arrears_policy": "Previous balances", "eligibility_policy": "Families with a previous balance",
    "provider_switch_policy": "When the provider is switched", "override_reason_policy": "Reasons for overrides",
}


def _column(name: str) -> str:
    return name if name in ("override_reason_policy",) else f"default_{name}"


def school_policy(school) -> SchoolCollectionPolicy:
    """The school's default policy (made with the defaults the first time it is asked for)."""
    policy, _ = SchoolCollectionPolicy.objects.get_or_create(school=school)
    return policy


def school_values(policy: SchoolCollectionPolicy) -> dict:
    values = {}
    for name in OVERRIDABLE:
        value = getattr(policy, _column(name))
        values[name] = value.isoformat() if isinstance(value, date) else value
    return values


# -- cleaning what a person typed --------------------------------------------------------------------


def clean_value(name: str, value):
    """One field's value, checked. Anything that is not a valid choice, whole number or date is refused with the field's own name."""
    label = LABELS.get(name, name)
    if name in CHOICES or name in SCHOOL_ONLY:
        enum = CHOICES.get(name) or SCHOOL_ONLY[name]
        if not isinstance(value, str) or value not in enum.values:
            raise CollectionRefused(f"'{label}' must be one of: {', '.join(enum.values)}.", "invalid_policy_value")
        return value
    if name in INTEGERS:
        low, high = INTEGERS[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise CollectionRefused(f"'{label}' must be a whole number from {low} to {high}.", "invalid_policy_value")
        return value
    if name in DATES:
        parsed = parse_date(value) if isinstance(value, str) else None
        if parsed is None:
            raise CollectionRefused(f"'{label}' must be a date (YYYY-MM-DD).", "invalid_policy_value")
        return parsed.isoformat()
    raise CollectionRefused(f"'{str(name)[:40]}' is not a collection policy setting.", "unknown_policy_field")


def clean_overrides(values) -> dict:
    """The fields an override may carry, each checked. It must change something, and never the provider."""
    if not isinstance(values, dict) or not values:
        raise CollectionRefused("Choose at least one setting to change.", "nothing_to_override")
    cleaned = {}
    for name, value in values.items():
        if name in ("provider", "connection", "provider_connection", "providerConnectionId"):
            raise CollectionRefused("A family, term or batch cannot choose its own provider. The school has one active collection provider.", "provider_not_overridable")
        if name not in OVERRIDABLE:
            raise CollectionRefused(f"'{str(name)[:40]}' cannot be overridden here.", "not_overridable")
        cleaned[name] = clean_value(name, value)
    return cleaned


def problems_of(values: dict) -> list[str]:
    """What is missing from a complete policy, in words: a wait with no waiting period, a count with no count, a date with no date."""
    problems = []
    if values.get("settlement_action") in GRACE_ACTIONS and not values.get("grace_period_hours"):
        problems.append("Set the waiting period: this policy waits before it acts on a settled account.")
    if values.get("reuse_scope") in (ReuseScope.SELECTED_TERMS, ReuseScope.MULTIPLE_SESSIONS) and not values.get("reuse_count"):
        problems.append("Say how many terms or sessions an account is reused for.")
    if values.get("reuse_scope") == ReuseScope.UNTIL_DATE and not values.get("reuse_until"):
        problems.append("Say the date an account is reused until.")
    return problems


# -- working a family's policy out -------------------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """The policy that applies, field by field, and where each field came from."""

    values: dict
    sources: dict = field(default_factory=dict)

    def __getitem__(self, name):
        return self.values[name]

    def get(self, name, default=None):
        return self.values.get(name, default)

    @property
    def problems(self) -> list[str]:
        return problems_of(self.values)

    def snapshot(self) -> dict:
        return {"values": dict(self.values), "sources": {k: dict(v) for k, v in self.sources.items()}, "problems": self.problems}

    def overridden(self) -> dict:
        return {name: value for name, value in self.values.items() if self.sources.get(name, {}).get("scope") != "school"}


def _live(override: CollectionPolicyOverride, today: date) -> bool:
    if override.removed_at or override.consumed_at:
        return False
    if override.expiry_kind == ExpiryKind.AT_DATE:
        return bool(override.expires_on) and today <= override.expires_on
    if override.expiry_kind == ExpiryKind.END_OF_TERM:
        return bool(override.expiry_term_id) and today <= override.expiry_term.ends_on
    if override.expiry_kind == ExpiryKind.END_OF_SESSION:
        return bool(override.expiry_session_id) and today <= override.expiry_session.ends_on
    return True


def _source(override: CollectionPolicyOverride) -> dict:
    return {
        "scope": override.scope, "overrideId": str(override.id), "reason": override.reason, "expiryKind": override.expiry_kind,
        "expiresOn": override.expires_on.isoformat() if override.expires_on else None,
    }


def _merge(base: dict, layers: list) -> Resolved:
    values = dict(base)
    sources = {name: {"scope": "school"} for name in values}
    for override in layers:
        for name, value in (override.values or {}).items():
            if name in values:
                values[name] = value
                sources[name] = _source(override)
    return Resolved(values, sources)


class PolicyContext:
    """The school's policy prepared once for a session, term and batch, so the policy of hundreds of families is worked out without a
    query each. `for_family` merges in the family's own override."""

    def __init__(self, school, *, session=None, term=None, batch=None, today: date | None = None):
        self.school = school
        self.today = today or periods.school_today()
        if term is not None and session is None:
            session = term.session
        self.session, self.term, self.batch = session, term, batch
        self.policy = school_policy(school)
        self.base = school_values(self.policy)
        rows = CollectionPolicyOverride.objects.select_related("expiry_term", "expiry_session").filter(
            school=school, removed_at__isnull=True, consumed_at__isnull=True
        )
        self._layers: list[CollectionPolicyOverride] = []
        self._family: dict = {}
        for row in rows:
            if not _live(row, self.today):
                continue
            if row.scope == OverrideScope.FAMILY:
                self._family[row.family_id] = row
            elif (
                (row.scope == OverrideScope.SESSION and session is not None and row.session_id == session.id)
                or (row.scope == OverrideScope.TERM and term is not None and row.term_id == term.id)
                or (row.scope == OverrideScope.BATCH and batch is not None and row.batch_id == batch.id)
            ):
                self._layers.append(row)
        self._layers.sort(key=lambda o: SCOPE_ORDER.index(o.scope))

    def base_policy(self) -> Resolved:
        """Everything above the family: the school, session, term and batch."""
        return _merge(self.base, self._layers)

    def for_family(self, family_id) -> Resolved:
        layers = list(self._layers)
        if family_id in self._family:
            layers.append(self._family[family_id])
        return _merge(self.base, layers)


def resolve(school, *, session=None, term=None, batch=None, family=None, today: date | None = None) -> Resolved:
    """The policy for one family (or, with no family, for the batch or period): the layers merged, with sources."""
    context = PolicyContext(school, session=session, term=term, batch=batch, today=today)
    return context.for_family(family.id if family is not None else None) if family is not None else context.base_policy()


# -- reasons and authority ---------------------------------------------------------------------------


def is_sensitive(scope: str, values: dict) -> bool:
    return scope == OverrideScope.FAMILY or any(name in SENSITIVE_FIELDS for name in values)


def reason_required(school, *, sensitive: bool) -> bool:
    rule = school_policy(school).override_reason_policy
    return rule == ReasonPolicy.REQUIRED_ALWAYS or (rule == ReasonPolicy.REQUIRED_SENSITIVE and sensitive)


def clean_reason(school, reason, *, sensitive: bool, what: str = "an override") -> str:
    reason = " ".join(str(reason or "").split())
    if len(reason) > MAX_REASON:
        raise CollectionRefused(f"A reason can be at most {MAX_REASON} characters.", "reason_too_long")
    if not reason and reason_required(school, sensitive=sensitive):
        raise CollectionRefused(f"Say why you are making {what}: the school asks for a reason.", "reason_required")
    return reason


def _require_policy_authority(membership, *, allow_prepare: bool = False) -> None:
    if not (authority.can_manage_policy(membership) or (allow_prepare and authority.can_prepare(membership))):
        raise CollectionRefused(
            "Only the owner, or someone the owner has authorised, can change the school's collection policy.", "not_policy_manager"
        )


# -- the school's default ----------------------------------------------------------------------------


@transaction.atomic
def update_school_policy(membership, values) -> SchoolCollectionPolicy:
    """Change the school's default policy. Only the fields sent change. Audited with what each was and became."""
    _require_policy_authority(membership)
    if not isinstance(values, dict) or not values:
        raise CollectionRefused("Choose at least one setting to change.", "nothing_to_change")
    policy = SchoolCollectionPolicy.objects.select_for_update().filter(school=membership.school).first() or school_policy(membership.school)
    before = {}
    after = {}
    for name, value in values.items():
        if name in SCHOOL_ONLY or name in OVERRIDABLE:
            column, cleaned = _column(name), clean_value(name, value)
        else:
            raise CollectionRefused(f"'{str(name)[:40]}' is not a collection policy setting.", "unknown_policy_field")
        current = getattr(policy, column)
        if isinstance(current, date):
            current = current.isoformat()
        if current == cleaned:
            continue
        before[name], after[name] = current, cleaned
        setattr(policy, column, parse_date(cleaned) if name in DATES else cleaned)
    if not after:
        return policy
    problems = problems_of(school_values(policy))
    if problems:
        raise CollectionRefused(problems[0], "policy_incomplete")
    policy.updated_by = membership
    policy.save()
    audit.record(membership.school, "school_policy_changed", actor=membership, obj=policy, object_id=membership.school_id, before=before, after=after)
    return policy


# -- the layers over it ------------------------------------------------------------------------------


def _expiry(membership, *, scope, session, term, kind, expires_on, expiry_term, expiry_session) -> dict:
    kind = kind or ExpiryKind.UNTIL_REMOVED
    if kind not in ExpiryKind.values:
        raise CollectionRefused(f"Choose how long the override lasts: {', '.join(ExpiryKind.values)}.", "invalid_expiry")
    out = {"expiry_kind": kind, "expires_on": None, "expiry_term": None, "expiry_session": None}
    if kind == ExpiryKind.AT_DATE:
        parsed = expires_on if isinstance(expires_on, date) else (parse_date(expires_on) if isinstance(expires_on, str) else None)
        if parsed is None:
            raise CollectionRefused("Say the date the override lasts until.", "invalid_expiry")
        if parsed < periods.school_today():
            raise CollectionRefused("That date has already passed.", "invalid_expiry")
        out["expires_on"] = parsed
    elif kind == ExpiryKind.END_OF_TERM:
        chosen = expiry_term or term or (periods.current_period(membership.school)[1])
        if chosen is None:
            raise CollectionRefused("There is no current term to end the override with. Choose a date instead.", "invalid_expiry")
        out["expiry_term"] = chosen
    elif kind == ExpiryKind.END_OF_SESSION:
        chosen = expiry_session or session or (periods.current_period(membership.school)[0])
        if chosen is None:
            raise CollectionRefused("There is no current session to end the override with. Choose a date instead.", "invalid_expiry")
        out["expiry_session"] = chosen
    return out


@transaction.atomic
def set_override(
    membership, *, scope, session=None, term=None, batch=None, family=None, values, reason="", expiry_kind=None, expires_on=None,
    expiry_term=None, expiry_session=None, allow_prepare: bool = False,
) -> CollectionPolicyOverride:
    """Put an override on a session, term, batch or family, replacing any live one there (which stays as history). Audited."""
    _require_policy_authority(membership, allow_prepare=allow_prepare and scope == OverrideScope.BATCH)
    if scope not in OverrideScope.values:
        raise CollectionRefused("Choose whether this is for a session, a term, a batch or a family.", "invalid_scope")
    school = membership.school
    targets = {OverrideScope.SESSION: session, OverrideScope.TERM: term, OverrideScope.BATCH: batch, OverrideScope.FAMILY: family}
    target = targets[scope]
    if target is None:
        raise CollectionRefused(f"Say which {scope} this is for.", "target_required")
    if getattr(target, "school_id", None) != school.id and getattr(getattr(target, "session", None), "school_id", None) != school.id:
        raise CollectionRefused(f"That {scope} is not at this school.", "target_not_found")
    cleaned = clean_overrides(values)
    reason = clean_reason(school, reason, sensitive=is_sensitive(scope, cleaned))
    expiry = _expiry(
        membership, scope=scope, session=session, term=term, kind=expiry_kind, expires_on=expires_on, expiry_term=expiry_term, expiry_session=expiry_session
    )
    # The result must still make sense: an override that leaves a wait with no waiting period would only fail later.
    context_session = session or (term.session if term else None) or (batch.session if batch else None)
    context_term = term or (batch.term if batch else None)
    where = {f"{scope}": target}
    live = CollectionPolicyOverride.objects.select_for_update().filter(school=school, scope=scope, removed_at__isnull=True, **{f"{scope}": target})
    previous = list(live)
    now = timezone.now()
    for old in previous:
        old.removed_by, old.removed_at, old.removal_reason = membership, now, "Replaced by a newer override."
        old.save(update_fields=["removed_by", "removed_at", "removal_reason"])
    created = CollectionPolicyOverride.objects.create(
        school=school, scope=scope, values=cleaned, reason=reason, created_by=membership, **where, **expiry,
    )
    merged = _merge_check(school, context_session, context_term, batch if scope == OverrideScope.BATCH else None, family if scope == OverrideScope.FAMILY else None)
    if merged:
        raise CollectionRefused(merged[0], "policy_incomplete")
    audit.record(
        school, "policy_override_set", actor=membership, obj=created, scope=scope, target=str(getattr(target, "id", "")), values=cleaned, reason=reason,
        expiry=expiry["expiry_kind"], replaced=[str(o.id) for o in previous],
    )
    return created


def _merge_check(school, session, term, batch, family) -> list[str]:
    context = PolicyContext(school, session=session, term=term, batch=batch)
    return (context.for_family(family.id) if family is not None else context.base_policy()).problems


@transaction.atomic
def remove_override(membership, override_id, *, reason="", allow_prepare: bool = False) -> CollectionPolicyOverride:
    """Take an override off. The family, batch, term or session goes back to inheriting; the override stays as history."""
    override = CollectionPolicyOverride.objects.select_for_update().filter(school=membership.school, id=override_id).first()
    if override is None:
        raise CollectionRefused("That override was not found.", "override_not_found")
    _require_policy_authority(membership, allow_prepare=allow_prepare and override.scope == OverrideScope.BATCH)
    if override.removed_at:
        raise CollectionRefused("That override has already been removed.", "already_removed")
    override.removed_by, override.removed_at = membership, timezone.now()
    override.removal_reason = " ".join(str(reason or "").split())[:MAX_REASON]
    override.save(update_fields=["removed_by", "removed_at", "removal_reason"])
    audit.record(membership.school, "policy_override_removed", actor=membership, obj=override, scope=override.scope, values=override.values, reason=override.removal_reason)
    return override


def consume_one_time(family, *, at: datetime | None = None) -> None:
    """A one-time family override is used up by the first account generated with it."""
    CollectionPolicyOverride.objects.filter(
        school=family.school, scope=OverrideScope.FAMILY, family=family, expiry_kind=ExpiryKind.ONE_TIME, removed_at__isnull=True, consumed_at__isnull=True
    ).update(consumed_at=at or timezone.now())


def describe(resolved: Resolved) -> dict:
    """A resolved policy for a screen: each field with its value, its label and whether it is the school's default or an override."""
    rows = []
    for name in OVERRIDABLE:
        source = resolved.sources.get(name, {"scope": "school"})
        rows.append({
            "field": name, "label": LABELS[name], "value": resolved.values.get(name), "inherited": source.get("scope") == "school",
            "source": source,
        })
    return {"fields": rows, "problems": resolved.problems}


def overrides_of(school, *, scope=None, include_history: bool = False):
    query = CollectionPolicyOverride.objects.select_related("session", "term", "family", "created_by__user", "expiry_term", "expiry_session").filter(school=school)
    if scope:
        query = query.filter(scope=scope)
    if not include_history:
        query = query.filter(removed_at__isnull=True)
    return query
