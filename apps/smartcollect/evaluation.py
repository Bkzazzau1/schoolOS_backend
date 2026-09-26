"""Working out, for every family, what a collection batch would do: what it owes, whether it is eligible, what its account would be asked
to collect, whether it already has an account, and whether the provider has what it needs to know about the payer.

Everything here READS. It never changes the ledger and never calls a provider. The result is the batch's preview, and its fingerprint is
what an approval is for: change anything that matters and the fingerprint changes, so a stale preview or a stale approval is caught.

Money is integer minor units throughout. Nothing here rewrites a receivable: arrears carried into a collection target, or families
included by an override, leave the ledger exactly as it was.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from django.db.models import Sum

from apps.academics.models import AcademicSession, AcademicTerm
from apps.receivables import ledger, periods
from apps.receivables.models import (
    CREDIT_IN,
    CREDIT_OUT,
    LIVE_STATUSES,
    AccountMode,
    AccountOrigin,
    AccountStatus,
    Family,
    FamilyCollectionAccount,
    FamilyCreditEntry,
    FamilyGuardian,
    FamilyStatus,
    FamilyStudent,
    ReceivableStatus,
    StudentReceivable,
)

from .constants import (
    SELECTABLE,
    ArrearsPolicy,
    Eligibility,
    EligibilityPolicy,
    ReuseScope,
)
from .models import FamilyPayerIdentity
from .policy import OVERRIDABLE, PolicyContext

CHECKED_FIELDS = ("name", "email", "phone", "identity")
FIELD_WORDS = {"name": "the payer's name", "email": "the payer's email address", "phone": "the payer's phone number", "identity": "the payer's BVN or NIN"}


# -- the calendar ------------------------------------------------------------------------------------


class Calendar:
    """The school's terms and sessions in order, so "the next two terms" and "the second session" mean something."""

    def __init__(self, school):
        self.terms = list(AcademicTerm.objects.filter(session__school=school).order_by("session__starts_on", "sequence", "starts_on").values_list("id", flat=True))
        self.sessions = list(AcademicSession.objects.filter(school=school).order_by("starts_on").values_list("id", flat=True))

    def term_gap(self, first_term_id, later_term_id) -> int | None:
        try:
            return self.terms.index(later_term_id) - self.terms.index(first_term_id)
        except ValueError:
            return None

    def session_gap(self, first_session_id, later_session_id) -> int | None:
        try:
            return self.sessions.index(later_session_id) - self.sessions.index(first_session_id)
        except ValueError:
            return None


def covers(account: FamilyCollectionAccount, session, term, calendar: Calendar) -> bool:
    """Whether an account already made is still the account for this period, by what the school promised when it was made."""
    if account.account_mode == AccountMode.DYNAMIC:
        return account.scope_session_id == session.id and account.scope_term_id == (term.id if term else None)
    scope = account.reuse_scope or ReuseScope.UNTIL_REPLACED
    if scope in (ReuseScope.INDEFINITELY, ReuseScope.UNTIL_REPLACED):
        return True
    if scope == ReuseScope.UNTIL_DATE:
        return account.valid_until is None or periods.window(session, term)[0] <= account.valid_until
    if scope in (ReuseScope.ONE_TERM, ReuseScope.SELECTED_TERMS):
        if account.scope_term_id is None:
            return account.scope_session_id == session.id
        if term is None:
            return account.scope_session_id == session.id
        gap = calendar.term_gap(account.scope_term_id, term.id)
        count = 1 if scope == ReuseScope.ONE_TERM else (account.reuse_count or 1)
        return gap is not None and 0 <= gap < count
    if account.scope_session_id is None:
        return True
    gap = calendar.session_gap(account.scope_session_id, session.id)
    count = 1 if scope == ReuseScope.ONE_SESSION else (account.reuse_count or 1)
    return gap is not None and 0 <= gap < count


