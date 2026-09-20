from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import text
from apps.sync.registry import EntityHandler, MutationContext

from .common import COMMENT, MAX_TEXT, MODERATORS, WRITERS, cached_post, display_name, may_see_post


class CommentHandler(EntityHandler):
    """A comment on a post. Whoever may see a post may comment on it.

    A person edits only their own comment; the author or a moderator can remove it.
    A comment stays on its post: `postId` cannot change. The commenter's name and
    account are set by the server.
    """

    entity_type = COMMENT
    roles = WRITERS
    allow_delete = True

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        old = ctx.existing or {}
        mine = old.get("authorMembershipId") == str(ctx.membership.id)
        if ctx.operation == "update" and not mine:
            raise Rejected("You can only edit your own comments.")
        if ctx.operation == "delete" and not mine and ctx.membership.role not in MODERATORS:
            raise Rejected("You can only remove your own comments.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, membership = ctx.payload, ctx.existing, ctx.membership
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        post_id = text(p, "postId", max_len=128)
        if old and old["postId"] != post_id:
            raise Rejected("A comment cannot move to another post.")
        post = cached_post(membership, post_id)
        if post is None or not may_see_post(membership, post):
            raise Rejected("That post does not exist.")
        return {
            "id": ctx.entity_id,
            "postId": post_id,
            "text": text(p, "text", max_len=MAX_TEXT),
            "author": (old or {}).get("author") or display_name(membership),
            "authorMembershipId": (old or {}).get("authorMembershipId") or str(membership.id),
            "createdAt": (old or {}).get("createdAt") or ctx.now,
            "updatedAt": ctx.now,
        }

    def visible(self, membership, payload):
        post = cached_post(membership, payload.get("postId", ""))
        if post is None:
            return payload if membership.role in MODERATORS else None
        return payload if may_see_post(membership, post) else None
