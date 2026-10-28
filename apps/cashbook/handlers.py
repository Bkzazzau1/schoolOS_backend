from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.notifications.services import notify
from apps.receivables.permissions import billing_authorities, can_manage_billing, can_operate_receivables
from apps.sync.registry import EntityHandler, MutationContext

from .constants import (
    APPROVED, CASHFLOW_ENTRY, CATEGORIES, DATE, DECISIONS, DECLINED, EXPENSE, ID, INCOME, MAX_AMOUNT, MAX_NOTE,
    PENDING, POSTED, TYPES,
)


class CashflowEntryHandler(EntityHandler):
    """The school's own income and spending, outside the fee ledger. Income posts the moment it is
    recorded; an expense needs a billing authority's decision before it counts as spent - the same
    two-tier split (operate day to day, a billing authority decides) apps.receivables.permissions
    already uses, mirrored exactly rather than invented fresh."""

    entity_type = CASHFLOW_ENTRY

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A cash flow entry cannot be deleted.")
        if ctx.operation == "create":
            if not can_operate_receivables(ctx.membership):
                raise Rejected("Only the owner or the Finance Office can record income or an expense.")
            return
        if not can_manage_billing(ctx.membership):
            raise Rejected("Only the owner, or someone the owner has given billing authority, can decide an expense.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That entry id is not valid.")
        if text(ctx.payload, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return self._decide(ctx) if ctx.existing else self._create(ctx)

    def _create(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        kind = choice(p.get("type"), TYPES, "type")
        date = text(p, "date", max_len=10)
        if not DATE.match(date):
            raise Rejected("date must be a real date.")
        amount = integer(p, "amount", minimum=1, maximum=MAX_AMOUNT)
        category = choice(p.get("category"), CATEGORIES, "category")
        description = text(p, "description", max_len=120)
        return {
            "id": ctx.entity_id,
            "date": date,
            "description": description,
            "type": kind,
            "category": category,
            "amount": amount,
            "status": POSTED if kind == INCOME else PENDING,
            "recordedBy": str(ctx.membership.id),
            "createdAt": ctx.now,
            "decidedBy": None,
            "decidedAt": None,
            "decisionNote": None,
        }

    def _decide(self, ctx: MutationContext) -> dict[str, Any]:
        old, p = ctx.existing, ctx.payload
        if old["type"] != EXPENSE:
            raise Rejected("Income is posted immediately and cannot be changed.")
        if old["status"] != PENDING:
            raise Rejected(f"This expense was already {'approved' if old['status'] == APPROVED else 'declined'}.")
        status = choice(p.get("status"), DECISIONS, "status")
        note = text(p, "decisionNote", max_len=MAX_NOTE, required=status == DECLINED)
        return {
            **old,
            "status": status,
            "decidedBy": str(ctx.membership.id),
            "decidedAt": ctx.now,
            "decisionNote": note or None,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        if stored["type"] != EXPENSE:
            return
        money = f"₦{stored['amount']:,}"
        if ctx.operation == "create":
            for authority in billing_authorities(ctx.membership.school):
                if authority.id != ctx.membership.id:
                    notify(
                        authority, "expense_request", "Expense to approve",
                        f"{stored['description']} ({money}) needs your approval.",
                        {"entryId": ctx.entity_id},
                    )
            return
        from apps.schools.models import Membership

        preparer = Membership.objects.filter(id=stored["recordedBy"]).select_related("school").first()
        if preparer is not None and preparer.id != ctx.membership.id:
            verdict = "approved" if stored["status"] == APPROVED else "declined"
            note = f" {stored['decisionNote']}" if stored["decisionNote"] else ""
            notify(
                preparer, "expense_decided", f"Expense {verdict}",
                f"{stored['description']} ({money}) was {verdict}.{note}",
                {"entryId": ctx.entity_id, "status": stored["status"]},
            )
