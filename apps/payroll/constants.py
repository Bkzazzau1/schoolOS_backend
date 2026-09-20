"""Values the payroll feature shares with the app. Each list mirrors the app's."""

import re

BATCH = "payroll_batch"
SALARY = "owner_payroll_profile"

PREPARED = "prepared"
APPROVED = "approved"
REJECTED = "rejected"
INSTRUCTED = "disbursementInstructed"
STATUSES = (PREPARED, APPROVED, REJECTED, INSTRUCTED)

#: A batch is identified by its month, "2026-09".
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

MAX_LINES = 5000
MAX_NOTE = 300
