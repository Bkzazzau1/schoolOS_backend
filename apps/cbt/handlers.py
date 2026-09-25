from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .models import CbtResultMode
from .services import (
    CBT_ATTEMPT_ENTITY,
    CBT_TEST_ENTITY,
    answer_question,
    replace_current_sync_payload,
    serialize_cbt_attempt,
    serialize_cbt_test,
    start_attempt,
    submit_attempt,
    upsert_cbt_test,
)
from .visibility import cbt_attempt_visible_payload, cbt_test_visible_payload


class CbtTestHandler(EntityHandler):
    entity_type = CBT_TEST_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        return cbt_test_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("CBT tests are historical records and cannot be deleted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the CBT test entity id.")
        action = choice(p.get("action"), {"saveDraft", "publish", "close"}, "action")

        cleaned = {
            "id": entity_id,
            "action": action,
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "termId": text(p, "termId", max_len=64),
        }
        if action in {"saveDraft", "publish"}:
            cleaned.update(
                {
                    "title": text(p, "title", max_len=240, required=False),
                    "durationMinutes": integer(p, "durationMinutes", minimum=0, maximum=600),
                    "instructions": text(p, "instructions", max_len=5000, required=False),
                    "resultMode": choice(
                        p.get("resultMode") or CbtResultMode.SCORE_ONLY,
                        set(CbtResultMode.values),
                        "resultMode",
                    ),
                    "questions": p.get("questions") or [],
                }
            )
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_cbt_test(membership=ctx.membership, payload=stored, publish_sync=False)
        canonical = serialize_cbt_test(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_type=CBT_TEST_ENTITY,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


class CbtAttemptHandler(EntityHandler):
    entity_type = CBT_ATTEMPT_ENTITY
    roles = frozenset({Role.STUDENT})

    def authorize(self, ctx: MutationContext) -> None:
        super().authorize(ctx)
        if ctx.operation != "update":
            raise Rejected("A CBT attempt is created by the school when a test is published, not by a device.")

    def visible(self, membership, payload):
        return cbt_attempt_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the CBT attempt entity id.")
        action = choice(p.get("action"), {"start", "answer", "submit"}, "action")
        cleaned = {"id": entity_id, "action": action}
        if action == "answer":
            cleaned["questionIndex"] = integer(p, "questionIndex", minimum=0, maximum=1000)
            cleaned["optionIndex"] = integer(p, "optionIndex", minimum=0, maximum=50)
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        action = stored["action"]
        if action == "start":
            item = start_attempt(membership=ctx.membership, external_id=stored["id"], publish_sync=False)
        elif action == "answer":
            item = answer_question(
                membership=ctx.membership,
                external_id=stored["id"],
                question_index=stored["questionIndex"],
                option_index=stored["optionIndex"],
                publish_sync=False,
            )
        elif action == "submit":
            item = submit_attempt(membership=ctx.membership, external_id=stored["id"], publish_sync=False)
        else:
            raise Rejected("Unsupported CBT attempt action.")

        canonical = serialize_cbt_attempt(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_type=CBT_ATTEMPT_ENTITY,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


HANDLERS = [CbtTestHandler(), CbtAttemptHandler()]
