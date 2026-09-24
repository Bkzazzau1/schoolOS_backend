from django.db import transaction

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler

from .workspace_payloads import student_workspace_payload


STUDENT_CLASS_LINK_ENTITY = "student_class_link"


class StudentClassLinkHandler(EntityHandler):
    """Read-only server link between a Student membership and canonical profile/class."""

    entity_type = STUDENT_CLASS_LINK_ENTITY
    roles = frozenset()

    def authorize(self, ctx) -> None:
        raise Rejected("Student class links are managed by the SchoolOS server.")

    def clean(self, ctx):
        raise Rejected("Student class links are managed by the SchoolOS server.")

    def visible(self, membership, payload):
        if membership.role != Role.STUDENT:
            return None
        if payload.get("studentMembershipId") != str(membership.id):
            return None
        return payload


@transaction.atomic
def publish_student_class_link(student, *, actor=None) -> None:
    if not student.account_user_id:
        return
    membership = Membership.objects.filter(
        user_id=student.account_user_id,
        school=student.school,
        role=Role.STUDENT,
        is_active=True,
    ).first()
    if membership is None:
        return

    entity_id = str(membership.id)
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=student.school,
            entity_type=STUDENT_CLASS_LINK_ENTITY,
            entity_id=entity_id,
        )
        .first()
    )

    # Keep the private profile/history after transfer, withdrawal or graduation,
    # but student_workspace_payload removes className/enrollmentActive when no
    # active enrollment exists. Class-based resources therefore stop resolving
    # without erasing the pupil's historical record.
    payload = {
        "studentMembershipId": str(membership.id),
        **student_workspace_payload(student),
    }
    if record is None:
        SyncRecord.objects.create(
            school=student.school,
            entity_type=STUDENT_CLASS_LINK_ENTITY,
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


HANDLER = StudentClassLinkHandler()
