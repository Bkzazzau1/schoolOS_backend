"""Folding one family into another, when a school finds two households are really one.

It happens: siblings entered under different surnames, a guardian who registered twice, two families that
became one. The school needs ONE ledger and ONE place to pay, without losing anything already recorded.

A merge moves the SOURCE family's whole financial life into the family it is merged INTO:

* its students and its payers (a payer both families had is kept once; only one primary payer remains);
* every charge it was billed, with the adjustments, payments and allocations against them;
* its credit ledger, so credit it held pays what the merged household owes (settled straight away);
* the payments recorded for it, and the statements issued to it (their numbers are kept);
* its payment accounts - except where the target already has a live account with the same bank (a family
  holds one per bank). Such an account stays attached to the old family, which points at the survivor, and a
  payment into it is credited to the merged family: no number a family was given ever stops working.

The source is left as an INACTIVE family with `merged_into` set and its code intact, never deleted. It is not
reversible here: undoing a merge would mean re-deciding which of the moved payments belonged to whom.
Everything is one transaction, every family involved is locked first (in a fixed order, so two merges cannot
deadlock), and the result is checked against the ledger's invariants before it is kept.

Merging changes who pays for what, so it needs BILLING AUTHORITY (the owner, or someone the owner has given it),
not just the finance office's day-to-day access. It is audited with the reason.
"""

from django.db import transaction
from django.utils import timezone

from apps.bankconnect.models import BankTransaction, TransactionAllocation

from . import audit, ledger, lifecycle
from .errors import Refused
from .families import active_students
from .models import (
    AccountStatus, Family, FamilyCollectionAccount, FamilyCreditEntry, FamilyGuardian, FamilyStatement, FamilyStatus, FamilyStudent,
    StudentReceivable,
)
from .permissions import require_billing_authority, require_operator

MAX_REASON = 300


def _problems(source: Family, into: Family) -> list[tuple[str, str]]:
    """Why these two cannot be merged, as `(code, words)`. Empty when they can."""
    found = []
    if source.pk == into.pk:
        found.append(("same_family", "A family cannot be merged into itself."))
    if source.school_id != into.school_id:
        found.append(("family_not_found", "Both families must be at the same school."))
    if source.merged_into_id is not None:
        found.append(("already_merged", f"{source.display_name} has already been merged into another family."))
    elif source.status != FamilyStatus.ACTIVE:
        found.append(("family_closed", f"{source.display_name} is closed. Reopen it to merge it."))
    if into.merged_into_id is not None:
        found.append(("target_merged", f"{into.display_name} has itself been merged into another family. Merge into that one."))
    elif into.status != FamilyStatus.ACTIVE:
        found.append(("target_closed", f"{into.display_name} is closed. Reopen it to merge into it."))
    return found


def _live_providers(family: Family) -> set:
    return set(FamilyCollectionAccount.objects.filter(family=family).exclude(status=AccountStatus.CLOSED).values_list("provider", flat=True))


def _brief(family: Family) -> dict:
    position = ledger.family_position(family)
    return {
        "id": str(family.id), "code": family.code, "name": family.display_name, "status": family.status,
        "students": [s.full_name for s in active_students(family).order_by("surname", "first_name", "id")], "outstandingMinor": position.outstanding,
        "creditMinor": position.credit, "charges": position.charges,
    }


def preview(source: Family, into: Family, *, actor) -> dict:
    """What a merge WOULD do, without doing it: who and what moves, what stays behind and why, and anything that would refuse it."""
    require_operator(actor, source.school)
    if into.school_id != source.school_id:
        raise Refused("That family was not found.", "family_not_found")
    keep = _live_providers(into)
    accounts = list(FamilyCollectionAccount.objects.filter(family=source).exclude(status=AccountStatus.CLOSED))
    theirs = {g.guardian_id for g in FamilyGuardian.objects.filter(family=into)}
    return {
        "problems": [{"code": code, "message": message} for code, message in _problems(source, into)],
        "source": _brief(source), "into": _brief(into),
        "moves": {
            "students": FamilyStudent.objects.filter(family=source, is_active=True).count(),
            "charges": StudentReceivable.objects.filter(family=source).count(),
            "creditEntries": FamilyCreditEntry.objects.filter(family=source).count(),
            "payments": BankTransaction.objects.filter(family=source).count(),
            "statements": FamilyStatement.objects.filter(family=source).count(),
            "payers": FamilyGuardian.objects.filter(family=source, is_active=True).exclude(guardian_id__in=theirs).count(),
        },
        "accountsMoved": [{"provider": a.provider, "bankName": a.bank_name} for a in accounts if a.provider not in keep],
        "accountsKeptAsIs": [{"provider": a.provider, "bankName": a.bank_name} for a in accounts if a.provider in keep],
        "irreversible": True,
    }


