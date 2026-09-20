"""The steps a batch goes through, and who may take each.

    prepared --approve--> approved --instruct--> disbursementInstructed
    prepared --reject---> rejected --prepare again--> prepared

Each step returns the record to store. The server stamps who and when, keeps the
lines it has, and appends to the batch's trail. Nothing the app sends outside a
step's own inputs (the lines when preparing, the reason when rejecting) is used.
A step never marks anyone as paid: that needs real bank evidence.
"""

from apps.core.errors import Rejected
from apps.owner.payroll.access import payroll_authorities

from . import lines as batch_lines
from .constants import APPROVED, INSTRUCTED, MAX_NOTE, PREPARED, REJECTED


def _need(ctx, authority: str, action: str) -> None:
    if authority not in payroll_authorities(ctx.membership):
        raise Rejected(f"You are not authorized to {action}.")


def _trail(old: dict | None, ctx, action: str, note: str = "") -> list[dict]:
    entry = {"action": action, "byMembershipId": str(ctx.membership.id), "at": ctx.now}
    if note:
        entry["note"] = note
    return [*((old or {}).get("trail") or []), entry]


def prepare(ctx, old: dict | None) -> dict:
    _need(ctx, "prepare", "prepare payroll batches")
    lines, total = batch_lines.build(ctx.membership.school, ctx.payload.get("lines"))
    return {
        "period": ctx.entity_id,
        "status": PREPARED,
        "lines": lines,
        "total": total,
        "preparedByMembershipId": str(ctx.membership.id),
        "preparedAt": ctx.now,
        "trail": _trail(old, ctx, "prepared"),
        "updatedAt": ctx.now,
    }


def approve(ctx, old: dict) -> dict:
    _need(ctx, "approve", "approve payroll batches")
    if old["preparedByMembershipId"] == str(ctx.membership.id):
        raise Rejected("A different person must approve a batch you prepared.")
    changed = batch_lines.still_current(ctx.membership.school, old["lines"])
    if changed:
        raise Rejected(f"The salary for {changed} changed after this batch was prepared. Ask for it to be prepared again.")
    return {
        **old, "status": APPROVED, "approvedByMembershipId": str(ctx.membership.id), "approvedAt": ctx.now,
        "trail": _trail(old, ctx, "approved"), "updatedAt": ctx.now,
    }


def reject(ctx, old: dict) -> dict:
    _need(ctx, "approve", "reject payroll batches")
    reason = ctx.payload.get("rejectionReason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise Rejected("Say why the batch is rejected.")
    if len(reason) > MAX_NOTE:
        raise Rejected("The reason is too long.")
    return {
        **old, "status": REJECTED, "rejectionReason": reason, "rejectedByMembershipId": str(ctx.membership.id),
        "trail": _trail(old, ctx, "rejected", reason), "updatedAt": ctx.now,
    }


def instruct(ctx, old: dict) -> dict:
    _need(ctx, "pay", "make payroll payments")
    return {
        **old, "status": INSTRUCTED, "instructedByMembershipId": str(ctx.membership.id), "instructedAt": ctx.now,
        "trail": _trail(old, ctx, "instructed"), "updatedAt": ctx.now,
    }


#: (stored status, requested status) -> the step. Anything else is refused.
STEPS = {
    (None, PREPARED): prepare,
    (PREPARED, PREPARED): prepare,
    (REJECTED, PREPARED): prepare,
    (PREPARED, APPROVED): approve,
    (PREPARED, REJECTED): reject,
    (APPROVED, INSTRUCTED): instruct,
}
