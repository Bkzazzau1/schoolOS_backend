from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .constants import APPEARANCE, APPEARANCE_ID, THEMES


class AppearanceHandler(EntityHandler):
    """The school's colour scheme. The owner chooses it and everyone in the
    school receives it, so the whole school looks the same. Who chose it and when
    are stamped by the server."""

    entity_type = APPEARANCE
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        return payload

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.entity_id != APPEARANCE_ID:
            raise Rejected("A school has one appearance record.")
        return {
            "themeId": choice(ctx.payload.get("themeId"), THEMES, "themeId"),
            "updatedByMembershipId": str(ctx.membership.id),
            "updatedAt": ctx.now,
        }
