"""Splitting one fee into instalments, in whole kobo, with nothing lost or invented.

A plan is a list of parts, each a share (in basis points, 10000 = the whole) and a due date. The shares
must add up to exactly the whole, and the money is split so the parts add up to exactly the amount:
each part takes its share rounded down, and the last takes whatever is left. The same input always
gives the same split.
"""

from datetime import date

from .errors import Refused

MAX_PARTS = 12
WHOLE = 10_000


def clean_plan(plan) -> list[dict]:
    """The plan as stored: `[{"basisPoints": 5000, "dueDate": "2026-09-30"}, ...]`, or `[]` for one payment."""
    if plan in (None, [], ()):
        return []
    if not isinstance(plan, (list, tuple)) or not 2 <= len(plan) <= MAX_PARTS:
        raise Refused(f"An instalment plan has between 2 and {MAX_PARTS} parts.", "invalid_plan")
    cleaned, total = [], 0
    for part in plan:
        if not isinstance(part, dict):
            raise Refused("Each instalment needs a share and a due date.", "invalid_plan")
        share = part.get("basisPoints")
        if isinstance(share, bool) or not isinstance(share, int) or not 1 <= share <= WHOLE:
            raise Refused("Each instalment's share must be a whole number of basis points (10000 is the whole fee).", "invalid_plan")
        try:
            due = date.fromisoformat(str(part.get("dueDate")))
        except ValueError:
            raise Refused("Each instalment needs a valid due date.", "invalid_plan")
        cleaned.append({"basisPoints": share, "dueDate": due.isoformat()})
        total += share
    if total != WHOLE:
        raise Refused(f"The instalments must add up to the whole fee; they add up to {total / 100:g}%.", "invalid_plan")
    return sorted(cleaned, key=lambda p: p["dueDate"])


def split(amount_minor: int, shares: list[int]) -> list[int]:
    """`amount_minor` divided by `shares` (which must add up to WHOLE) so the parts add up exactly."""
    parts, given, running = [], 0, 0
    for index, share in enumerate(shares):
        running += share
        cumulative = amount_minor if index == len(shares) - 1 else amount_minor * running // WHOLE
        parts.append(cumulative - given)
        given = cumulative
    return parts


def parts_for(amount_minor: int, due_date, plan: list[dict]) -> list[tuple[int, date, int]]:
    """`(instalment number, due date, amount)` for one student's charge of this item."""
    if not plan:
        return [(1, due_date, amount_minor)]
    amounts = split(amount_minor, [p["basisPoints"] for p in plan])
    return [(n, date.fromisoformat(p["dueDate"]), amount) for n, (p, amount) in enumerate(zip(plan, amounts), start=1)]
