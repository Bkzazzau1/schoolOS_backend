from typing import Any

from apps.academics.models import TeachingAssignment
from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .models import TimetableEntry, TimetableOverride
from .services import (
    TIMETABLE_ENTRY_ENTITY,
    TIMETABLE_OVERRIDE_ENTITY,
    refresh_term_sync,
    serialize_entry,
    serialize_override,
    upsert_entry,
    upsert_override,
)
from .teacher_sync import (
    TEACHER_TIMETABLE_LINK_ENTITY,
    publish_school_teacher_timetable_links,
)


TEACHER_TIMETABLE_INTENT_ENTITY = "teacher_timetable_intent"
_TIMETABLE_WRITE_ROLES = frozenset({Role.ADMINISTRATOR})
_TIMETABLE_READ_ROLES = {Role.ADMINISTRATOR, Role.PROPRIETOR}
_INTENT_TYPES = {"syncRequest", "changeRequest", "issueReport"}


def _optional_text(payload, key, *, max_len=250):
    return text(payload, key, max_len=max_len, required=False)


def _positive_int(payload, key, *, minimum=1, maximum):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(f"{key} must be an integer.")
    if value < minimum or value > maximum:
        raise Rejected(f"{key} is outside the allowed range.")
    return value


def _canonicalize_record(ctx, payload):
    SyncRecord.objects.filter(
        school=ctx.membership.school,
        entity_type=ctx.entity_type,
        entity_id=ctx.entity_id,
    ).update(payload=payload)


def _principal_can_see(membership, payload):
    return (
        membership.role == Role.PRINCIPAL
        and str(payload.get("section") or "").strip().casefold() == "secondary"
    )


class TimetableEntryHandler(EntityHandler):
    entity_type = TIMETABLE_ENTRY_ENTITY
    roles = _TIMETABLE_WRITE_ROLES

    def visible(self, membership, payload):
        # Teachers intentionally do not read global rows. They receive one private
        # teacher_timetable_schedule record that can actively remove old lessons
        # after a handover instead of leaving stale cached rows behind.
        if membership.role in _TIMETABLE_READ_ROLES:
            return payload
        if _principal_can_see(membership, payload):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the timetable-entry entity id.")
        return {
            "id": entity_id,
            "termId": text(p, "termId", max_len=64),
            "classSubjectId": text(p, "classSubjectId", max_len=64),
            "dayOfWeek": _positive_int(p, "dayOfWeek", maximum=7),
            "periodNumber": _positive_int(p, "periodNumber", maximum=30),
            "startTime": text(p, "startTime", max_len=8),
            "endTime": text(p, "endTime", max_len=8),
            "room": _optional_text(p, "room", max_len=120),
            "isActive": boolean(p, "isActive"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_entry(membership=ctx.membership, payload=stored)
        item = TimetableEntry.objects.select_related(
            "school",
            "term__session",
            "class_subject__academic_class",
            "class_subject__subject",
        ).get(pk=item.pk)
        _canonicalize_record(ctx, serialize_entry(item))
        refresh_term_sync(
            item.term,
            actor=ctx.membership,
            exclude_external_id=item.external_id,
        )
        publish_school_teacher_timetable_links(
            item.school,
            actor=ctx.membership,
        )


class TimetableOverrideHandler(EntityHandler):
    entity_type = TIMETABLE_OVERRIDE_ENTITY
    roles = _TIMETABLE_WRITE_ROLES

    def visible(self, membership, payload):
        lesson = payload.get("lesson") if isinstance(payload.get("lesson"), dict) else {}
        if membership.role in _TIMETABLE_READ_ROLES:
            return payload
        if _principal_can_see(membership, lesson):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the timetable-override entity id.")
        return {
            "id": entity_id,
            "timetableEntryId": text(p, "timetableEntryId", max_len=64),
            "lessonDate": text(p, "lessonDate", max_len=10),
            "substituteTeacherId": _optional_text(
                p,
                "substituteTeacherId",
                max_len=64,
            ),
            "room": _optional_text(p, "room", max_len=120),
            "note": _optional_text(p, "note", max_len=2000),
            "isCancelled": boolean(p, "isCancelled"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        item = upsert_override(membership=ctx.membership, payload=stored)
        item = TimetableOverride.objects.select_related(
            "timetable_entry__school",
            "timetable_entry__term__session",
            "timetable_entry__class_subject__academic_class",
            "timetable_entry__class_subject__subject",
            "substitute_teacher_membership__user",
        ).get(pk=item.pk)
        _canonicalize_record(ctx, serialize_override(item))
        publish_school_teacher_timetable_links(
            item.school,
            actor=ctx.membership,
        )


class TeacherTimetableIntentHandler(EntityHandler):
    entity_type = TEACHER_TIMETABLE_INTENT_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        if membership.role == Role.ADMINISTRATOR:
            return payload
        if membership.role == Role.PRINCIPAL and "secondary" in {
            str(value).casefold() for value in payload.get("scopeSections", [])
        }:
            return payload
        if (
            membership.role == Role.TEACHER
            and payload.get("actorMembershipId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=64)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the timetable-intent entity id.")
        intent_type = choice(p.get("type"), _INTENT_TYPES, "type")
        lesson_id = _optional_text(p, "lessonId", max_len=64)
        if intent_type == "issueReport" and not lesson_id:
            raise Rejected("A timetable issue report must reference a lesson.")

        sections = set()
        if lesson_id:
            entry = (
                TimetableEntry.objects.filter(
                    school=ctx.membership.school,
                    external_id=lesson_id,
                    is_active=True,
                )
                .select_related("class_subject__academic_class")
                .first()
            )
            if entry is None:
                raise Rejected("Timetable lesson does not exist.")
            owns = TeachingAssignment.objects.filter(
                class_subject=entry.class_subject,
                teacher_membership=ctx.membership,
                ended_at__isnull=True,
            ).exists()
            if not owns:
                raise Rejected("A Teacher may report only their own assigned timetable lesson.")
            sections.add(entry.class_subject.academic_class.section.strip().casefold())
        else:
            sections.update(
                value.strip().casefold()
                for value in TeachingAssignment.objects.filter(
                    teacher_membership=ctx.membership,
                    ended_at__isnull=True,
                ).values_list("class_subject__academic_class__section", flat=True)
                if value.strip()
            )

        return {
            "id": entity_id,
            "type": intent_type,
            "status": "queuedForReview",
            "actorMembershipId": str(ctx.membership.id),
            "createdAt": ctx.now,
            "lessonId": lesson_id or None,
            "detail": _optional_text(p, "detail", max_len=2000),
            "scopeSections": sorted(sections),
        }


class TeacherTimetableLinkHandler(EntityHandler):
    entity_type = TEACHER_TIMETABLE_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx) -> None:
        raise Rejected("Teacher timetable schedules are managed by the SchoolOS server.")

    def clean(self, ctx):
        raise Rejected("Teacher timetable schedules are managed by the SchoolOS server.")

    def visible(self, membership, payload):
        if membership.role != Role.TEACHER:
            return None
        if payload.get("teacherMembershipId") != str(membership.id):
            return None
        return payload


HANDLERS = [
    TimetableEntryHandler(),
    TimetableOverrideHandler(),
    TeacherTimetableIntentHandler(),
    TeacherTimetableLinkHandler(),
]
