from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.sync.registry import EntityHandler, MutationContext

from .common import MODERATORS, REACTION, WRITERS, cached_post, may_see_post

KINDS = ("like",)


class ReactionHandler(EntityHandler):
    """A person's reaction to a post.

    The record id is `<postId>:<membershipId>`, so a person can react to a post at most
    once, and only as themselves. Taking the reaction back deletes it. Anyone who may
    see a post may react to it.
    """

    entity_type = REACTION
    roles = WRITERS
    allow_delete = True

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if not ctx.entity_id.endswith(f":{ctx.membership.id}"):
            raise Rejected("A reaction is always your own: its id ends with your membership id.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, membership = ctx.payload, ctx.membership
        post_id = text(p, "postId", max_len=128)
        if ctx.entity_id != f"{post_id}:{membership.id}":
            raise Rejected("The id must be the post id, a colon, then your membership id.")
        post = cached_post(membership, post_id)
        if post is None or not may_see_post(membership, post):
            raise Rejected("That post does not exist.")
        return {
            "postId": post_id,
            "kind": choice(p.get("kind", "like"), KINDS, "kind"),
            "authorMembershipId": str(membership.id),
            "createdAt": (ctx.existing or {}).get("createdAt") or ctx.now,
        }

    def visible(self, membership, payload):
        post = cached_post(membership, payload.get("postId", ""))
        if post is None:
            return payload if membership.role in MODERATORS else None
        return payload if may_see_post(membership, post) else None
