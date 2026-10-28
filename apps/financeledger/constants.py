"""Values the Finance Office's own fee ledger shares with the app. Each mirrors the app's.

This is the Finance Office's manual, day-to-day record of what each section is charged and what has
been received for it - fee structures, payments, due-date reminders, bank statement lines. It is a
separate, simpler system from `apps.receivables` (which backs Smart Money Collection's automated,
provider-reconciled family accounts): a school always has this manual ledger, with or without a
connected collection provider. See `apps.receivables.permissions` for the authority split (billing
authority decides what is owed; the Finance Office operates the ledger day to day) this app reuses
rather than redefining.
"""

import re

FEE_STRUCTURE = "finance_fee_structure"
PAYMENT = "finance_payment"
TERM_DUE = "finance_term_due"
REMINDER = "finance_reminder"
BANK_LINE = "finance_bank_line"

SECTIONS = ("Early Years", "Primary", "Secondary")
PAYMENT_METHODS = ("Cash", "Bank transfer", "POS")

MAX_FEE_ITEMS = 20
MAX_AMOUNT = 100_000_000
MAX_NOTE = 300

#: Ids like "PAY-1234567890" / "REM-S1-1234567890" / "BNK-1234567890". Any tidy id is accepted, so two
#: devices that pick the same one are told there is a conflict instead of overwriting each other.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$")

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
