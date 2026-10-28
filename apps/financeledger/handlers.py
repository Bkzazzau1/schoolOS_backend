from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.receivables.permissions import can_manage_billing, can_operate_receivables
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .constants import (
    BANK_LINE, DATE, FEE_STRUCTURE, ID, MAX_AMOUNT, MAX_FEE_ITEMS, MAX_NOTE, PAYMENT, PAYMENT_METHODS, REMINDER,
    SECTIONS, TERM_DUE,
)
from .sync_ids import sync_safe_id


class FeeStructureHandler(EntityHandler):
    """What a section is charged for a term. Only a billing authority may set it; the whole Finance
    Office may read it, since recording a payment needs to know what is owed."""

    entity_type = FEE_STRUCTURE

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A fee structure cannot be deleted.")
        if not can_manage_billing(ctx.membership):
            raise Rejected("Only the owner, or someone the owner has given billing authority, can set fees.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        term = text(ctx.payload, "term", max_len=80)
        section = choice(ctx.payload.get("section"), SECTIONS, "section")
        if sync_safe_id(f"{term}|{section}") != ctx.entity_id:
            raise Rejected("id must match the record.")
        raw_items = ctx.payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise Rejected("Add at least one charge.")
        if len(raw_items) > MAX_FEE_ITEMS:
            raise Rejected("Too many charges.")
        items = []
        names = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise Rejected("Every charge must be an item.")
            name = text(raw, "name", max_len=80)
            amount = integer(raw, "amount", minimum=1, maximum=MAX_AMOUNT)
            lowered = name.lower()
            if lowered in names:
                raise Rejected("Two charges have the same name.")
            names.add(lowered)
            items.append({"name": name, "amount": amount})
        return {"term": term, "section": section, "items": items, "updatedAt": ctx.now}


class TermDueHandler(EntityHandler):
    """When fees for a term fall due. Only a billing authority may set it."""

    entity_type = TERM_DUE

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A due date cannot be deleted.")
        if not can_manage_billing(ctx.membership):
            raise Rejected("Only the owner, or someone the owner has given billing authority, can set due dates.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        term = text(ctx.payload, "term", max_len=80)
        if sync_safe_id(term) != ctx.entity_id:
            raise Rejected("id must match the record.")
        date = text(ctx.payload, "date", max_len=10)
        if not DATE.match(date):
            raise Rejected("date must be a real date.")
        return {"term": term, "date": date}


class PaymentHandler(EntityHandler):
    """Money received for a student. Recorded by the owner or the Finance Office; never edited once
    stored except to void it, with a reason - the amount, student and receipt never change."""

    entity_type = PAYMENT

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A payment cannot be deleted.")
        if not can_operate_receivables(ctx.membership):
            raise Rejected("Only the owner or the Finance Office can record payments.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That payment id is not valid.")
        if text(ctx.payload, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return self._void(ctx) if ctx.existing else self._create(ctx)

    def _create(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        student_id = text(p, "studentId", max_len=64)
        amount = integer(p, "amount", minimum=1, maximum=MAX_AMOUNT)
        method = choice(p.get("method"), PAYMENT_METHODS, "method")
        reference = text(p, "reference", max_len=80, required=False)
        if method != "Cash" and not reference:
            raise Rejected("Enter the reference so the payment can be reconciled.")
        if reference and self._reference_already_recorded(ctx, reference):
            raise Rejected("That reference has already been recorded on another payment.")
        count = SyncRecord.objects.filter(school=ctx.membership.school, entity_type=PAYMENT).count()
        return {
            "id": ctx.entity_id,
            "receiptNumber": f"RCT-{count + 1:06d}",
            "studentId": student_id,
            "studentName": text(p, "studentName", max_len=120),
            "className": text(p, "className", max_len=60),
            "term": text(p, "term", max_len=80),
            "amount": amount,
            "method": method,
            "receivedAt": ctx.now,
            "reference": reference,
            "recordedBy": str(ctx.membership.id),
            "note": text(p, "note", max_len=MAX_NOTE, required=False),
            "voidedReason": "",
        }

    def _void(self, ctx: MutationContext) -> dict[str, Any]:
        old, p = ctx.existing, ctx.payload
        if old["voidedReason"]:
            raise Rejected("This payment is already voided.")
        reason = text(p, "voidedReason", max_len=MAX_NOTE)
        for key in ("id", "receiptNumber", "studentId", "studentName", "className", "term", "amount",
                    "method", "receivedAt", "reference", "recordedBy", "note"):
            if p.get(key) != old.get(key):
                raise Rejected("A payment cannot be changed, only voided.")
        return {**old, "voidedReason": reason}

    @staticmethod
    def _reference_already_recorded(ctx: MutationContext, reference: str) -> bool:
        # Filtered in Python, not with a JSONField key lookup: those don't behave the same way
        # across SQLite (tests) and Postgres (production), and a school's payment history is
        # small enough that this is never a real cost.
        lowered = reference.lower()
        rows = SyncRecord.objects.filter(
            school=ctx.membership.school, entity_type=PAYMENT, deleted=False,
        ).values_list("payload", flat=True)
        return any(
            not (row.get("voidedReason") or "") and (row.get("reference") or "").strip().lower() == lowered
            for row in rows
        )


class ReminderHandler(EntityHandler):
    """A fee reminder queued for a family. Append-only: once queued, a reminder is never changed."""

    entity_type = REMINDER

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A reminder cannot be changed or deleted once queued.")
        if not can_operate_receivables(ctx.membership):
            raise Rejected("Only the owner or the Finance Office can queue a reminder.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That reminder id is not valid.")
        p = ctx.payload
        if text(p, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return {
            "id": ctx.entity_id,
            "studentId": text(p, "studentId", max_len=64),
            "studentName": text(p, "studentName", max_len=120),
            "term": text(p, "term", max_len=80),
            "level": integer(p, "level", minimum=1, maximum=3),
            "balance": integer(p, "balance", minimum=1, maximum=MAX_AMOUNT),
            "queuedAt": ctx.now,
            "message": text(p, "message", max_len=MAX_NOTE, required=False),
            "queuedBy": str(ctx.membership.id),
        }


class BankLineHandler(EntityHandler):
    """One line from the school's bank or POS statement. Append-only, and a reference can only be
    entered once, so the same real transaction is never matched twice."""

    entity_type = BANK_LINE

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation != "create":
            raise Rejected("A statement line cannot be changed or deleted once added.")
        if not can_operate_receivables(ctx.membership):
            raise Rejected("Only the owner or the Finance Office can add a statement line.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That line id is not valid.")
        p = ctx.payload
        if text(p, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        date = text(p, "date", max_len=10)
        if not DATE.match(date):
            raise Rejected("date must be a real date.")
        amount = integer(p, "amount", minimum=1, maximum=MAX_AMOUNT)
        reference = text(p, "reference", max_len=80)
        if self._reference_already_on_statement(ctx, reference):
            raise Rejected("That reference is already on the statement.")
        return {
            "id": ctx.entity_id,
            "date": date,
            "amount": amount,
            "reference": reference,
            "narration": text(p, "narration", max_len=MAX_NOTE, required=False),
            "addedBy": str(ctx.membership.id),
        }

    @staticmethod
    def _reference_already_on_statement(ctx: MutationContext, reference: str) -> bool:
        lowered = reference.lower()
        rows = SyncRecord.objects.filter(
            school=ctx.membership.school, entity_type=BANK_LINE, deleted=False,
        ).values_list("payload", flat=True)
        return any((row.get("reference") or "").strip().lower() == lowered for row in rows)
