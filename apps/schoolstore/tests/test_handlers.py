"""The School Store: the owner or Finance Office stocks it and sells from it; a sale always pays at
the point of sale (never "on account"), and selling or voiding always keeps stock and sales in sync."""

from apps.schoolstore.constants import STORE_ITEM, STORE_SALE
from apps.staff.tests.helpers import StaffTestCase
from apps.sync.models import SyncRecord


class StoreTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.finance = self.members["accountant"]

    def item(self, id="ITEM-1", who=None, **over):
        body = {"id": id, "name": "School Uniform (Small)", "price": 8000, "stock": 10}
        body.update(over)
        return self.push(STORE_ITEM, id, body, who=who or self.finance)

    def stocked_item(self, id="ITEM-1", stock=10, price=8000):
        self.ok(self.item(id=id, stock=stock, price=price))
        return id

    _unset = object()

    def sale(self, id="SALE-1", who=None, items=_unset, **over):
        body = {"id": id, "studentId": "S1", "studentName": "Musa Ibrahim", "className": "Primary 4",
                "items": [{"itemId": "ITEM-1", "quantity": 2}] if items is self._unset else items,
                "method": "Cash", "reference": "", "note": ""}
        body.update(over)
        return self.push(STORE_SALE, id, body, who=who or self.finance)


class StoreItemTests(StoreTestCase):
    def test_the_owner_or_finance_office_may_stock_an_item(self):
        self.ok(self.item())
        self.assertEqual(self.stored(STORE_ITEM, "ITEM-1").payload["stock"], 10)

    def test_nobody_else_may_manage_the_store(self):
        for role in ("principal", "teacher", "staff", "parent", "student"):
            self.rejected(self.item(who=self.members[role]), "Finance Office")

    def test_price_and_stock_must_make_sense(self):
        self.rejected(self.item(price=0))
        self.rejected(self.item(stock=-1))

    def test_an_item_cannot_be_deleted(self):
        self.ok(self.item())
        self.rejected(self.push(STORE_ITEM, "ITEM-1", operation="delete", who=self.finance), "cannot be deleted")

    def test_restocking_is_a_normal_update(self):
        self.ok(self.item(stock=10))
        self.ok(self.push(STORE_ITEM, "ITEM-1", {"id": "ITEM-1", "name": "School Uniform (Small)", "price": 8000,
                                                   "stock": 25}, operation="update", who=self.finance))
        self.assertEqual(self.stored(STORE_ITEM, "ITEM-1").payload["stock"], 25)


class StoreSaleTests(StoreTestCase):
    def test_a_sale_takes_the_price_from_the_real_item_not_the_app(self):
        self.stocked_item(price=8000, stock=10)
        self.ok(self.sale(items=[{"itemId": "ITEM-1", "quantity": 2, "unitPrice": 1}]))
        stored = self.stored(STORE_SALE, "SALE-1").payload
        self.assertEqual(stored["amount"], 16000)
        self.assertEqual(stored["items"][0]["unitPrice"], 8000)

    def test_a_sale_reduces_stock_and_voiding_restores_it(self):
        self.stocked_item(stock=10)
        self.ok(self.sale())
        self.assertEqual(self.stored(STORE_ITEM, "ITEM-1").payload["stock"], 8)
        stored_sale = self.stored(STORE_SALE, "SALE-1").payload
        self.push(STORE_SALE, "SALE-1", {**stored_sale, "voidedReason": "typed twice"}, operation="update", who=self.finance)
        self.assertEqual(self.stored(STORE_ITEM, "ITEM-1").payload["stock"], 10)

    def test_a_sale_is_refused_when_stock_is_not_enough(self):
        self.stocked_item(stock=1)
        self.rejected(self.sale(items=[{"itemId": "ITEM-1", "quantity": 2}]), "left in stock")
        self.assertEqual(self.stored(STORE_ITEM, "ITEM-1").payload["stock"], 1)

    def test_a_sale_for_an_item_no_longer_in_the_store_is_refused(self):
        self.rejected(self.sale(), "no longer in the store")

    def test_nobody_else_may_record_a_sale(self):
        self.stocked_item()
        self.rejected(self.sale(who=self.members["parent"]), "Finance Office")

    def test_a_non_cash_sale_needs_a_reference(self):
        self.stocked_item()
        self.rejected(self.sale(method="POS", reference=""), "reference")
        self.ok(self.sale(method="POS", reference="POS-REF-1"))

    def test_the_same_reference_cannot_be_recorded_twice(self):
        self.stocked_item()
        self.ok(self.sale(id="SALE-1", method="Bank transfer", reference="REF-1"))
        self.rejected(self.sale(id="SALE-2", method="Bank transfer", reference="REF-1"), "already been recorded")

    def test_voiding_needs_a_reason_and_cannot_change_anything_else(self):
        self.stocked_item()
        self.ok(self.sale())
        stored = self.stored(STORE_SALE, "SALE-1").payload
        self.rejected(self.push(STORE_SALE, "SALE-1", {**stored, "voidedReason": ""}, operation="update", who=self.finance), "required")
        self.rejected(
            self.push(STORE_SALE, "SALE-1", {**stored, "amount": 1, "voidedReason": "mistake"}, operation="update", who=self.finance),
            "only voided",
        )
        self.ok(self.push(STORE_SALE, "SALE-1", {**stored, "voidedReason": "mistake"}, operation="update", who=self.finance))

    def test_a_sale_cannot_be_voided_twice(self):
        self.stocked_item()
        self.ok(self.sale())
        stored = self.stored(STORE_SALE, "SALE-1").payload
        self.push(STORE_SALE, "SALE-1", {**stored, "voidedReason": "first"}, operation="update", who=self.finance)
        voided = self.stored(STORE_SALE, "SALE-1").payload
        self.rejected(self.push(STORE_SALE, "SALE-1", {**voided, "voidedReason": "second"}, operation="update", who=self.finance), "already voided")

    def test_a_sale_cannot_be_deleted(self):
        self.stocked_item()
        self.ok(self.sale())
        self.rejected(self.push(STORE_SALE, "SALE-1", operation="delete", who=self.finance), "cannot be deleted")

    def test_too_many_lines_or_an_empty_sale_is_refused(self):
        self.stocked_item()
        self.rejected(self.sale(items=[]), "at least one")
        self.rejected(self.sale(items=[{"itemId": "ITEM-1", "quantity": 1}] * 21), "Too many")

    def test_receipt_numbers_follow_the_real_server_count(self):
        self.stocked_item(stock=10)
        self.ok(self.sale(id="SALE-1"))
        self.ok(self.sale(id="SALE-2"))
        self.assertEqual(self.stored(STORE_SALE, "SALE-2").payload["receiptNumber"], "STR-000002")
