from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, integer, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .access import payroll_authorities
from .constants import MAX_HISTORY_ENTRIES, MAX_MONTHLY_SALARY


def _figures(entry: dict) -> tuple:
    """What a history entry says, ignoring who and when (the server stamps those)."""
    return (entry.get("gross"), entry.get("deductions"), entry.get("onPayroll"))


class SalaryProfileHandler(EntityHandler):
    """A staff member's salary and whether they are on payroll.

    Only the owner sets salaries. History is append-only: an existing entry can
    never be changed or removed, and new entries are stamped with the server's
    time and the acting membership. Records cannot be deleted.
    """

    entity_type = "owner_payroll_profile"
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        """Everyone who may work on payroll needs the salaries to do it."""
        return payload if "view" in payroll_authorities(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        if text(p, "staffId", max_len=128) != ctx.entity_id:
            raise Rejected("staffId must match the record.")
        gross = integer(p, "gross", maximum=MAX_MONTHLY_SALARY)
        deductions = integer(p, "deductions", maximum=gross)
        on_payroll = boolean(p, "onPayroll")
        if on_payroll and gross == 0:
            raise Rejected("Set a gross salary before adding to payroll.")
        return {
            "staffId": ctx.entity_id,
            "name": text(p, "name"),
            "role": text(p, "role", max_len=100, required=False),
            "gross": gross,
            "deductions": deductions,
            "onPayroll": on_payroll,
            "history": self._history(ctx, gross, deductions, on_payroll),
            "updatedAt": ctx.now,
        }

    def _history(self, ctx, gross: int, deductions: int, on_payroll: bool) -> list[dict]:
        sent = ctx.payload.get("history")
        if not isinstance(sent, list) or not sent or len(sent) > MAX_HISTORY_ENTRIES:
            raise Rejected("history must list every salary change.")
        if not all(isinstance(entry, dict) for entry in sent):
            raise Rejected("history entries must be objects.")

        stored = list((ctx.existing or {}).get("history", []))
        if [_figures(e) for e in sent[: len(stored)]] != [_figures(e) for e in stored]:
            raise Rejected(
                "Salary history cannot be changed. It is out of date or has been altered; refresh and try again."
            )
        if _figures(sent[-1]) != (gross, deductions, on_payroll):
            raise Rejected("The newest history entry must match the current salary.")

        added = []
        for entry in sent[len(stored):]:
            added.append(
                {
                    "at": ctx.now,
                    "gross": integer(entry, "gross", maximum=MAX_MONTHLY_SALARY),
                    "deductions": integer(entry, "deductions", maximum=MAX_MONTHLY_SALARY),
                    "onPayroll": boolean(entry, "onPayroll"),
                    "byMembershipId": str(ctx.membership.id),
                }
            )
        return stored + added
