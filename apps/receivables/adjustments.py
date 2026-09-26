"""Deciding that a family owes less: discounts, scholarships, waivers and corrections - and voiding a charge.

Only someone with billing authority at the school can do any of it. None of it rewrites a charge: the
gross amount stays exactly as published, and each decision is a separate, append-only adjustment row
with who decided, when and why. An adjustment is undone by a REVERSAL row, never by editing or deleting.

If an adjustment leaves a charge needing less than has already been put towards it, the excess is not
discarded: it is taken off the charge and held as family credit (see `allocation.rebalance`).
"""

import uuid
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.core.money import format_money

from . import allocation, audit, credit, ledger
from .errors import Refused
from .models import AdjustmentKind, ReceivableAdjustment, ReceivableStatus, StudentReceivable
from .permissions import require_billing_authority

MAX_REASON = 300


def _reason(reason) -> str:
    cleaned = " ".join(str(reason or "").split())
    if not cleaned:
        raise Refused("Say why: a reason is needed for every adjustment.", "reason_required")
    return cleaned[:MAX_REASON]


def _amount(amount) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise Refused("An amount must be a whole number of kobo greater than zero.", "invalid_amount")
    return amount


def _fresh(receivable: StudentReceivable) -> StudentReceivable:
    return StudentReceivable.objects.select_for_update().get(pk=receivable.pk)


def _apply(receivable, kind, amount, reason, actor, requested_by, source_ref, metadata, group_id) -> ReceivableAdjustment:
    """One adjustment on one charge. The caller holds the family row and settles the family afterwards."""
    if source_ref:
        existing = ReceivableAdjustment.objects.filter(receivable=receivable, source_ref=source_ref).first()
        if existing is not None:
            return existing  # the same decision applied twice is one decision
    if receivable.status == ReceivableStatus.VOID:
        raise Refused("That charge has been voided.", "receivable_void")
    pos = ledger.position(receivable)
    if amount > pos.net:
        raise Refused(
            f"That is more than the {format_money(pos.net, receivable.currency)} still payable on '{receivable.item_name}'.",
            "exceeds_charge",
        )
    adjustment = ReceivableAdjustment.objects.create(
        school=receivable.school, receivable=receivable, kind=kind, amount_minor=amount, reason=reason,
        group_id=group_id, requested_by=requested_by or actor, authorized_by=actor, authorized_at=timezone.now(),
        source_ref=source_ref, metadata=metadata or {},
    )
    allocation.rebalance(receivable, actor=actor, reason=f"{kind} of {format_money(amount, receivable.currency)}")
    ledger.refresh_status(receivable)
    audit.record(
        receivable.school, "adjustment_applied", actor=actor, obj=adjustment, receivable=str(receivable.id),
        adjustment=kind, amount=amount, reason=reason, source=source_ref,
    )
    return adjustment


def _check_kind(kind) -> None:
    if kind not in AdjustmentKind.values:
        raise Refused("That is not a kind of adjustment.", "invalid_kind")


@transaction.atomic
def adjust(
    receivable: StudentReceivable, *, kind: str, amount_minor: int, reason, actor, requested_by=None,
    source_ref: str = "", metadata=None,
) -> ReceivableAdjustment:
    """Take an amount off one charge."""
    require_billing_authority(actor, receivable.school)
    _check_kind(kind)
    amount, reason = _amount(amount_minor), _reason(reason)
    family = credit.lock_family(receivable.family)
    adjustment = _apply(_fresh(receivable), kind, amount, reason, actor, requested_by, source_ref, metadata, None)
    from . import lifecycle

    lifecycle.settle_family(family, actor)
    return adjustment


def _spread(amount: int, weights: list[int]) -> list[int]:
    """`amount` shared out in proportion to `weights`, in whole kobo, adding up exactly and never giving any
    part more than its weight. The same input always gives the same shares."""
    total = sum(weights)
    shares = [amount * w // total for w in weights]
    leftover = amount - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total), i))
    for index in order[:leftover]:
        shares[index] += 1
    return shares


