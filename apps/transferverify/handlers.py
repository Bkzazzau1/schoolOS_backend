from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.sync.registry import EntityHandler, MutationContext

from .models import BadDebtStatus
from .services import (
    BAD_DEBT_ENTITY,
    advance_status,
    classify,
    replace_current_sync_payload,
    resolve,
    serialize_classification,
    update_classification,
)
from .visibility import bad_debt_classification_visible_payload


class BadDebtClassificationHandler(EntityHandler):
    """A school's own private bad debt classification. Authority here is
    duty-based (the Proprietor, or a Finance delegate holding the
    finance.bad_debt_classification duty - see apps.transferverify.services),
    not role-based, so no fixed `roles` set applies: every action is
    re-checked against that duty independently of which role the acting
    membership happens to hold, the same way apps.concessions already works.
    """

    entity_type = BAD_DEBT_ENTITY

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A bad debt classification is a historical record and cannot be deleted.")

    def visible(self, membership, payload):
        return bad_debt_classification_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the classification entity id.")
        action = choice(p.get("action"), {"classify", "update", "advanceStatus", "resolve"}, "action")
        cleaned: dict[str, Any] = {"id": entity_id, "action": action}
        if action == "classify":
            cleaned["studentId"] = text(p, "studentId", max_len=64)
            cleaned["outstandingAmountMinor"] = integer(p, "outstandingAmountMinor", minimum=1)
            cleaned["reason"] = text(p, "reason", max_len=2000, required=False)
            cleaned["notes"] = text(p, "notes", max_len=2000, required=False)
            cleaned["evidenceReference"] = text(p, "evidenceReference", max_len=200, required=False)
        elif action == "update":
            if "outstandingAmountMinor" in p:
                cleaned["outstandingAmountMinor"] = integer(p, "outstandingAmountMinor", minimum=1)
            cleaned["reason"] = text(p, "reason", max_len=2000, required=False)
            cleaned["notes"] = text(p, "notes", max_len=2000, required=False)
            cleaned["evidenceReference"] = text(p, "evidenceReference", max_len=200, required=False)
        elif action == "advanceStatus":
            # Never OUTSTANDING (that's only the starting state) or RESOLVED
            # (that has its own dedicated, always-available action below).
            cleaned["status"] = choice(
                p.get("status"),
                {BadDebtStatus.RECOVERY_IN_PROGRESS.value, BadDebtStatus.BAD_DEBT.value},
                "status",
            )
        elif action == "resolve":
            cleaned["note"] = text(p, "note", max_len=2000, required=False)
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        action = stored["action"]
        if action == "classify":
            item = classify(membership=ctx.membership, payload=stored, publish_sync=False)
        elif action == "update":
            item = update_classification(
                membership=ctx.membership, external_id=stored["id"], payload=stored, publish_sync=False
            )
        elif action == "advanceStatus":
            item = advance_status(
                membership=ctx.membership, external_id=stored["id"], status=stored["status"], publish_sync=False
            )
        elif action == "resolve":
            item = resolve(
                membership=ctx.membership, external_id=stored["id"], note=stored.get("note", ""), publish_sync=False
            )
        else:
            raise Rejected("Unsupported bad debt classification action.")

        canonical = serialize_classification(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_type=BAD_DEBT_ENTITY,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


HANDLERS = [BadDebtClassificationHandler()]
