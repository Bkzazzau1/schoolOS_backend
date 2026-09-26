"""What a family could be debited: worked out from the receivables ledger, the mandate and the provider, never from what a screen says.

The rule that governs everything here: a MANDATE authorises a debit mechanism; it does not define the family's debt. Whatever a mandate's maximum
is, the proposed debit comes from what the ledger says the family owes in the batch's scope,

    ledger position (what is owed, less any credit the family already holds)
        -> the amount that could be collected
            -> the mandate's own limit (and the provider's, for a provider that limits a whole month)
                -> the proposed debit, and exactly how it would pay the family's charges (in payment order),

and it can only ever be lowered by a person afterwards. The same evaluation is run again just before the provider is asked (see execution.py), so a
family that paid in the meantime is never debited for what it no longer owes.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from django.db.models import Sum

from apps.receivables import credit, ledger, periods
from apps.receivables.models import Family

from .constants import LIVE_MANDATE, DebitOutcome, Eligibility
from .mandates import is_debit_ready
from .models import DirectDebitMandate, MandateTransaction
from .providers import registry

#: A family that can be put in a batch and selected.
SELECTABLE = (Eligibility.ELIGIBLE,)


@dataclass
class Evaluation:
    family: Family
    mandate: DirectDebitMandate | None = None
    outstanding_minor: int = 0
    eligible_minor: int = 0
    maximum_minor: int | None = None
    proposed_minor: int = 0
    allocation: list = field(default_factory=list)
    status: str = Eligibility.ELIGIBLE
    note: str = ""
    ready: bool = False
    adjusted: bool = False

    @property
    def receivable_ids(self) -> list[str]:
        return [row["receivableId"] for row in self.allocation]


# -- the ledger side ----------------------------------------------------------------------------------


def scope_ids(family: Family, session_id, term_id) -> set[str]:
    """The charges this batch is for: the family's live charges in the session (and term, if one was chosen)."""
    rows = ledger.live_receivables(family).filter(session_id=session_id)
    if term_id:
        rows = rows.filter(term_id=term_id)
    return {str(pk) for pk in rows.values_list("id", flat=True)}


def owed_in_scope(family: Family, session_id, term_id, *, today: date) -> list:
    """`[(receivable, outstanding)]` for the charges in scope that still owe something, in payment order."""
    return credit.outstanding_in_order(family, today=today, only=scope_ids(family, session_id, term_id))


def allocation_for(owed: list, amount_minor: int) -> list[dict]:
    """How `amount_minor` would pay the charges, in payment order: exactly what the settlement engine will do with a payment of that size."""
    remaining, plan = amount_minor, []
    for receivable, outstanding in owed:
        if remaining <= 0:
            break
        take = min(remaining, outstanding)
        plan.append({"receivableId": str(receivable.id), "amountMinor": take})
        remaining -= take
    return plan


# -- the mandate side ---------------------------------------------------------------------------------


def _month_bounds(today: date) -> tuple:
    first = today.replace(day=1)
    nxt = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
    return first, nxt


def used_this_month(mandate: DirectDebitMandate, today: date) -> tuple[int, int]:
    """What has been (or may have been) debited on the mandate this calendar month: the total and the number of debits. A debit whose outcome is
    not known yet counts, because it may have happened."""
    first, nxt = _month_bounds(today)
    rows = MandateTransaction.objects.filter(
        mandate=mandate, created_at__date__gte=first, created_at__date__lt=nxt,
        status__in=[DebitOutcome.PENDING, DebitOutcome.SUCCESS, DebitOutcome.UNKNOWN],
    )
    return rows.aggregate(t=Sum("amount_minor"))["t"] or 0, rows.count()


def limit_of(mandate: DirectDebitMandate, today: date) -> tuple[int, str]:
    """The most one more debit on this mandate could be, and why it is what it is (when it is zero)."""
    maximum = mandate.maximum_amount_minor
    connector = registry.get_connector(mandate.provider)
    if maximum is None:
        return 10**15, ""
    if connector is not None and connector.info.maximum_scope == "calendar_month":
        used, count = used_this_month(mandate, today)
        if mandate.max_debits is not None and count >= mandate.max_debits:
            return 0, "The most debits this month has been reached."
        left = max(maximum - used, 0)
        return left, ("The mandate's limit for this month is used." if left == 0 else "")
    return maximum, ""


