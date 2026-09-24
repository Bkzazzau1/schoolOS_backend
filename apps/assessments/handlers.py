from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .models import AssessmentType
from .services import (
    ASSESSMENT_ENTITY,
    correct_score,
    lock_assessment,
    principal_review,
    release_assessment,
    replace_current_sync_payload,
    return_for_correction,
    save_scores,
    serialize_assessment,
    submit_assessment,
    upsert_assessment,
)
from .visibility import assessment_visible_payload

_TEACHER_ACTIONS = {"saveDraft", "publish", "saveScores", "submit"}
_MANAGER_ACTIONS = {"lock", "release", "returnForCorrection"}
_CORRECTION_ACTION = "correctScore"
_PRINCIPAL_ACTIONS = {"principalReview", "principalReturn"}


class AssessmentHandler(EntityHandler):
    entity_type = ASSESSMENT_ENTITY
    roles = frozenset({Role.TEACHER, Role.ADMINISTRATOR, Role.PROPRIETOR, Role.PRINCIPAL})

    def visible(self, membership, payload):
        return assessment_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("Assessments are historical records and cannot be deleted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the assessment entity id.")

        all_actions = _TEACHER_ACTIONS | _MANAGER_ACTIONS | _PRINCIPAL_ACTIONS | {_CORRECTION_ACTION}
        action = choice(p.get("action"), all_actions, "action")

        if action in {"saveDraft", "publish"}:
            if ctx.membership.role != Role.TEACHER:
                raise Rejected("Only a Teacher membership can author an assessment.")
            return {
                "id": entity_id,
                "action": action,
                "classSubjectId": text(p, "classSubjectId", max_len=64),
                "termId": text(p, "termId", max_len=64),
                "type": choice(p.get("type"), set(AssessmentType.values), "type"),
                "title": text(p, "title", max_len=240, required=False),
                "maximumScore": integer(p, "maximumScore", minimum=0, maximum=1000000),
                "weight": p.get("weight"),
            }

        if action in {"saveScores", "submit"}:
            if ctx.membership.role != Role.TEACHER:
                raise Rejected("Only a Teacher membership can enter or submit assessment scores.")
            entries = p.get("entries") or []
            if not isinstance(entries, list):
                raise Rejected("entries must be a list.")
            cleaned_entries = []
            for row in entries:
                if not isinstance(row, dict):
                    raise Rejected("Each score entry must be an object.")
                cleaned_entries.append(
                    {
                        "studentId": text(row, "studentId", max_len=80),
                        "score": row.get("score"),
                    }
                )
            return {"id": entity_id, "action": action, "entries": cleaned_entries}

        if action in _MANAGER_ACTIONS:
            if ctx.membership.role not in {Role.ADMINISTRATOR, Role.PROPRIETOR}:
                raise Rejected("Only the Proprietor or Administrator can lock, release or return an assessment.")
            return {
                "id": entity_id,
                "action": action,
                "comment": text(p, "comment", max_len=2000, required=False),
            }

        if action == _CORRECTION_ACTION:
            if ctx.membership.role not in {Role.TEACHER, Role.ADMINISTRATOR, Role.PROPRIETOR}:
                raise Rejected("This membership cannot correct assessment scores.")
            return {
                "id": entity_id,
                "action": action,
                "studentId": text(p, "studentId", max_len=80),
                "score": p.get("score"),
                "comment": text(p, "comment", max_len=2000, required=False),
            }

        if action in _PRINCIPAL_ACTIONS:
            if ctx.membership.role != Role.PRINCIPAL:
                raise Rejected("Only a Principal membership can record an academic review.")
            return {
                "id": entity_id,
                "action": action,
                "comment": text(p, "comment", max_len=2000, required=False),
            }

        raise Rejected("Unsupported assessment action.")

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        action = stored["action"]
        if action in {"saveDraft", "publish"}:
            item = upsert_assessment(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "saveScores":
            item = save_scores(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "submit":
            item = submit_assessment(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "lock":
            item = lock_assessment(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "release":
            item = release_assessment(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "returnForCorrection":
            item = return_for_correction(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == _CORRECTION_ACTION:
            item = correct_score(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action in _PRINCIPAL_ACTIONS:
            item = principal_review(membership=ctx.membership, payload=stored, publish_sync=False)
        else:
            raise Rejected("Unsupported assessment action.")

        canonical = serialize_assessment(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


HANDLERS = [AssessmentHandler()]
