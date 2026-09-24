from typing import Any

from apps.academics.models import AcademicClass, AcademicTerm
from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .services import (
    REPORT_CARD_ENTITY,
    compile_class_report_cards,
    principal_review_report_card,
    publish_report_card_sync,
    release_report_card,
    replace_current_sync_payload,
    serialize_report_card,
    submit_report_card,
)
from .visibility import report_card_visible_payload

BATCH_ENTITY = "academic_report_card_batch"


class ReportCardBatchHandler(EntityHandler):
    """Compiles/regenerates every roster student's report card for one class
    and term. entity_id is `f"{classId}:{termId}"` so a recompile updates the
    same batch receipt instead of creating a new one each time. The batch
    record itself is a small receipt, not something any other role reads;
    each student's own academic_report_card record is what carries content
    and visibility.
    """

    entity_type = BATCH_ENTITY
    roles = frozenset({Role.ADMINISTRATOR, Role.PROPRIETOR})

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation == "delete":
            raise Rejected("A report-card compilation receipt cannot be deleted.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the batch entity id.")
        choice(p.get("action"), {"compile"}, "action")
        return {
            "id": entity_id,
            "action": "compile",
            "classId": text(p, "classId", max_len=64),
            "termId": text(p, "termId", max_len=64),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        try:
            academic_class = AcademicClass.objects.get(id=stored["classId"], school=school)
        except (AcademicClass.DoesNotExist, ValueError, TypeError):
            raise Rejected("Class does not exist in this school.")
        try:
            term = AcademicTerm.objects.get(id=stored["termId"], session__school=school)
        except (AcademicTerm.DoesNotExist, ValueError, TypeError):
            raise Rejected("Academic term does not exist in this school.")

        cards = compile_class_report_cards(
            school=school, academic_class=academic_class, term=term, actor=ctx.membership
        )
        for card in cards:
            publish_report_card_sync(card, actor=ctx.membership)

        stored["compiledCount"] = len(cards)
        stored["compiledAt"] = ctx.now


class ReportCardHandler(EntityHandler):
    entity_type = REPORT_CARD_ENTITY
    roles = frozenset({Role.ADMINISTRATOR, Role.PROPRIETOR, Role.PRINCIPAL})

    def visible(self, membership, payload):
        return report_card_visible_payload(membership, payload)

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation != "update":
            raise Rejected("Report cards are compiled by the school, not created or deleted from a device.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the report-card entity id.")

        if ctx.membership.role == Role.PRINCIPAL:
            return {
                "id": entity_id,
                "action": choice(p.get("action"), {"principalReview", "principalReturn"}, "action"),
                "comment": text(p, "comment", max_len=2000, required=False),
            }
        if ctx.membership.role in {Role.ADMINISTRATOR, Role.PROPRIETOR}:
            return {
                "id": entity_id,
                "action": choice(p.get("action"), {"submit", "release"}, "action"),
            }
        raise Rejected("This membership cannot change report cards.")

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        action = stored["action"]
        if action == "submit":
            item = submit_report_card(actor=ctx.membership, external_id=stored["id"])
        elif action == "release":
            item = release_report_card(actor=ctx.membership, external_id=stored["id"])
        elif action in {"principalReview", "principalReturn"}:
            item = principal_review_report_card(
                actor=ctx.membership,
                external_id=stored["id"],
                action="approve" if action == "principalReview" else "return",
                comment=stored.get("comment", ""),
            )
        else:
            raise Rejected("Unsupported report-card action.")

        canonical = serialize_report_card(item)
        replace_current_sync_payload(
            school=ctx.membership.school,
            entity_id=ctx.entity_id,
            payload=canonical,
            actor=ctx.membership,
        )
        stored.clear()
        stored.update(canonical)


HANDLERS = [ReportCardBatchHandler(), ReportCardHandler()]
