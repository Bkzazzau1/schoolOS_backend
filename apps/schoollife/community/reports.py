from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.notifications.services import notify_many
from apps.schools.models import Membership
from apps.sync.registry import EntityHandler, MutationContext

from .common import MODERATORS, REPORT, WRITERS, cached_post, may_see_post

AWAITING, REVIEWED, DISMISSED = "awaitingReview", "actioned", "dismissed"


class ReportHandler(EntityHandler):
    """A person reporting a post to the moderators.

    Anyone who may see a post may report it. The report starts as awaiting review; only
    a moderator can settle it. Only the moderators and the person who reported it see it.
    Moderators are told when a report arrives.
    """

    entity_type = REPORT
    roles = WRITERS

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if ctx.operation == "update" and ctx.membership.role not in MODERATORS:
            raise Rejected("Only a moderator can settle a report.")

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, old, membership = ctx.payload, ctx.existing, ctx.membership
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        if old:
            return {**old, "status": choice(p.get("status"), (REVIEWED, DISMISSED), "status"),
                    "settledByMembershipId": str(membership.id), "settledAt": ctx.now}
        post_id = text(p, "postId", max_len=128)
        post = cached_post(membership, post_id)
        if post is None or not may_see_post(membership, post):
            raise Rejected("That post does not exist.")
        return {"id": ctx.entity_id, "postId": post_id, "status": AWAITING,
                "reportedByMembershipId": str(membership.id), "createdAt": ctx.now,
                "reason": text(p, "reason", max_len=300, required=False)}

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        if ctx.operation != "create":
            return
        moderators = Membership.objects.filter(school=ctx.membership.school, role__in=MODERATORS, is_active=True).select_related("school")
        notify_many([m for m in moderators if m.id != ctx.membership.id], "community_report",
                    "A post was reported", "A community post needs review.", {"reportId": ctx.entity_id, "postId": stored["postId"]})

    def visible(self, membership, payload):
        if membership.role in MODERATORS or payload.get("reportedByMembershipId") == str(membership.id):
            return payload
        return None
