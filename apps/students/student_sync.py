from django.db import transaction

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler

from .models import EnrollmentStatus


STUDENT_CLASS_LINK_ENTITY = "student_class_link"


class StudentClassLinkHandler(EntityHandler):
    """Read-only server link between a Student membership and canonical class."""

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
    enrollment = (
        student.enrollments.filter(status=EnrollmentStatus.ACTIVE)
        .order_by("-started_at", "-id")
        .first()
    )

    if enrollment is None:
        # The pupil left the active roster. Publish a tombstone so every device
        # removes the previous class assignment rather than continuing to show
        # class-scoped CBTs or resources from the old enrollment.
        if record is not None and not record.deleted:
            record.deleted = True
            record.version += 1
            record.updated_by = actor
            record.save(update_fields=["deleted", "version", "updated_by"])
        return

    payload = {
        "studentMembershipId": str(membership.id),
        "canonicalStudentId": str(student.id),
        "studentId": student.student_code,
        "admissionNumber": student.admission_number,
        "academicSection": enrollment.academic_section,
        "className": enrollment.class_name,
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
