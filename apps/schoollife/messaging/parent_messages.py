"""A parent's real messages toward their child's class, and real replies from the child's real current class
teacher - one thread per real family. Not a `Spec`: correct authorization here means checking a real
`GuardianLink` (this membership is really this child's guardian) or a real `TeachingAssignment` for the child's
real current class (this membership really teaches it) - cross-table, per-record checks the generic Spec engine
cannot express at all (`Spec.audience` is only ever given a payload, never a membership - see framework.py). The
same query shape `apps/weekly_learning/visibility.py` already uses for the same two real relationships.

A thread is `channel-<studentCode>`, matching the id `apps/students/parent_sync.py` already publishes to a
parent's own device as `childIds`. Every message is its own row, created once and never changed - a
conversation's history is never edited or deleted, the same append-only shape `apps/transport/driver_messages.py`
already uses for the school's other real messaging channel.
"""

from apps.academics.models import TeachingAssignment
from apps.core.errors import Rejected
from apps.core.validation import text
from apps.schools.models import Role
from apps.students.models import GuardianLink, Student, StudentEnrollment
from apps.sync.registry import EntityHandler, MutationContext

from ..framework import MANAGERS

PARENT_MESSAGE = "parent_message"
_THREAD_PREFIX = "channel-"


def _student_code_from_thread(thread_id: str) -> str:
    return thread_id[len(_THREAD_PREFIX) :] if thread_id.startswith(_THREAD_PREFIX) else ""


def _student_for_thread(school, thread_id: str) -> Student | None:
    code = _student_code_from_thread(thread_id)
    if not code:
        return None
    return Student.objects.filter(school=school, student_code=code).first()


def _is_real_guardian(membership, student: Student) -> bool:
    return GuardianLink.objects.filter(account_user=membership.user, student=student).exists()


def _is_real_class_teacher(membership, student: Student) -> bool:
    enrollment = StudentEnrollment.objects.filter(student=student, ended_at__isnull=True).first()
    if enrollment is None:
        return False
    return TeachingAssignment.objects.filter(
        teacher_membership=membership,
        ended_at__isnull=True,
        class_subject__academic_class__name=enrollment.class_name,
        class_subject__session__school=membership.school,
    ).exists()


def _may_reach_thread(membership, student: Student) -> bool:
    if membership.role in MANAGERS:
        return True
    if membership.role == Role.PARENT:
        return _is_real_guardian(membership, student)
    if membership.role == Role.TEACHER:
        return _is_real_class_teacher(membership, student)
    return False


class ParentMessageHandler(EntityHandler):
    """A real guardian of the child, the child's real current class teacher, or a manager (for oversight) may
    write into this family's thread. Nobody else reaches it at all - not another family's guardian, not a
    teacher who does not teach this child, not even another teacher in the same school."""

    entity_type = PARENT_MESSAGE
    roles = MANAGERS | {Role.PARENT, Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not send messages here.")
        if ctx.operation != "create":
            raise Rejected("A message cannot be changed or removed once sent.")
        thread_id = str(ctx.payload.get("threadId") or "")
        student = _student_for_thread(ctx.membership.school, thread_id)
        if student is None:
            raise Rejected("This conversation was not found.")
        if not _may_reach_thread(ctx.membership, student):
            raise Rejected("This is not a conversation you are part of.")

    def clean(self, ctx: MutationContext) -> dict:
        payload = ctx.payload
        if text(payload, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        thread_id = text(payload, "threadId", max_len=128)
        body = text(payload, "body", max_len=4000)
        return {
            "id": ctx.entity_id,
            "threadId": thread_id,
            "body": body,
            # Who really sent this, and from which side of the conversation - set by the server from who is
            # actually authenticated, never taken from the app, so nobody can claim to speak as the school (or
            # as a family) they are not.
            "authorRole": ctx.membership.role,
            "authorMembershipId": str(ctx.membership.id),
            "createdAt": ctx.now,
        }

    def visible(self, membership, payload):
        thread_id = str(payload.get("threadId") or "")
        student = _student_for_thread(membership.school, thread_id)
        if student is None:
            return None
        return payload if _may_reach_thread(membership, student) else None


PARENT_MESSAGE_RECEIPT = "parent_message_receipt"


class ParentMessageReceiptHandler(EntityHandler):
    """A real 'I have seen this thread' receipt - the same real guardian, real current class
    teacher, or manager who may reach a real family's thread (see `_may_reach_thread`) may each
    record their own receipt for it. Entity id: `<membershipId>:thread-seen:<threadId>:<epoch>`,
    append-only, the same shape `apps/transport/driver_messages.py` already uses for a Driver's own
    receipts - this is what lets a thread's own `unread` state be real instead of always false."""

    entity_type = PARENT_MESSAGE_RECEIPT
    roles = MANAGERS | {Role.PARENT, Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not mark this thread seen.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")
        thread_id = str(ctx.payload.get("threadId") or "")
        student = _student_for_thread(ctx.membership.school, thread_id)
        if student is None:
            raise Rejected("This conversation was not found.")
        if not _may_reach_thread(ctx.membership, student):
            raise Rejected("This is not a conversation you are part of.")

    def clean(self, ctx: MutationContext) -> dict:
        member_id = str(ctx.membership.id)
        parts = ctx.entity_id.split(":")
        if len(parts) < 4 or parts[0] != member_id or parts[1] != "thread-seen" or not parts[-1].isdigit():
            raise Rejected("This receipt does not belong to the active membership.")
        thread_id = ":".join(parts[2:-1])
        if text(ctx.payload, "threadId", max_len=160) != thread_id:
            raise Rejected("threadId must match the record.")
        return {
            "id": ctx.entity_id,
            "threadId": thread_id,
            "membershipId": member_id,
            "seenAt": ctx.now,
        }

    def visible(self, membership, payload):
        if membership.role in MANAGERS:
            return payload
        return payload if payload.get("membershipId") == str(membership.id) else None