@transaction.atomic
def adjust_charge(
    receivable: StudentReceivable, *, kind: str, amount_minor: int, reason, actor, requested_by=None,
    source_ref: str = "", metadata=None,
) -> list[ReceivableAdjustment]:
    """Take an amount off a whole CHARGE, spreading it over its instalments in proportion to what each still
    requires, so a scholarship affects every instalment consistently."""
    require_billing_authority(actor, receivable.school)
    _check_kind(kind)
    amount, reason = _amount(amount_minor), _reason(reason)
    family = credit.lock_family(receivable.family)
    parts = list(
        StudentReceivable.objects.select_for_update().filter(charge_key=receivable.charge_key, family=family)
        .exclude(status=ReceivableStatus.VOID).order_by("installment_number")
    )
    if not parts:
        raise Refused("That charge has been voided.", "receivable_void")
    if source_ref:
        done = list(ReceivableAdjustment.objects.filter(receivable__in=parts, source_ref=source_ref))
        if done:
            return done
    figures = ledger.positions(parts)
    payable = sum(figures[p.id].net for p in parts)
    if amount > payable:
        raise Refused(
            f"That is more than the {format_money(payable, receivable.currency)} still payable on '{receivable.item_name}'.",
            "exceeds_charge",
        )
    group = uuid.uuid4()
    made = [
        _apply(part, kind, share, reason, actor, requested_by, source_ref, metadata, group)
        for part, share in zip(parts, _spread(amount, [figures[p.id].net for p in parts])) if share > 0
    ]
    from . import lifecycle

    lifecycle.settle_family(family, actor)
    return made


@transaction.atomic
def reverse(adjustment: ReceivableAdjustment, *, actor, reason) -> ReceivableAdjustment:
    """Undo an adjustment. The charge needs its full amount again; the decision and its undoing both stay on record."""
    require_billing_authority(actor, adjustment.school)
    reason = _reason(reason)
    family = credit.lock_family(adjustment.receivable.family)
    adjustment = ReceivableAdjustment.objects.select_for_update().get(pk=adjustment.pk)
    if adjustment.reverses_id:
        raise Refused("A reversal cannot itself be reversed.", "already_a_reversal")
    if ReceivableAdjustment.objects.filter(reverses=adjustment).exists():
        raise Refused("That adjustment was already reversed.", "already_reversed")
    receivable = _fresh(adjustment.receivable)
    if receivable.status == ReceivableStatus.VOID:
        raise Refused("That charge has been voided.", "receivable_void")
    reversal = ReceivableAdjustment.objects.create(
        school=adjustment.school, receivable=receivable, kind=adjustment.kind, amount_minor=adjustment.amount_minor,
        reason=reason, reverses=adjustment, group_id=adjustment.group_id, requested_by=actor, authorized_by=actor,
        authorized_at=timezone.now(),
    )
    ledger.refresh_status(receivable)
    audit.record(
        adjustment.school, "adjustment_reversed", actor=actor, obj=reversal, receivable=str(receivable.id),
        original=str(adjustment.id), amount=adjustment.amount_minor, reason=reason,
    )
    from . import lifecycle

    lifecycle.settle_family(family, actor)
    return reversal


@transaction.atomic
def void_receivable(receivable: StudentReceivable, *, actor, reason) -> StudentReceivable:
    """Cancel a charge that should never have been raised. It stays on record, marked void, with who and why.
    Anything already paid towards it is released as family credit, never lost."""
    require_billing_authority(actor, receivable.school)
    reason = _reason(reason)
    family = credit.lock_family(receivable.family)
    receivable = _fresh(receivable)
    if receivable.status == ReceivableStatus.VOID:
        raise Refused("That charge is already void.", "receivable_void")
    released = allocation.rebalance(receivable, actor=actor, reason=f"Charge voided: {reason}", void=True)
    receivable.status, receivable.voided_at, receivable.voided_by, receivable.void_reason = (
        ReceivableStatus.VOID, timezone.now(), actor, reason,
    )
    receivable.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    audit.record(
        receivable.school, "receivable_voided", actor=actor, obj=receivable, reason=reason,
        item=receivable.item_name, gross=receivable.gross_amount_minor, released_to_credit=released,
    )
    from . import lifecycle

    lifecycle.settle_family(family, actor)
    return receivable


@transaction.atomic
def void_schedule_charges(schedule, *, actor, reason, include_paid: bool = False, today: date | None = None) -> dict:
    """Void the charges a schedule raised, when the schedule itself was wrong. By default only charges nobody has
    paid anything towards are voided; the rest are returned for a person to decide about."""
    require_billing_authority(actor, schedule.school)
    voided, skipped = 0, []
    receivables = list(schedule.receivables.exclude(status=ReceivableStatus.VOID))
    figures = ledger.positions(receivables)
    for receivable in receivables:
        if figures[receivable.id].paid > 0 and not include_paid:
            skipped.append(receivable)
            continue
        void_receivable(receivable, actor=actor, reason=reason)
        voided += 1
    return {"voided": voided, "skipped_paid": skipped}
