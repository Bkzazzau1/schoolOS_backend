"""Values the concessions feature shares with the app. Each list mirrors the app's."""

import re

CONCESSION = "concession_request"

TYPES = ("scholarship", "discount")

PENDING = "pendingApproval"
APPROVED = "approved"
DECLINED = "declined"
DECISIONS = (APPROVED, DECLINED)

#: The duty the owner can give someone (through a job assignment) to raise requests.
SUBMIT_DUTY = "finance.concessions"

#: Ids like "CNC-2026-041". Any tidy id is accepted, so two devices that pick the
#: same one are told there is a conflict instead of overwriting each other.
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,63}$")

MAX_FEE = 100_000_000
MAX_NOTE = 300

ROLE_LABELS = {"proprietor": "Proprietor", "accountant": "Finance Office"}
