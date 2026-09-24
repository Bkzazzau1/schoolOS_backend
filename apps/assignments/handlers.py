from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .models import AssignmentType
from .services import (
    ASSIGNMENT_ENTITY,
    SUBMISSION_ENTITY,
    mark_submission,
    replace_current_sync_payload,
    serialize_assignment,
    serialize_submission,
    upsert_assignment,
    upsert_student_submission,
)
from .visibility import assignment_visible_payload, submission_visible_payload


class AssignmentHandler(EntityHandler):
    entity_type = ASSIGNMENT_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        return assignment_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("Assignments are historical records and cannot be deleted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the assignment entity id.")
        action = choice(
            p.get("action"),
            {"saveDraft", "publish", "revise", "close"},
            "action",
        )
        return {
            "id": entity_id,
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "termId": text(p, "termId", max_len=64),
            "topicId": text(p, "topicId", max_len=64, required=False),
            "action": action,
            "type": choice(p.get("type"), set(AssignmentType.values), "type"),
            "title": text(p, "title", max_len=240, required=False),
            "instructions": text(p, "instructions", max_len=30000, required=False),
            "dueAt": text(p, "dueAt", max_len=80, required=False),
            "maximumScore": integer(p, "maximumScore", minimum=0, maximum=1000000),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_assignment(
            membership=ctx.membership,
            payload=stored,
            publish_sync=False,
        )
        canonical = serialize_assignment(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_type=ASSIGNMENT_ENTITY,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


class AssignmentSubmissionHandler(EntityHandler):
    entity_type = SUBMISSION_ENTITY
    roles = frozenset({Role.STUDENT, Role.TEACHER})

    def visible(self, membership, payload):
        return submission_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("Assignment submissions are historical records and cannot be deleted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the assignment-submission entity id.")

        if ctx.membership.role == Role.STUDENT:
            return {
                "id": entity_id,
                "assignmentId": text(p, "assignmentId", max_len=128),
                "action": choice(
                    p.get("action"), {"saveDraft", "submit"}, "action"
                ),
                "responseText": text(
                    p, "responseText", max_len=30000, required=False
                ),
            }

        if ctx.membership.role == Role.TEACHER:
            action = choice(p.get("action"), {"grade", "return"}, "action")
            score = p.get("score")
            if action == "grade" and score is None:
                raise Rejected("score is required when grading submitted work.")
            return {
                "id": entity_id,
                "action": action,
                "score": score,
                "feedback": text(p, "feedback", max_len=10000, required=False),
            }

        raise Rejected("This membership cannot change assignment submissions.")

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        if ctx.membership.role == Role.STUDENT:
            item = upsert_student_submission(
                membership=ctx.membership,
                payload=stored,
                publish_sync=False,
            )
        else:
            item = mark_submission(
                membership=ctx.membership,
                payload=stored,
                publish_sync=False,
            )
        canonical = serialize_submission(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_type=SUBMISSION_ENTITY,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        # Submission/marking changes alter assignment counters. This is an
        # independent server-side change and therefore receives its own sync seq.
        from .services import publish_assignment_sync

        publish_assignment_sync(item.assignment, actor=ctx.membership)
        stored.clear()
        stored.update(canonical)


HANDLERS = [AssignmentHandler(), AssignmentSubmissionHandler()]
