"""Values the Finance Office's cash flow ledger shares with the app. Each mirrors the app's.

The school's own income and spending, outside the fee ledger entirely: a donation, a facility rental,
paying for repairs or supplies. Not `apps.receivables` (what families owe) and not `apps.billing`
(what the school pays SkulGen).
"""

import re

CASHFLOW_ENTRY = "finance_cashflow_entry"

INCOME = "income"
EXPENSE = "expense"
TYPES = (INCOME, EXPENSE)

POSTED = "posted"
PENDING = "pendingApproval"
APPROVED = "approved"
DECLINED = "declined"
DECISIONS = (APPROVED, DECLINED)

CATEGORIES = (
    "Utilities", "Maintenance & Repairs", "Supplies & Materials", "Transport",
    "Staff Welfare", "Professional Fees", "Donation", "Grant", "Facility Rental", "Other",
)

MAX_AMOUNT = 100_000_000
MAX_NOTE = 300

#: Ids like "EXP-1234567890". Any tidy id is accepted, so two devices that pick the same one are told
#: there is a conflict instead of overwriting each other.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$")

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
