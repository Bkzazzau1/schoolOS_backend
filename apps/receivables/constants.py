"""Words the school-fee receivables domain shares with the rest of SchoolOS.

This is what a SCHOOL charges its families: fee schedules, what each student owes, what each family
has paid and what it is owed back. It is not `apps.billing`, which is what SchoolOS charges schools
(plans, subscriptions, usage). Nothing here imports or reuses those models.
"""

#: The duty that lets a person determine what families owe: publish fees, set due dates, and decide
#: discounts, scholarships, waivers and other adjustments. The proprietor always has it; anyone else
#: has it only because the proprietor gave it to them through a job assignment. It is never implied
#: by a job title such as Accountant, Bursar or Principal.
BILLING_AUTHORITY_DUTY = "finance.billing_authority"

#: Duties that let someone WORK the ledger (look at it, correct an allocation) without deciding
#: what families owe. The finance office does this every day.
OPERATE_DUTIES = ("finance.billing_authority", "finance.reconciliation", "finance.accounts", "finance.collections")

DEFAULT_CURRENCY = "NGN"

#: Money is whole minor units (kobo) everywhere, as in `apps.bankconnect`. One naira fee is at most
#: this many kobo, which keeps every sum comfortably inside a 64-bit integer.
MAX_AMOUNT_MINOR = 100_000_000_000
