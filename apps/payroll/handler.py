from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice
from apps.notifications.services import notify_many
from apps.owner.payroll.access import members_with, payroll_authorities
from apps.schools.models import Membership
from apps.sync.registry import EntityHandler, MutationContext

from .constants import APPROVED, BATCH, INSTRUCTED, PERIOD, PREPARED, REJECTED, STATUSES
from .transitions import STEPS


class PayrollBatchHandler(EntityHandler):
    """A month's payroll: prepared, then approved by someone else, then instructed.

    Whoever is holding the phone, the rules are the server's: the lines come from
    the salary records, the approver cannot be the preparer (the owner included),
    each step needs its own authority, an approved batch can no longer change, and
    who did what and when is stamped by the server and kept in the batch's trail.
    Batches cannot be deleted.
    """

    entity_type = BATCH

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A payroll batch cannot be deleted.")
        if not payroll_authorities(ctx.membership):
            raise Rejected("You are not authorized to work on payroll.")

    def visible(self, membership, payload):
        return payload if "view" in payroll_authorities(membership) else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not PERIOD.match(ctx.entity_id):
            raise Rejected("A batch is identified by its month, for example 2026-09.")
        if ctx.payload.get("period") != ctx.entity_id:
            raise Rejected("period must match the record.")
        wanted = choice(ctx.payload.get("status"), STATUSES, "status")
        old = ctx.existing
        step = STEPS.get((old["status"] if old else None, wanted))
        if step is None:
            if old is None:
                raise Rejected("A new batch starts as prepared.")
            raise Rejected(f"A batch that is {old['status']} cannot become {wanted}.")
        return step(ctx, old)

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        status, period = stored["status"], stored["period"]
        preparer = Membership.objects.filter(id=stored["preparedByMembershipId"]).select_related("school").first()
        who = {
            PREPARED: ("approve", "Payroll batch to approve", f"The {period} payroll is ready for approval."),
            APPROVED: ("pay", "Payroll batch approved", f"The {period} payroll was approved and can be paid."),
            REJECTED: (None, "Payroll batch rejected", f"The {period} payroll was rejected: {stored.get('rejectionReason', '')}"),
            INSTRUCTED: (None, "Payroll payment instructed", f"Payment of the {period} payroll was instructed."),
        }[status]
        authority, title, message = who
        recipients = {m.id: m for m in members_with(school, authority)} if authority else {}
        owners = Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school")
        recipients.update({m.id: m for m in owners})
        if status != PREPARED and preparer is not None:
            recipients[preparer.id] = preparer
        recipients.pop(ctx.membership.id, None)  # no need to tell someone what they just did
        notify_many(recipients.values(), "payroll_batch", title, message, {"period": period, "status": status})
