from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, is_email, string_list, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from ..common import REVOKED, resolve_status, server_owned_link
from .constants import DUTIES, JOB_ROLES, RECIPIENT_TYPES


class JobAssignmentHandler(EntityHandler):
    """A job the owner gives to someone, registered or not.

    Like payroll authority, whether the assignment is active and which account
    it is linked to are set only by the server. Records cannot be deleted, only
    revoked.
    """

    entity_type = "owner_job_assignment"
    roles = frozenset({Role.PROPRIETOR})

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        existing = ctx.existing or {}
        recipient = choice(p.get("recipientType"), RECIPIENT_TYPES, "recipientType")
        registered_id = text(p, "registeredStaffId", max_len=128, required=recipient == "registered")
        if recipient == "unregistered" and registered_id:
            raise Rejected("An unregistered person has no staff record.")

        # A registered person is reached through their staff record, so an
        # email is optional for them. Everyone else needs one.
        email = text(p, "email", max_len=254, required=recipient == "unregistered").lower()
        if email and not is_email(email):
            raise Rejected("Enter a valid email address.")

        role = choice(p.get("role"), JOB_ROLES, "role")
        section_id = text(p, "sectionId", max_len=128, required=role == "sectionHead")
        status = resolve_status(p, ctx.existing)
        cleaned = {
            "name": text(p, "name"),
            "email": email,
            "title": text(p, "title", max_len=120),
            "recipientType": recipient,
            "registeredStaffId": registered_id or None,
            "role": role,
            "sectionId": section_id or None,
            "sectionName": text(p, "sectionName", required=False) or None,
            "scope": "section" if section_id else "school",
            "duties": string_list(p, "duties", allowed=DUTIES),
            "status": status,
            "membershipId": server_owned_link(ctx.existing),
            "assignedByMembershipId": str(ctx.membership.id),
            "createdAt": existing.get("createdAt") or ctx.now,
            "updatedAt": ctx.now,
        }
        if status == REVOKED:
            cleaned["revokedByMembershipId"] = str(ctx.membership.id)
        return cleaned
