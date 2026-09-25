from typing import Any

from apps.core.errors import Rejected
from apps.core.identity import normalize_name, normalize_nin, normalize_phone
from apps.core.validation import choice, integer, is_email, text
from apps.notifications.services import notify_many
from apps.schools.models import Membership
from apps.sync import records
from apps.sync.registry import EntityHandler, MutationContext

from .. import identity
from ..authority import can_approve_staff
from ..constants import DIRECTORY, PENDING, PROPOSAL, PROPOSER_ROLES, SYSTEM_ROLES

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

    def visible(self, membership, payload):
        """The owner and anyone assigned to approve see every proposal; a proposer sees their own."""
        if payload.get("proposedByMembershipId") == str(membership.id) or can_approve_staff(membership):
            return payload
        return None

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
        name = text(p, "name")

        # An owner appointing someone already on staff to a second role confirms
        # that explicitly (see StaffProposalRepository.propose); this is verified
        # independently here rather than trusted from the client, so it can only
        # ever apply to the one real person who already holds this exact phone
        # and NIN - every other clash is refused exactly as before.
        appointment_of = text(p, "appointmentOfStaffId", max_len=128, required=False)
        if appointment_of:
            confirmed = identity.confirmed_holder(ctx.membership.school, phone=phone, nin=nin)
            if confirmed != appointment_of:
                raise Rejected(
                    "This phone number and NIN no longer match an existing staff member's record exactly. "
                    "Check them and try again."
                )
            existing = records.read(ctx.membership.school, DIRECTORY, appointment_of) or {}
            if normalize_name(existing.get("name", "")) != normalize_name(name):
                raise Rejected("The name does not match the existing staff record for this phone number and NIN.")
        else:
            identity.check_available(ctx.membership.school, "proposal", ctx.entity_id, phone=phone, nin=nin)

        return {
            "name": name,
            "roleTitle": text(p, "roleTitle", max_len=100),
            "systemRole": choice(p.get("systemRole"), SYSTEM_ROLES, "systemRole"),
            "workArea": text(p, "workArea", max_len=100),
            "email": email,
            "phone": phone,
            "nin": nin,
            "gross": gross,
            "deductions": integer(p, "deductions", maximum=gross),
            "appointmentOfStaffId": appointment_of,
            # Set by the server:
            "status": PENDING,
            "proposedByMembershipId": str(ctx.membership.id),
            "proposedByRole": ctx.membership.role,
            "proposedAt": ctx.now,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        if not stored.get("appointmentOfStaffId"):
            # A confirmed second appointment shares an already-verified identity;
            # its numbers stay claimed by the original staff record, never
            # duplicated onto this proposal.
            identity.set_claims(school, "proposal", ctx.entity_id, stored["name"], phone=stored["phone"], nin=stored["nin"])
        owners = Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school")
        notify_many(
            owners, "staff_proposal", "New staff proposal",
            f"{stored['name']} was proposed as {stored['roleTitle']} ({SYSTEM_ROLES[stored['systemRole']]}). "
            "Review it in Staff Profiles.",
            {"proposalId": ctx.entity_id},
        )
