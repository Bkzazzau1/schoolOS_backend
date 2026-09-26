"""Which charge a payment (or a credit) goes towards first.

The default is what most schools would expect:

1. the oldest OVERDUE charge, then
2. the oldest charge that is due now (within the next 30 days), then
3. the next unpaid charge,

with ties broken the same way every time (due date, then when it was raised, then id) so the same
payment always lands the same way. A payment aimed at one student can put that student's charges first
(`prefer_student`); nothing else changes.

The policy is one small function, replaceable without touching the allocation code: point
`RECEIVABLES_ALLOCATION_POLICY` in the settings at a function with the same signature.
"""

from datetime import date, timedelta

from django.conf import settings
from django.utils.module_loading import import_string

#: A charge falling due within this many days counts as "due now".
DUE_NOW_DAYS = 30


def default_policy(receivables, *, today: date, prefer_student=None) -> list:
    """The charges in the order money should go to them. Only charges with something outstanding should be passed in."""

    preferred_id = getattr(prefer_student, "id", prefer_student)  # a Student or just its id

    def key(r):
        bucket = 0 if r.due_date < today else 1 if r.due_date <= today + timedelta(days=DUE_NOW_DAYS) else 2
        preferred = 0 if preferred_id is not None and r.student_id == preferred_id else 1
        return (preferred, bucket, r.due_date, r.created_at, str(r.id))

    return sorted(receivables, key=key)


def get_policy():
    path = getattr(settings, "RECEIVABLES_ALLOCATION_POLICY", "")
    return import_string(path) if path else default_policy
