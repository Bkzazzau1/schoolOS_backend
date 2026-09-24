from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schools.models import Role
from apps.sync.registry import EntityHandler, MutationContext

from .models import AttendanceMark, LessonAttendanceState
from .services import LESSON_ATTENDANCE_ENTITY, serialize_register, upsert_register


class LessonAttendanceRegisterHandler(EntityHandler):
    entity_type = LESSON_ATTENDANCE_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        if membership.role == Role.ADMINISTRATOR:
            return payload
        if (
            membership.role == Role.PRINCIPAL
            and str(payload.get("section") or "").strip().casefold() == "secondary"
        ):
            return payload
        if (
            membership.role == Role.TEACHER
            and payload.get("teacherId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the lesson-attendance entity id.")

        raw_entries = p.get("entries")
        if not isinstance(raw_entries, list):
            raise Rejected("entries must be a list.")
        entries = []
        seen = set()
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise Rejected("Each attendance entry must be an object.")
            student_id = text(raw, "studentId", max_len=80)
            if student_id in seen:
                raise Rejected("Attendance entries must contain unique studentId values.")
            seen.add(student_id)
            status = choice(raw.get("status"), set(AttendanceMark.values), "status")
            note = text(raw, "note", max_len=500, required=False)
            entries.append(
                {
                    "studentId": student_id,
                    "status": status,
                    "note": note,
                }
            )

        return {
            "id": entity_id,
            "timetableEntryId": text(p, "timetableEntryId", max_len=64),
            "lessonDate": text(p, "lessonDate", max_len=10),
            "topicId": text(p, "topicId", max_len=64, required=False),
            "state": choice(
                p.get("state"),
                set(LessonAttendanceState.values),
                "state",
            ),
            "entries": entries,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        register = upsert_register(membership=ctx.membership, payload=stored)
        canonical = serialize_register(register)
        # upsert_register has already replaced the generic SyncRecord payload
        # with this canonical form. Keep `stored` in sync for any downstream
        # in-transaction consumers.
        stored.clear()
        stored.update(canonical)


HANDLERS = [LessonAttendanceRegisterHandler()]
