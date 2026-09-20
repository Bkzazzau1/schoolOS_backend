from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.sync.registry import EntityHandler, MutationContext

from ..constants import DIRECTORY, FILE_STATUSES
from ..models import IdentityClaim

#: Set by the server (when a proposal is approved, or when the owner adds someone
#: directly), never taken from the app.
SERVER_OWNED = ("staffCategory", "systemRole", "approvedFromProposal", "createdByMembershipId", "createdAt")


class StaffDirectoryHandler(EntityHandler):
    """The school's list of staff: who they are, their job title, their section.

    Only the owner adds people directly (everyone else proposes, and approval adds
    them). The owner edits any field. The administrator only reviews the file, so
    they may change nothing but the file status. Records cannot be deleted.
    """

    entity_type = DIRECTORY
    roles = frozenset({"proprietor", "administrator"})

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if ctx.operation == "create" and ctx.membership.role != "proprietor":
            raise Rejected("Only the owner adds staff. Everyone else proposes them for the owner to approve.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, existing = ctx.payload, ctx.existing
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        cleaned = {
            "id": ctx.entity_id,
            "name": text(p, "name"),
            "role": text(p, "role", max_len=100),
            "section": text(p, "section", max_len=100),
            "fileStatus": choice(p.get("fileStatus"), FILE_STATUSES, "fileStatus"),
        }
        if existing is not None:
            if ctx.membership.role == "administrator":
                for key in ("name", "role", "section"):
                    if cleaned[key] != existing.get(key):
                        raise Rejected("The administrator can review a staff file but not change who they are.")
            for key in SERVER_OWNED:
                if key in existing:
                    cleaned[key] = existing[key]
        else:
            cleaned.update(
                staffCategory="added_by_owner",
                createdByMembershipId=str(ctx.membership.id),
                createdAt=ctx.now,
            )
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        # Keep "already used by <name>" messages current if someone is renamed.
        IdentityClaim.objects.filter(
            school=ctx.membership.school, holder_type="staff", holder_id=ctx.entity_id
        ).update(holder_name=stored["name"])