def account_scope(values: dict, session, term, calendar: Calendar) -> dict:
    """What a new account is promised, from the policy in force: its scope, how long it is reused and until when."""
    mode = values["account_mode"]
    start, end = periods.window(session, term)
    out = {"mode": mode, "scope_session": session, "scope_term": term, "valid_from": start, "valid_until": end, "reuse_scope": "", "reuse_count": None}
    if mode == AccountMode.DYNAMIC:
        return out  # made for this period and its amount
    scope = values["reuse_scope"]
    out["reuse_scope"] = scope
    count = values.get("reuse_count")
    if scope == ReuseScope.ONE_TERM:
        pass  # this period's end
    elif scope == ReuseScope.SELECTED_TERMS:
        out["reuse_count"] = count
        if term is not None and count:
            index = calendar.terms.index(term.id) + count - 1
            later = AcademicTerm.objects.filter(pk=calendar.terms[index]).first() if index < len(calendar.terms) else None
            out["valid_until"] = later.ends_on if later else None
        else:
            out["valid_until"] = end
    elif scope == ReuseScope.ONE_SESSION:
        out["valid_until"] = session.ends_on
    elif scope == ReuseScope.MULTIPLE_SESSIONS:
        out["reuse_count"] = count
        index = calendar.sessions.index(session.id) + (count or 1) - 1
        later = AcademicSession.objects.filter(pk=calendar.sessions[index]).first() if index < len(calendar.sessions) else None
        out["valid_until"] = later.ends_on if later else None
    elif scope == ReuseScope.UNTIL_DATE:
        out["valid_until"] = date.fromisoformat(values["reuse_until"]) if values.get("reuse_until") else None
    else:  # indefinitely, until replaced
        out["valid_until"] = None
    return out


# -- what the school has on file about who pays -------------------------------------------------------


@dataclass(frozen=True)
class Payer:
    name: str = ""
    email: str = ""
    phone: str = ""
    identity: bool = False

    def has(self, requirement: str) -> bool:
        return bool(getattr(self, requirement))


def missing_details(payer: Payer, requirements) -> list[str]:
    return [FIELD_WORDS[r] for r in requirements if r in CHECKED_FIELDS and not payer.has(r)]


# -- the figures ------------------------------------------------------------------------------------


def classify(receivable: StudentReceivable, session, term) -> str:
    """Whether a charge belongs to this batch's period ("current"), to an earlier one ("previous"), or to a later one ("later")."""
    if receivable.session_id == session.id and (term is None or receivable.term_id in (None, term.id)):
        return "current"
    start = periods.window(session, term)[0]
    if periods.window(receivable.session, receivable.term)[1] < start:
        return "previous"
    return "later"


@dataclass
class Evaluation:
    family: Family
    previous_minor: int = 0
    current_minor: int = 0
    credit_minor: int = 0
    proposed_minor: int = 0
    breakdown: list = field(default_factory=list)
    arrears_policy: str = ArrearsPolicy.CARRY_FORWARD
    status: str = Eligibility.ELIGIBLE
    note: str = ""
    policy: dict = field(default_factory=dict)
    existing_action: str = "none"
    existing_account_id: str = ""
    missing: list = field(default_factory=list)


class Universe:
    """Everything the preview needs about a school's families, loaded in a handful of queries instead of a few per family."""

    def __init__(self, school, session, term, connection):
        self.school = school
        self.session, self.term, self.connection = session, term, connection
        self.calendar = Calendar(school)
        active = Family.objects.filter(school=school, status=FamilyStatus.ACTIVE, merged_into__isnull=True)
        with_students = set(FamilyStudent.objects.filter(school=school, is_active=True).values_list("family_id", flat=True))
        self.families = [f for f in active.order_by("display_name", "code") if f.id in with_students]
        ids = [f.id for f in self.families]
        self.receivables: dict = {}
        rows = list(
            StudentReceivable.objects.filter(school=school, family_id__in=ids).exclude(status=ReceivableStatus.VOID).select_related("session", "term")
        )
        self.positions = ledger.positions(rows)
        for row in rows:
            self.receivables.setdefault(row.family_id, []).append(row)
        self.credit: dict = {}
        for row in FamilyCreditEntry.objects.filter(school=school, family_id__in=ids).values("family_id", "kind").annotate(total=Sum("amount_minor")):
            sign = 1 if row["kind"] in CREDIT_IN else (-1 if row["kind"] in CREDIT_OUT else 0)
            self.credit[row["family_id"]] = self.credit.get(row["family_id"], 0) + sign * row["total"]
        self.accounts = {
            a.family_id: a for a in FamilyCollectionAccount.objects.filter(school=school, family_id__in=ids, status__in=LIVE_STATUSES)
        }
        guardians: dict = {}
        for link in FamilyGuardian.objects.filter(school=school, is_active=True, family_id__in=ids).select_related("guardian"):
            best = guardians.get(link.family_id)
            if best is None or (link.is_primary_payer and not best.is_primary_payer):
                guardians[link.family_id] = link
        identity = {i.family_id: i for i in FamilyPayerIdentity.objects.filter(school=school, family_id__in=ids)}
        self.payers = {}
        for family_id, link in guardians.items():
            on_file = identity.get(family_id)
            self.payers[family_id] = Payer(
                name=link.guardian.name.strip(), email=(link.guardian.email or "").strip(), phone=(link.guardian.phone or "").strip(),
                identity=bool(on_file and (on_file.has_bvn or on_file.has_nin)),
            )

    def figures(self, family_id) -> tuple[int, int, list]:
        previous = current = 0
        breakdown = []
        for r in self.receivables.get(family_id, []):
            outstanding = self.positions[r.id].outstanding
            if not outstanding:
                continue
            kind = classify(r, self.session, self.term)
            if kind == "current":
                current += outstanding
            elif kind == "previous":
                previous += outstanding
                breakdown.append({
                    "receivableId": str(r.id), "label": r.item_name, "period": periods.label(r.session, r.term),
                    "dueDate": r.due_date.isoformat(), "outstandingMinor": outstanding,
                })
        return previous, current, breakdown


