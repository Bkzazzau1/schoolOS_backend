"""The two channel types Teacher Messages' own demo content still stands in for: a private channel
from one real teacher to school leadership, and a real shared channel per real subject a teacher
currently teaches ("Mathematics Department", ...). Not Specs, for the same reason parent_messages.py
is not one: correct authorization here means checking a real `TeachingAssignment` (this membership
really teaches this real subject right now) - a cross-table, per-record check `Spec.audience`
cannot express (it is only ever given a payload, never a membership - see ../framework.py).

A leadership thread is `leadership-thread-<teacherMembershipId>` - one real thread per real teacher,
the same "one thread per real non-manager participant, managers may reply" shape
`apps/transport/driver_messages.py` already uses for Driver <-> Transport Control. A department
thread is `department-thread-<subjectCode>` - one real, *shared* thread per real subject, since a
department is genuinely a group of real peer teachers, not a single family: any real teacher who
currently teaches that subject may read and write it, the same way any Driver-reading manager may
read any Driver's thread, just without a single distinguished "other side". Every message is its
own row, created once and never changed, the same append-only shape every other real channel in
this app already uses.
"""

import uuid as _uuid

from apps.academics.models import Subject, TeachingAssignment
from apps.core.errors import Rejected
from apps.core.validation import text
from apps.schools.models import Membership, Role
from apps.sync.registry import EntityHandler, MutationContext

from ..framework import MANAGERS

TEACHER_LEADERSHIP_MESSAGE = "teacher_leadership_message"
TEACHER_LEADERSHIP_RECEIPT = "teacher_leadership_receipt"
TEACHER_DEPARTMENT_MESSAGE = "teacher_department_message"
TEACHER_DEPARTMENT_RECEIPT = "teacher_department_receipt"

_LEADERSHIP_PREFIX = "leadership-thread-"
_DEPARTMENT_PREFIX = "department-thread-"


# -- School leadership: one real thread per real teacher --------------------------------------


def _leadership_teacher_id(thread_id: str) -> str:
    return thread_id[len(_LEADERSHIP_PREFIX) :] if thread_id.startswith(_LEADERSHIP_PREFIX) else ""


def _is_real_teacher(school, membership_id: str) -> bool:
    try:
        _uuid.UUID(membership_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return Membership.objects.filter(id=membership_id, school=school, role=Role.TEACHER).exists()


def _may_reach_leadership_thread(membership, teacher_membership_id: str) -> bool:
    if membership.role in MANAGERS:
        return True
    return membership.role == Role.TEACHER and str(membership.id) == teacher_membership_id


class TeacherLeadershipMessageHandler(EntityHandler):
    """The real teacher this thread belongs to, or a manager (school leadership) replying, may write
    here. Nobody else reaches it - not another teacher, not any other role."""

    entity_type = TEACHER_LEADERSHIP_MESSAGE
    roles = MANAGERS | {Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not send messages here.")
        if ctx.operation != "create":
            raise Rejected("A message cannot be changed or removed once sent.")
        thread_id = str(ctx.payload.get("threadId") or "")
        teacher_membership_id = _leadership_teacher_id(thread_id)
        if not teacher_membership_id or not _is_real_teacher(ctx.membership.school, teacher_membership_id):
            raise Rejected("This conversation was not found.")
        if not _may_reach_leadership_thread(ctx.membership, teacher_membership_id):
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
            "authorRole": ctx.membership.role,
            "authorMembershipId": str(ctx.membership.id),
            "createdAt": ctx.now,
        }

    def visible(self, membership, payload):
        thread_id = str(payload.get("threadId") or "")
        teacher_membership_id = _leadership_teacher_id(thread_id)
        if not teacher_membership_id:
            return None
        return payload if _may_reach_leadership_thread(membership, teacher_membership_id) else None


class TeacherLeadershipReceiptHandler(EntityHandler):
    """A real 'I have seen this thread' receipt - the same real teacher or manager who may reach a
    leadership thread may each record their own. Entity id:
    `<membershipId>:thread-seen:<threadId>:<epoch>`, append-only."""

    entity_type = TEACHER_LEADERSHIP_RECEIPT
    roles = MANAGERS | {Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not mark this thread seen.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")
        thread_id = str(ctx.payload.get("threadId") or "")
        teacher_membership_id = _leadership_teacher_id(thread_id)
        if not teacher_membership_id or not _is_real_teacher(ctx.membership.school, teacher_membership_id):
            raise Rejected("This conversation was not found.")
        if not _may_reach_leadership_thread(ctx.membership, teacher_membership_id):
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


# -- Department: one real, shared thread per real subject a teacher currently teaches ----------


def _subject_for_department_thread(school, thread_id: str) -> Subject | None:
    code = thread_id[len(_DEPARTMENT_PREFIX) :] if thread_id.startswith(_DEPARTMENT_PREFIX) else ""
    if not code:
        return None
    return Subject.objects.filter(school=school, code=code).first()


def _teaches_subject(membership, subject: Subject) -> bool:
    return TeachingAssignment.objects.filter(
        teacher_membership=membership,
        ended_at__isnull=True,
        class_subject__subject=subject,
        class_subject__session__school=membership.school,
    ).exists()


def _may_reach_department(membership, subject: Subject) -> bool:
    if membership.role in MANAGERS:
        return True
    return membership.role == Role.TEACHER and _teaches_subject(membership, subject)


class TeacherDepartmentMessageHandler(EntityHandler):
    """Any real teacher who currently teaches this real subject may write into this shared
    department thread, plus a manager for oversight. Nobody else reaches it - not a teacher who
    does not teach this subject, not any other role."""

    entity_type = TEACHER_DEPARTMENT_MESSAGE
    roles = MANAGERS | {Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not send messages here.")
        if ctx.operation != "create":
            raise Rejected("A message cannot be changed or removed once sent.")
        thread_id = str(ctx.payload.get("threadId") or "")
        subject = _subject_for_department_thread(ctx.membership.school, thread_id)
        if subject is None:
            raise Rejected("This conversation was not found.")
        if not _may_reach_department(ctx.membership, subject):
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
            "authorRole": ctx.membership.role,
            "authorMembershipId": str(ctx.membership.id),
            "createdAt": ctx.now,
        }

    def visible(self, membership, payload):
        thread_id = str(payload.get("threadId") or "")
        subject = _subject_for_department_thread(membership.school, thread_id)
        if subject is None:
            return None
        return payload if _may_reach_department(membership, subject) else None


class TeacherDepartmentReceiptHandler(EntityHandler):
    """A real 'I have seen this thread' receipt - any real teacher or manager who may reach a
    department thread may each record their own. Entity id:
    `<membershipId>:thread-seen:<threadId>:<epoch>`, append-only."""

    entity_type = TEACHER_DEPARTMENT_RECEIPT
    roles = MANAGERS | {Role.TEACHER}
    allow_delete = False

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.membership.role not in self.roles:
            raise Rejected("Your role may not mark this thread seen.")
        if ctx.operation != "create":
            raise Rejected("This record is never changed once recorded.")
        thread_id = str(ctx.payload.get("threadId") or "")
        subject = _subject_for_department_thread(ctx.membership.school, thread_id)
        if subject is None:
            raise Rejected("This conversation was not found.")
        if not _may_reach_department(ctx.membership, subject):
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