def pick_mandate(family: Family, *, today: date):
    """The mandate a debit for this family would use: the primary one if it can be debited, otherwise another that can. Returns
    `(mandate, ready, reason)`; `mandate` is None only when the family has no live mandate at all."""
    live = list(
        DirectDebitMandate.objects.select_related("provider_connection", "payer__guardian").filter(family=family, status__in=LIVE_MANDATE)
        .order_by("-is_primary", "-created_at")
    )
    if not live:
        return None, False, ""
    for mandate in live:
        ok, _ = is_debit_ready(mandate, today=today)
        if ok:
            return mandate, True, ""
    first = live[0]
    return first, False, is_debit_ready(first, today=today)[1]


# -- one family ---------------------------------------------------------------------------------------


def evaluate(session_id, term_id, family: Family, *, today: date | None = None, adjusted_to: int | None = None, mandate: DirectDebitMandate | None = None) -> Evaluation:
    """Everything about debiting one family for a batch's scope, from the ledger and the mandate as they are right now. `adjusted_to` is a
    person's lower amount: it is applied only if it is still no more than the ledger allows. `mandate` pins the mandate an instruction was
    prepared with (the one to check again before debiting)."""
    today = today or periods.school_today()
    result = Evaluation(family=family)
    owed = owed_in_scope(family, session_id, term_id, today=today)
    result.outstanding_minor = sum(outstanding for _, outstanding in owed)
    result.eligible_minor = max(result.outstanding_minor - ledger.credit_balance(family), 0)
    if mandate is not None:
        chosen, ready = mandate, is_debit_ready(mandate, today=today)[0]
        reason = "" if ready else is_debit_ready(mandate, today=today)[1]
    else:
        chosen, ready, reason = pick_mandate(family, today=today)
    result.mandate, result.ready = chosen, ready
    if chosen is None:
        result.status, result.note = Eligibility.NO_MANDATE, "The family has no direct-debit mandate."
        return result
    if result.eligible_minor <= 0:
        result.status, result.note = Eligibility.NOTHING_DUE, "The ledger says nothing is left to collect for this period."
        return result
    connector = registry.get_connector(chosen.provider)
    if not ready:
        result.status = Eligibility.NOT_READY
        result.note = reason or "The mandate cannot be debited yet."
        if connector is not None and not connector.info.capabilities.supports_manual_debit:
            result.status = Eligibility.PROVIDER_CANNOT_DEBIT
        elif chosen.provider_connection.status != "connected":
            result.status = Eligibility.PROVIDER_UNAVAILABLE
        return result
    limit, why = limit_of(chosen, today)
    result.maximum_minor = limit
    if limit <= 0:
        result.status, result.note = Eligibility.NOT_READY, why or "The mandate's limit leaves nothing to debit."
        return result
    computed = min(result.eligible_minor, limit)
    proposed = computed
    if adjusted_to is not None and 0 < adjusted_to <= computed:
        proposed, result.adjusted = adjusted_to, adjusted_to < computed
    result.proposed_minor = proposed
    result.allocation = allocation_for(owed, proposed)
    result.status, result.note = Eligibility.ELIGIBLE, ""
    return result


# -- fingerprints ------------------------------------------------------------------------------------


def fingerprint(*, family_id, mandate_id, connection_id, provider: str, outstanding_minor: int, eligible_minor: int, maximum_minor, proposed_minor: int,
                allocation: list, ready: bool, selected: bool) -> str:
    """A deterministic fingerprint of everything about one debit that matters: who, through what, how much, and how it would pay. It changes if any
    of it does - including the family's balance - and that is what withdraws an approval."""
    body = {
        "family": str(family_id), "mandate": str(mandate_id or ""), "connection": str(connection_id or ""), "provider": provider,
        "outstanding": outstanding_minor, "eligible": eligible_minor, "maximum": maximum_minor, "proposed": proposed_minor,
        "allocation": [(a["receivableId"], a["amountMinor"]) for a in allocation], "ready": bool(ready), "selected": bool(selected),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fingerprint_of_evaluation(ev: Evaluation, *, selected: bool) -> str:
    return fingerprint(
        family_id=ev.family.id, mandate_id=ev.mandate.id if ev.mandate else None, connection_id=ev.mandate.provider_connection_id if ev.mandate else None,
        provider=ev.mandate.provider if ev.mandate else "", outstanding_minor=ev.outstanding_minor, eligible_minor=ev.eligible_minor,
        maximum_minor=ev.maximum_minor, proposed_minor=ev.proposed_minor, allocation=ev.allocation, ready=ev.ready, selected=selected,
    )


def batch_hash(batch, entries: list[tuple]) -> str:
    """The batch's fingerprint: what it is for, and every SELECTED debit's fingerprint. The checker approves this."""
    header = {"school": str(batch.school_id), "session": str(batch.session_id), "term": str(batch.term_id or "")}
    body = {"header": header, "items": sorted((str(f), fp) for f, fp in entries)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

