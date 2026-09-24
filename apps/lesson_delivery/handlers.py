from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, integer, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .models import LessonPlanReviewDecision
from .services import (
    LESSON_DELIVERY_ENTITY,
    LESSON_PLAN_ENTITY,
    LESSON_PLAN_REVIEW_ENTITY,
    SYLLABUS_PROGRESS_ENTITY,
    review_plan,
    serialize_delivery,
    serialize_plan,
    serialize_review,
    teacher_can_view_progress,
    upsert_delivery,
    upsert_plan,
)


class LessonPlanHandler(EntityHandler):
    entity_type = LESSON_PLAN_ENTITY
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
            str(payload.get("effectiveTeacherId") or ""),
        }:
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the lesson-plan entity id.")
        return {
            "id": entity_id,
            "timetableEntryId": text(p, "timetableEntryId", max_len=64),
            "lessonDate": text(p, "lessonDate", max_len=10),
            "topicId": text(p, "topicId", max_len=64),
            "action": choice(
                p.get("action"),
                {"saveDraft", "submit"},
                "action",
            ),
            "objectives": text(p, "objectives", max_len=6000, required=False),
            "starter": text(p, "starter", max_len=4000, required=False),
            "activities": text(p, "activities", max_len=10000, required=False),
            "assessment": text(p, "assessment", max_len=6000, required=False),
            "resources": text(p, "resources", max_len=6000, required=False),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        plan = upsert_plan(membership=ctx.membership, payload=stored)
        canonical = serialize_plan(plan)
        stored.clear()
        stored.update(canonical)


class LessonPlanReviewHandler(EntityHandler):
    entity_type = LESSON_PLAN_REVIEW_ENTITY
    roles = frozenset({Role.PRINCIPAL})

    def visible(self, membership, payload):
        if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR, Role.PRINCIPAL}:
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if ctx.operation != "create":
            raise Rejected("Lesson-plan reviews are append-only.")
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the lesson-plan-review entity id.")
        decision = p.get("decision")
        if decision == "returned":
            decision = LessonPlanReviewDecision.NEEDS_CHANGES
        return {
            "id": entity_id,
            "planId": text(p, "planId", max_len=128),
            "planVersion": integer(p, "planVersion", minimum=1, maximum=1000000),
            "decision": choice(
                decision,
                set(LessonPlanReviewDecision.values),
                "decision",
            ),
            "comment": text(p, "comment", max_len=4000, required=False),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        review = review_plan(membership=ctx.membership, payload=stored)
        canonical = serialize_review(review)
        stored.clear()
        stored.update(canonical)


class LessonDeliveryHandler(EntityHandler):
    entity_type = LESSON_DELIVERY_ENTITY
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
            str(payload.get("teacherId") or ""),
            str(payload.get("currentEffectiveTeacherId") or ""),
        }:
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the lesson-delivery entity id.")
        return {
            "id": entity_id,
            "timetableEntryId": text(p, "timetableEntryId", max_len=64),
            "lessonDate": text(p, "lessonDate", max_len=10),
            "planId": text(p, "planId", max_len=128),
            "action": choice(
                p.get("action"),
                {"saveDraft", "deliver"},
                "action",
            ),
            "reflection": text(p, "reflection", max_len=8000, required=False),
            "homework": text(p, "homework", max_len=4000, required=False),
            "topicCompleted": boolean(p, "topicCompleted"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_delivery(membership=ctx.membership, payload=stored)
        canonical = serialize_delivery(item)
        stored.clear()
        stored.update(canonical)


class SyllabusProgressHandler(EntityHandler):
    entity_type = SYLLABUS_PROGRESS_ENTITY
    roles = frozenset()

    def authorize(self, ctx: MutationContext) -> None:
        raise Rejected("Syllabus progress is derived from canonical delivered lessons.")

    def visible(self, membership, payload):
        if membership.role in {Role.PROPRIETOR, Role.ADMINISTRATOR}:
            return payload
        if membership.role == Role.PRINCIPAL:
            # Progress records are scoped through a class-subject; the payload is
            # visible to Principal only for Secondary rows.
            if str(payload.get("className") or ""):
                from apps.academics.models import ClassSubject

                item = ClassSubject.objects.filter(id=payload.get("classSubjectId")).select_related(
                    "academic_class"
                ).first()
                if item is not None and item.academic_class.section.strip().casefold() == "secondary":
                    return payload
            return None
        if membership.role == Role.TEACHER and teacher_can_view_progress(
            membership,
            payload.get("classSubjectId"),
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        raise Rejected("Syllabus progress is server-generated.")


HANDLERS = [
    LessonPlanHandler(),
    LessonPlanReviewHandler(),
    LessonDeliveryHandler(),
    SyllabusProgressHandler(),
]