# -- deciding ---------------------------------------------------------------------------------------


def account_action(account, universe: Universe) -> tuple[str, str]:
    """What the family's live account means for this batch: none, keep (it already covers the period), replace (it does not, and can be
    retired then remade), or conflict (something must happen to it first)."""
    if account is None:
        return "none", ""
    if account.origin == AccountOrigin.LEGACY_MANUAL or account.connection_id != universe.connection.id:
        return "conflict", "It has an account made with another provider (or recorded by hand). Retire that account first."
    if account.status == AccountStatus.CLOSING:
        return "conflict", "Its earlier account is still being closed."
    if account.status == AccountStatus.SUSPENDED:
        return "conflict", "Its account was suspended by a person. Reinstate or close it first."
    if account.status == AccountStatus.PROVISIONING or covers(account, universe.session, universe.term, universe.calendar):
        return "keep", "It already has an account that covers this period."
    return "replace", "Its earlier account no longer covers this period, so it is retired and remade."


def evaluate(universe: Universe, family: Family, resolved, prior) -> Evaluation:
    """One family's place in the batch. `prior` is the batch item it already has (its override and its choice of balances), or None."""
    info = universe.info
    previous, current, breakdown = universe.figures(family.id)
    values = resolved.values
    policy = values["arrears_policy"]
    chosen = set((prior.custom_arrears_receivable_ids if prior else None) or [])
    if policy == ArrearsPolicy.CARRY_FORWARD:
        carried = previous
    elif policy == ArrearsPolicy.CUSTOM_SELECTION:
        carried = sum(b["outstandingMinor"] for b in breakdown if b["receivableId"] in chosen)
    else:
        carried = 0
    credit = max(universe.credit.get(family.id, 0), 0)
    proposed = max(current + carried - credit, 0)
    evaluation = Evaluation(
        family=family, previous_minor=previous, current_minor=current, credit_minor=credit, proposed_minor=proposed, breakdown=breakdown,
        arrears_policy=policy, policy=resolved.snapshot(),
    )
    account = universe.accounts.get(family.id)
    action, why = account_action(account, universe)
    evaluation.existing_action = action
    evaluation.existing_account_id = str(account.id) if account else ""
    if action == "conflict":
        evaluation.status, evaluation.note = Eligibility.PROVIDER_CONFLICT, why
        return evaluation
    if action == "keep":
        evaluation.status, evaluation.note = Eligibility.HAS_ACCOUNT, why
        return evaluation
    mode = values["account_mode"]
    caps = info.capabilities
    if not (caps.supports_static_accounts if mode == AccountMode.STATIC else caps.supports_dynamic_accounts):
        evaluation.status = Eligibility.UNSUPPORTED_MODE
        evaluation.note = f"{info.display_name} does not issue {mode} accounts. Change the account type in the policy for this school, term, batch or family."
        return evaluation
    payer = universe.payers.get(family.id, Payer())
    evaluation.missing = missing_details(payer, info.customer_requirements)
    if evaluation.missing:
        evaluation.status = Eligibility.MISSING_DETAILS
        evaluation.note = f"{info.display_name} needs " + " and ".join(evaluation.missing) + " before it will make an account."
        return evaluation
    if previous > 0:
        rule = values["eligibility_policy"]
        if rule == EligibilityPolicy.EXCLUDE:
            evaluation.status, evaluation.note = Eligibility.EXCLUDED, "Still owes for an earlier term, and the school's policy leaves such families out."
            return evaluation
        if rule == EligibilityPolicy.NEEDS_OVERRIDE:
            evaluation.status, evaluation.note = Eligibility.NEEDS_OVERRIDE, "Still owes for an earlier term. A person must override to include them."
            return evaluation
        if rule == EligibilityPolicy.MANUAL_APPROVAL:
            evaluation.status, evaluation.note = Eligibility.MANUAL_APPROVAL, "Still owes for an earlier term. The approver must approve this family on its own."
            return evaluation
    if proposed == 0:
        evaluation.status = Eligibility.NOTHING_DUE
        evaluation.note = "Owes nothing for this period." if not info.requires_amount else (
            f"Owes nothing for this period, and {info.display_name} makes an account for an amount."
        )
        return evaluation
    evaluation.status, evaluation.note = Eligibility.ELIGIBLE, ""
    return evaluation


