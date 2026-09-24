from django.db import transaction

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler

from .models import GuardianLink
from .workspace_payloads import parent_child_workspace_payload


PARENT_FAMILY_LINK_ENTITY = "parent_family_link"


class ParentFamilyLinkHandler(EntityHandler):
    """Read-only, server-generated family workspace for one Parent membership."""

    entity_type = PARENT_FAMILY_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx) -> None:
        raise Rejected("Parent family links are managed by the SchoolOS server.")

    def clean(self, ctx):
        raise Rejected("Parent family links are managed by the SchoolOS server.")

    def visible(self, membership, payload):
        if membership.role != Role.PARENT:
            return None
        if payload.get("parentMembershipId") != str(membership.id):
            return None
        return payload


@transaction.atomic
def publish_parent_family_link(parent_membership: Membership, *, actor=None) -> None:
    links = list(
        GuardianLink.objects.filter(
            account_user=parent_membership.user,
            student__school=parent_membership.school,
        )
        .select_related("student")
        .order_by("student__student_code", "created_at")
    )
    students = []
    seen = set()
    for link in links:
        if link.student_id in seen:
            continue
        seen.add(link.student_id)
        students.append(link.student)

    payload = {
        "parentMembershipId": str(parent_membership.id),
        "childIds": [student.student_code for student in students],
        "children": [parent_child_workspace_payload(student) for student in students],
    }
    entity_id = str(parent_membership.id)
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=parent_membership.school,
            entity_type=PARENT_FAMILY_LINK_ENTITY,
            entity_id=entity_id,
        )
        .first()
    )
    if record is None:
        SyncRecord.objects.create(
            school=parent_membership.school,
            entity_type=PARENT_FAMILY_LINK_ENTITY,
            entity_id=entity_id,
            payload=payload,
            version=1,
            deleted=False,
            updated_by=actor,
        )
        return
    if record.payload == payload and not record.deleted:
        return
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.updated_by = actor
    record.save(update_fields=["payload", "deleted", "version", "updated_by"])


def publish_parent_family_links_for_student(student, *, actor=None) -> None:
    user_ids = list(
        student.guardians.exclude(account_user_id=None)
        .values_list("account_user_id", flat=True)
        .distinct()
    )
    if not user_ids:
        return
    memberships = Membership.objects.filter(
        user_id__in=user_ids,
        school=student.school,
        role=Role.PARENT,
        is_active=True,
    )
    for membership in memberships:
        publish_parent_family_link(membership, actor=actor)


HANDLER = ParentFamilyLinkHandler()
