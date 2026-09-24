from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .services import (
    WEEKLY_LEARNING_ENTITY,
    parent_can_view_payload,
    replace_current_sync_payload,
    serialize_update,
    student_can_view_payload,
    upsert_update,
)


class WeeklyLearningHandler(EntityHandler):
    entity_type = WEEKLY_LEARNING_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
            return payload
        if (
            membership.role == Role.PRINCIPAL
            and str(payload.get("section") or "").strip().casefold() == "secondary"
        ):
            return payload
        if membership.role == Role.TEACHER and str(membership.id) in {
            str(payload.get("authorMembershipId") or ""),
            str(payload.get("currentTeacherId") or ""),
        }:
            return payload
        if parent_can_view_payload(membership, payload):
            return payload
        if student_can_view_payload(membership, payload):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("Weekly learning records are preserved; deletion is not permitted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=160)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the weekly-learning entity id.")
        return {
            "id": entity_id,
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "termId": text(p, "termId", max_len=64),
            "weekStart": text(p, "weekStart", max_len=10),
            "action": choice(
                p.get("action"),
                {"saveDraft", "publish"},
                "action",
            ),
            "nextFocus": text(p, "nextFocus", max_len=4000, required=False),
            "supportNote": text(p, "supportNote", max_len=4000, required=False),
            "parentNote": text(p, "parentNote", max_len=6000, required=False),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_update(
            membership=ctx.membership,
            payload=stored,
            publish_sync=False,
        )
        canonical = serialize_update(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


HANDLERS = [WeeklyLearningHandler()]
