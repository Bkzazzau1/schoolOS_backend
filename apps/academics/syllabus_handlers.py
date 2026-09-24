from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .models import AcademicLifecycleStatus, CurriculumTopic, TeachingAssignment


PROGRESS_ENTITY = "teacher_syllabus_progress"
EVENT_ENTITY = "teacher_syllabus_progress_event"
_PROGRESS_STATUSES = {"completed", "inProgress"}
_EVENT_ACTIONS = {"markedComplete", "markedInProgress"}
_READ_ROLES = {Role.PROPRIETOR, Role.ADMINISTRATOR, Role.PRINCIPAL}


def _topic_for_teacher(ctx: MutationContext, topic_id: str) -> CurriculumTopic:
    topic = (
        CurriculumTopic.objects.select_related(
            "term",
            "class_subject__session",
            "class_subject__academic_class",
        )
        .filter(
            id=topic_id,
            class_subject__session__school=ctx.membership.school,
        )
        .first()
    )
    if topic is None:
        raise Rejected("This canonical curriculum topic does not exist in your school.")
    if topic.term.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed-term syllabus record is historical and cannot be changed.")
    if not TeachingAssignment.objects.filter(
        class_subject=topic.class_subject,
        teacher_membership=ctx.membership,
        ended_at__isnull=True,
    ).exists():
        raise Rejected("This topic is not in your active teaching assignment.")
    return topic


class TeacherSyllabusProgressHandler(EntityHandler):
    entity_type = PROGRESS_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        if membership.role in _READ_ROLES:
            return payload
        if (
            membership.role == Role.TEACHER
            and payload.get("actorMembershipId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        topic_id = text(p, "id", max_len=64)
        if topic_id != ctx.entity_id:
            raise Rejected("id must match the syllabus-progress entity id.")
        topic = _topic_for_teacher(ctx, topic_id)
        status = choice(p.get("reportedStatus"), _PROGRESS_STATUSES, "reportedStatus")
        previous_version = 0
        if ctx.existing is not None:
            value = ctx.existing.get("version")
            if isinstance(value, int) and value > 0:
                previous_version = value
        return {
            "id": topic_id,
            "className": topic.class_subject.academic_class.name,
            "week": topic.sequence,
            "reportedStatus": status,
            "actorMembershipId": str(ctx.membership.id),
            "version": previous_version + 1,
            "updatedAt": ctx.now,
        }


class TeacherSyllabusProgressEventHandler(EntityHandler):
    entity_type = EVENT_ENTITY
    roles = frozenset({Role.TEACHER})

    def visible(self, membership, payload):
        if membership.role in _READ_ROLES:
            return payload
        if (
            membership.role == Role.TEACHER
            and payload.get("actorMembershipId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        entity_id = text(p, "id", max_len=128)
        if entity_id != ctx.entity_id:
            raise Rejected("id must match the syllabus-progress-event entity id.")
        topic_id = text(p, "recordId", max_len=64)
        _topic_for_teacher(ctx, topic_id)
        action = choice(p.get("action"), _EVENT_ACTIONS, "action")
        progress = SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=PROGRESS_ENTITY,
            entity_id=topic_id,
            deleted=False,
        ).first()
        if progress is None:
            raise Rejected("Save the syllabus progress record before its audit event.")
        version = progress.payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise Rejected("The syllabus progress record has no valid canonical version.")
        expected_action = (
            "markedComplete"
            if progress.payload.get("reportedStatus") == "completed"
            else "markedInProgress"
        )
        if action != expected_action:
            raise Rejected("The syllabus audit action does not match the saved progress status.")
        return {
            "id": entity_id,
            "recordId": topic_id,
            "action": action,
            "actorMembershipId": str(ctx.membership.id),
            "version": version,
            "occurredAt": ctx.now,
        }


HANDLERS = [
    TeacherSyllabusProgressHandler(),
    TeacherSyllabusProgressEventHandler(),
]
