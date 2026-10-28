from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .constants import CAMPUS


class CampusHandler(EntityHandler):
    """One of the school's real physical sites.

    Only the owner changes the structure; everyone in the school reads it. A
    campus cannot be deleted, because sections point at it (see SectionHandler).
    """

    entity_type = CAMPUS
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        return payload

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return {
            "id": ctx.entity_id,
            "name": text(p, "name", max_len=120),
            "updatedByMembershipId": str(ctx.membership.id),
            "updatedAt": ctx.now,
        }
