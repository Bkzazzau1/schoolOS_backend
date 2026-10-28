from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import integer, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .constants import CAMPUS, SECTION


class SectionHandler(EntityHandler):
    """An academic section of the school (nursery, primary, secondary, ...).

    Only the owner changes the structure; everyone in the school reads it. A
    section cannot be deleted, because appointments and jobs point at it.
    """

    entity_type = SECTION
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        return payload

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        campus_id = text(p, "campusId", max_len=128, required=False)
        if campus_id and not SyncRecord.objects.filter(
            school=ctx.membership.school, entity_type=CAMPUS, entity_id=campus_id, deleted=False
        ).exists():
            raise Rejected("Choose a real campus.")
        return {
            "id": ctx.entity_id,
            "name": text(p, "name", max_len=120),
            "stage": text(p, "stage", max_len=60),
            "campusId": campus_id or None,
            "leaderTitle": text(p, "leaderTitle", max_len=120, required=False),
            "leaderName": text(p, "leaderName", max_len=120, required=False),
            "classes": integer(p, "classes", maximum=500),
            "updatedByMembershipId": str(ctx.membership.id),
            "updatedAt": ctx.now,
        }