def _move_payers(source: Family, into: Family) -> int:
    """Move the source's payers over; a guardian the target already has stays as it was, and there is at most one primary."""
    moved = 0
    target_has_primary = FamilyGuardian.objects.filter(family=into, is_active=True, is_primary_payer=True).exists()
    have = {g.guardian_id: g for g in FamilyGuardian.objects.select_for_update().filter(family=into)}
    for link in FamilyGuardian.objects.select_for_update().filter(family=source).order_by("-is_primary_payer", "created_at", "id"):
        existing = have.get(link.guardian_id)
        if existing is not None:
            # The survivor already has this guardian on record (a child once moved between the two): keep that one,
            # switch it on if this one was live, and let this copy stop. A guardian is only ever listed once per family.
            if link.is_active and not existing.is_active:
                existing.is_active = True
                if link.is_primary_payer and not target_has_primary:
                    existing.is_primary_payer, target_has_primary = True, True
                existing.save(update_fields=["is_active", "is_primary_payer"])
            link.is_active, link.is_primary_payer = False, False
            link.save(update_fields=["is_active", "is_primary_payer"])
            continue
        if link.is_primary_payer and target_has_primary:
            link.is_primary_payer = False
        target_has_primary = target_has_primary or (link.is_primary_payer and link.is_active)
        link.family = into
        link.save(update_fields=["family", "is_primary_payer"])
        have[link.guardian_id] = link
        moved += 1
    return moved


def _move_accounts(source: Family, into: Family) -> tuple[int, int]:
    """Move the source's accounts, except a live one where the target already has a live account with that bank."""
    keep = _live_providers(into)
    moved = left = 0
    for account in FamilyCollectionAccount.objects.select_for_update().filter(family=source).order_by("created_at", "id"):
        if account.status != AccountStatus.CLOSED and account.provider in keep:
            left += 1
            continue
        FamilyCollectionAccount.objects.filter(pk=account.pk).update(family=into)
        moved += 1
    return moved, left


@transaction.atomic
def merge(source: Family, into: Family, *, actor, reason) -> dict:
    """Fold `source` into `into`. Returns what moved."""
    require_billing_authority(actor, source.school)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("Say why these families are being merged.", "reason_required")
    if len(reason) > MAX_REASON:
        raise Refused(f"A reason can be at most {MAX_REASON} characters.", "reason_too_long")
    if into.school_id != source.school_id:
        raise Refused("That family was not found.", "family_not_found")
    locked = {f.pk: f for f in Family.objects.select_for_update().filter(pk__in=[source.pk, into.pk]).order_by("pk")}  # fixed order: no deadlock
    source, into = locked[source.pk], locked[into.pk]
    problems = _problems(source, into)
    if problems:
        raise Refused(problems[0][1], problems[0][0])

    before = {"source": ledger.family_position(source), "into": ledger.family_position(into)}
    moved = {
        "students": FamilyStudent.objects.filter(family=source, is_active=True).count(),
        "charges": StudentReceivable.objects.filter(family=source).count(),
        "creditEntries": FamilyCreditEntry.objects.filter(family=source).count(),
        "payments": BankTransaction.objects.filter(family=source).count(),
        "statements": FamilyStatement.objects.filter(family=source).count(),
    }
    # Row-by-row saves guard what a charge or an allocation may change; this is the one deliberate exception, so it
    # is done as plain updates, inside this transaction, and checked against the ledger's invariants below.
    FamilyStudent.objects.filter(family=source).update(family=into)
    moved["payers"] = _move_payers(source, into)
    StudentReceivable.objects.filter(family=source).update(family=into)
    FamilyCreditEntry.objects.filter(family=source).update(family=into)
    BankTransaction.objects.filter(family=source).update(family=into)
    TransactionAllocation.objects.filter(family=source).update(family=into)
    FamilyStatement.objects.filter(family=source).update(family=into)
    moved["accounts"], moved["accountsLeftInPlace"] = _move_accounts(source, into)
    Family.objects.filter(merged_into=source).update(merged_into=into)  # keep merges one level deep

    source.status, source.merged_into, source.merged_at = FamilyStatus.INACTIVE, into, timezone.now()
    source.save(update_fields=["status", "merged_into", "merged_at", "updated_at"])

    # The two households' credit and debts are now one: use the credit, refresh statuses, wake or rest accounts.
    after = lifecycle.settle_family(into, actor, announce_reactivation=False)
    problems = ledger.verify_family(into)
    if problems:
        raise Refused("The merge would have left the ledger inconsistent, so nothing was changed.", "merge_inconsistent")
    audit.record(
        into.school, "families_merged", actor=actor, obj=into, source=str(source.id), source_code=source.code, reason=reason,
        moved=moved, outstanding_before=before["source"].outstanding + before["into"].outstanding, outstanding_after=after.outstanding,
    )
    return {"into": _brief(into), "source": {"id": str(source.id), "code": source.code, "mergedInto": str(into.id)}, "moved": moved}
