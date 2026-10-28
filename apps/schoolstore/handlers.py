from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.financeledger.constants import PAYMENT_METHODS
from apps.receivables.permissions import can_operate_receivables
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .constants import ID, MAX_AMOUNT, MAX_NOTE, MAX_QUANTITY, MAX_SALE_ITEMS, MAX_STOCK, STORE_ITEM, STORE_SALE


class StoreItemHandler(EntityHandler):
    """A thing the store sells - a uniform, a book, stationery - and how many are left. Only the owner
    or the Finance Office manage it; everyone who may operate the ledger may see it, since recording a
    sale needs to know what things cost."""

    entity_type = STORE_ITEM

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A store item cannot be deleted - set its stock to 0 to retire it.")
        if not can_operate_receivables(ctx.membership):
            raise Rejected("Only the owner or the Finance Office can manage the store.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That item id is not valid.")
        p = ctx.payload
        if text(p, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        name = text(p, "name", max_len=80)
        price = integer(p, "price", minimum=1, maximum=MAX_AMOUNT)
        stock = integer(p, "stock", minimum=0, maximum=MAX_STOCK)
        return {"id": ctx.entity_id, "name": name, "price": price, "stock": stock}


class StoreSaleHandler(EntityHandler):
    """Items sold to a student, paid for at the point of sale. The price and name of each line come
    from the store's own real item record, never from the app, and selling takes the quantity off that
    item's stock in the same transaction a sale is never recorded without really reducing stock, and
    voiding a sale (a mistake, with a reason) always puts it back. Never edited once stored except to
    void it."""

    entity_type = STORE_SALE

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A sale cannot be deleted.")
        if not can_operate_receivables(ctx.membership):
            raise Rejected("Only the owner or the Finance Office can record a sale.")

    def visible(self, membership, payload):
        return payload if can_operate_receivables(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That sale id is not valid.")
        if text(ctx.payload, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return self._void(ctx) if ctx.existing else self._create(ctx)

    def _create(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        raw_items = p.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise Rejected("Add at least one item to the sale.")
        if len(raw_items) > MAX_SALE_ITEMS:
            raise Rejected("Too many items on one sale.")
        lines: list[dict[str, Any]] = []
        total = 0
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise Rejected("Every line must be an item.")
            item_id = text(raw, "itemId", max_len=64)
            quantity = integer(raw, "quantity", minimum=1, maximum=MAX_QUANTITY)
            item = SyncRecord.objects.filter(
                school=ctx.membership.school, entity_type=STORE_ITEM, entity_id=item_id, deleted=False,
            ).first()
            if item is None:
                raise Rejected("One of the items in this sale is no longer in the store.")
            unit_price = item.payload["price"]
            lines.append(
                {"itemId": item_id, "itemName": item.payload["name"], "quantity": quantity, "unitPrice": unit_price}
            )
            total += unit_price * quantity

        method = choice(p.get("method"), PAYMENT_METHODS, "method")
        reference = text(p, "reference", max_len=80, required=False)
        if method != "Cash" and not reference:
            raise Rejected("Enter the reference so the sale can be reconciled.")
        if reference and self._reference_already_recorded(ctx, reference):
            raise Rejected("That reference has already been recorded on another sale.")

        count = SyncRecord.objects.filter(school=ctx.membership.school, entity_type=STORE_SALE).count()
        return {
            "id": ctx.entity_id,
            "receiptNumber": f"STR-{count + 1:06d}",
            "studentId": text(p, "studentId", max_len=64),
            "studentName": text(p, "studentName", max_len=120),
            "className": text(p, "className", max_len=60),
            "items": lines,
            "amount": total,
            "method": method,
            "reference": reference,
            "soldAt": ctx.now,
            "recordedBy": str(ctx.membership.id),
            "note": text(p, "note", max_len=MAX_NOTE, required=False),
            "voidedReason": "",
        }

    def _void(self, ctx: MutationContext) -> dict[str, Any]:
        old, p = ctx.existing, ctx.payload
        if old["voidedReason"]:
            raise Rejected("This sale is already voided.")
        reason = text(p, "voidedReason", max_len=MAX_NOTE)
        for key in ("id", "receiptNumber", "studentId", "studentName", "className", "items", "amount",
                    "method", "reference", "soldAt", "recordedBy", "note"):
            if p.get(key) != old.get(key):
                raise Rejected("A sale cannot be changed, only voided.")
        return {**old, "voidedReason": reason}

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        if ctx.operation == "create":
            self._adjust_stock(ctx, stored["items"], sign=-1)
        elif ctx.operation == "update" and stored["voidedReason"] and not ctx.existing.get("voidedReason"):
            self._adjust_stock(ctx, stored["items"], sign=1)

    @staticmethod
    def _adjust_stock(ctx: MutationContext, lines: list[dict[str, Any]], *, sign: int) -> None:
        for line in lines:
            item = SyncRecord.objects.select_for_update().filter(
                school=ctx.membership.school, entity_type=STORE_ITEM, entity_id=line["itemId"], deleted=False,
            ).first()
            if item is None:
                if sign < 0:
                    raise Rejected(f"{line['itemName']} is no longer in the store.")
                continue  # Restocking an item since removed from the catalog: nothing to restock onto.
            new_stock = item.payload.get("stock", 0) + sign * line["quantity"]
            if new_stock < 0:
                raise Rejected(f"Only {item.payload.get('stock', 0)} of {line['itemName']} left in stock.")
            item.payload = {**item.payload, "stock": new_stock}
            item.version += 1
            item.save()

    @staticmethod
    def _reference_already_recorded(ctx: MutationContext, reference: str) -> bool:
        lowered = reference.lower()
        rows = SyncRecord.objects.filter(
            school=ctx.membership.school, entity_type=STORE_SALE, deleted=False,
        ).values_list("payload", flat=True)
        return any(
            not (row.get("voidedReason") or "") and (row.get("reference") or "").strip().lower() == lowered
            for row in rows
        )