def can_select(status: str, *, override: bool, requires_amount: bool) -> tuple[bool, str]:
    """Whether a family in this state may be selected, and if not, why."""
    if status == Eligibility.NOTHING_DUE:
        return (not requires_amount), ("This provider makes an account for an amount, and there is nothing to collect." if requires_amount else "")
    if status in SELECTABLE:
        return True, ""
    if status in (Eligibility.NEEDS_OVERRIDE, Eligibility.EXCLUDED):
        return (True, "") if override else (False, "Override this family's eligibility first.")
    return False, "This family cannot be given an account in this batch yet."


# -- the batch's figures and fingerprint ---------------------------------------------------------------


def state_of(evaluation: Evaluation, prior, requires_amount: bool) -> dict:
    """The item's stored fields as they should be now: the evaluation, with the person's own choices (selection, override) kept where
    they still make sense."""
    override = bool(prior and prior.eligibility_override) and evaluation.status in (Eligibility.NEEDS_OVERRIDE, Eligibility.EXCLUDED)
    allowed, _ = can_select(evaluation.status, override=override, requires_amount=requires_amount)
    if prior is None:
        selected = evaluation.status == Eligibility.ELIGIBLE
    elif evaluation.status == Eligibility.ELIGIBLE and prior.eligibility_status != Eligibility.ELIGIBLE:
        selected = True  # it has just become eligible (its details were added, its balance cleared): offered like any eligible family
    else:
        selected = bool(prior.selected) and allowed
    return {
        "selected": selected,
        "previous_arrears_minor": evaluation.previous_minor, "current_due_minor": evaluation.current_minor, "credit_minor": evaluation.credit_minor,
        "proposed_collection_minor": evaluation.proposed_minor, "arrears_breakdown": evaluation.breakdown, "arrears_policy": evaluation.arrears_policy,
        "eligibility_status": evaluation.status, "eligibility_note": evaluation.note[:300],
        "eligibility_override": override, "override_reason": (prior.override_reason if override else ""), "policy_snapshot": evaluation.policy,
        "existing_action": evaluation.existing_action, "missing": evaluation.missing,
        "custom_arrears_receivable_ids": sorted((prior.custom_arrears_receivable_ids if prior else None) or []) if evaluation.arrears_policy == ArrearsPolicy.CUSTOM_SELECTION else [],
    }


def fingerprint(family_id, state: dict) -> str:
    """A fingerprint of everything that matters about one family in the batch: its figures, its eligibility, its selection and override,
    and the policy it would be made under. Two calls with the same facts give the same fingerprint."""
    body = {
        "family": str(family_id), "selected": bool(state["selected"]), "previous": state["previous_arrears_minor"], "current": state["current_due_minor"],
        "credit": state["credit_minor"], "proposed": state["proposed_collection_minor"], "status": str(state["eligibility_status"]),
        "override": [bool(state["eligibility_override"]), state.get("override_reason", "") if state["eligibility_override"] else ""],
        "arrears": [str(state["arrears_policy"]), sorted(state["custom_arrears_receivable_ids"])],
        "action": state.get("existing_action", ""), "missing": sorted(state.get("missing") or []),
        "policy": {name: (state["policy_snapshot"].get("values") or {}).get(name) for name in OVERRIDABLE},
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def batch_hash(batch, entries: list[tuple], policy_snapshot: dict | None = None) -> str:
    """The hash an approval is for. `entries` is `[(family_id, fingerprint)]` for the SELECTED families: changing which families are
    selected, or anything that matters about one, changes it."""
    header = {
        "batch": str(batch.id), "connection": str(batch.provider_connection_id), "provider": batch.provider, "environment": batch.environment,
        "session": str(batch.session_id), "term": str(batch.term_id) if batch.term_id else "",
        "policy": {name: ((policy_snapshot or batch.policy_snapshot).get("values") or {}).get(name) for name in OVERRIDABLE},
    }
    body = {"header": header, "items": sorted((str(f), fp) for f, fp in entries)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_universe(batch) -> Universe:
    from apps.bankconnect.providers import registry

    universe = Universe(batch.school, batch.session, batch.term, batch.provider_connection)
    universe.info = registry.get_connector(batch.provider).info
    return universe


def policy_context(batch) -> PolicyContext:
    return PolicyContext(batch.school, session=batch.session, term=batch.term, batch=batch)
