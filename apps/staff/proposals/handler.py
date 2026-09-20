from typing import Any

from apps.core.errors import Rejected
from apps.core.identity import normalize_nin, normalize_phone
from apps.core.validation import choice, integer, is_email, text
from apps.notifications.services import notify_many
from apps.schools.models import Membership
from apps.sync.registry import EntityHandler, MutationContext

from .. import identity
from ..constants import PENDING, PROPOSAL, PROPOSER_ROLES, SYSTEM_ROLES

MAX_SALARY = 1_000_000_000


class StaffProposalHandler(EntityHandler):
    """Someone proposes a new staff member, with their salary, for the owner.

    A proposal is not a staff member: nobody is added to the staff list, payroll
    or anywhere else until it is approved, and approving happens on the server
    (`staff/schools/<school>/proposals/<id>/approve/`), never through sync. So
    the app can only *create* a proposal here. Its status, who proposed it and
    when are set by the server, and it cannot be edited or deleted afterwards.

    Its phone number and NIN are held for it, so the same person cannot be proposed
    twice, or proposed when they are already staff.
    """

    entity_type = PROPOSAL
    roles = PROPOSER_ROLES

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if ctx.operation == "update":
            raise Rejected(
                "A proposal cannot be changed once it is sent. Ask the owner to reject it, then propose again."
            )

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        phone = normalize_phone(text(p, "phone", max_len=30))
        if phone is None:
            raise Rejected("Enter a valid Nigerian phone number, for example 0803 123 4567.")
        nin = normalize_nin(text(p, "nin", max_len=30))
        if nin is None:
            raise Rejected("A NIN is exactly 11 digits.")
        email = text(p, "email", max_len=254).lower()
        if not is_email(email):
            raise Rejected("Enter the staff member's email address. Their registration link is sent there once approved.")
        gross = integer(p, "gross", minimum=1, maximum=MAX_SALARY)
        identity.check_available(ctx.membership.school, "proposal", ctx.entity_id, phone=phone, nin=nin)
        return {
            "name": text(p, "name"),
            "roleTitle": text(p, "roleTitle", max_len=100),
            "systemRole": choice(p.get("systemRole"), SYSTEM_ROLES, "systemRole"),
            "workArea": text(p, "workArea", max_len=100),
            "email": email,
            "phone": phone,
            "nin": nin,
            "gross": gross,
            "deductions": integer(p, "deductions", maximum=gross),
            # Set by the server:
            "status": PENDING,
            "proposedByMembershipId": str(ctx.membership.id),
            "proposedByRole": ctx.membership.role,
            "proposedAt": ctx.now,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        identity.set_claims(school, "proposal", ctx.entity_id, stored["name"], phone=stored["phone"], nin=stored["nin"])
        owners = Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school")
        notify_many(
            owners, "staff_proposal", "New staff proposal",
            f"{stored['name']} was proposed as {stored['roleTitle']} ({SYSTEM_ROLES[stored['systemRole']]}). "
            "Review it in Staff Profiles.",
            {"proposalId": ctx.entity_id},
        )
