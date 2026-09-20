#: What the owner can authorize someone to do. Matches the app's
#: payrollAuthorityLabels.
AUTHORITIES = frozenset({"prepare", "approve", "pay", "approveStaff", "view"})

#: A salary above this is refused as a typing mistake (naira per month).
MAX_MONTHLY_SALARY = 1_000_000_000

MAX_HISTORY_ENTRIES = 1000
