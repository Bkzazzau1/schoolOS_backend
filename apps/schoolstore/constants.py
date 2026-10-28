"""Values the School Store shares with the app. Each mirrors the app's.

A school's own shop - uniforms, books, stationery - sold to students at the point of sale. Unlike the
old, deleted design this replaces, a sale is never "on account": a family pays cash, transfer or POS
when the item changes hands, exactly like a fee payment (`apps.financeledger`) - there is no real
"buy now, collect later" workflow anywhere in this product, so none is invented here.
"""

import re

STORE_ITEM = "finance_store_item"
STORE_SALE = "finance_store_sale"

MAX_AMOUNT = 100_000_000
MAX_NOTE = 300
MAX_SALE_ITEMS = 20
MAX_QUANTITY = 1000
MAX_STOCK = 1_000_000

#: Ids like "SALE-1234567890" / "ITEM-1234567890". Any tidy id is accepted, so two devices that pick
#: the same one are told there is a conflict instead of overwriting each other.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$")
