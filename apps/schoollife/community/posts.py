from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schoollife.framework import LEADERS, STAFF_SIDE
from apps.sync.registry import EntityHandler, MutationContext

from .common import AUDIENCES, MAX_TEXT, MODERATORS, POST, VISIBILITIES, WRITERS, display_name, may_see_post, role_label


class PostHandler(EntityHandler):
    """A post in the school community.

    Any adult member may post to the community, and change or remove their own post.
    Moderators (owner, principal, administrator) may change or remove any post.
    Staff-only posts can be made only by the staff side, and only the owner and principal
    may put a post on the public showcase. The author's name, role, time and account are
    set by the server. Comments and reactions are their own records, so any `comments` or
    `reactions` sent inside a post are dropped.
    """

    entity_type = POST
    roles = WRITERS
    allow_delete = True

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if ctx.operation in ("update", "delete"):
            mine = (ctx.existing or {}).get("authorMembershipId") == str(ctx.membership.id)
            if not mine and ctx.membership.role not in MODERATORS:
                raise Rejected("You can only change or remove your own posts.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, membership = ctx.payload, ctx.existing, ctx.membership
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        audience = choice(p.get("audience"), AUDIENCES, "audience")
        visibility = choice(p.get("visibility"), VISIBILITIES, "visibility")
        if audience == "staffOnly" and membership.role not in STAFF_SIDE:
            raise Rejected("Only staff can post to staff only.")
        if visibility == "publicShowcase" and (old or {}).get("visibility") != visibility and membership.role not in LEADERS:
            raise Rejected("Only the owner or principal can put a post on the public showcase.")
        return {
            "id": ctx.entity_id,
            "title": text(p, "title", max_len=200),
            "body": text(p, "body", max_len=MAX_TEXT * 5),
            "audience": audience,
            "visibility": visibility,
            "mediaLabel": text(p, "mediaLabel", max_len=200, required=False) or None,
            "author": (old or {}).get("author") or display_name(membership),
            "role": (old or {}).get("role") or role_label(membership),
            "authorMembershipId": (old or {}).get("authorMembershipId") or str(membership.id),
            "createdAt": (old or {}).get("createdAt") or ctx.now,
            "updatedAt": ctx.now,
        }

    def visible(self, membership, payload):
        return payload if may_see_post(membership, payload) else None
