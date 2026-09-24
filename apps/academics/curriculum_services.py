import uuid

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.students.models import EnrollmentStatus, Student
from apps.sync.models import SyncRecord

from .models import (
    AcademicClass,
    AcademicLifecycleStatus,
    AcademicSession,
    AcademicTerm,
    ClassSubject,
    CurriculumRequirement,
    CurriculumTopic,
    EnrollmentAcademicContext,
    StudentSubjectSelection,
    Subject,
    TeachingAssignment,
)


TEACHER_CLASS_LINK_ENTITY = "teacher_class_assignment"


def _subject(school, value: str):
    try:
        item = Subject.objects.get(id=value)
    except (Subject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Subject does not exist.")
    if item.school_id != school.id:
        raise Rejected("Subject does not belong to this school.")
    return item


def _session(school, value: str):
    try:
        item = AcademicSession.objects.get(id=value)
    except (AcademicSession.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic session does not exist.")
    if item.school_id != school.id:
        raise Rejected("Academic session does not belong to this school.")
    return item


def _academic_class(school, value: str):
    try:
        item = AcademicClass.objects.get(id=value)
    except (AcademicClass.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic class does not exist.")
    if item.school_id != school.id:
        raise Rejected("Academic class does not belong to this school.")
    return item


def _class_subject(school, value: str):
    try:
        item = ClassSubject.objects.select_related(
            "session", "academic_class", "subject"
        ).get(id=value)
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class-subject requirement does not exist.")
    if item.session.school_id != school.id:
        raise Rejected("Class-subject requirement does not belong to this school.")
    return item


def _term(school, value: str):
    try:
        item = AcademicTerm.objects.select_related("session").get(id=value)
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not exist.")
    if item.session.school_id != school.id:
        raise Rejected("Academic term does not belong to this school.")
    return item


def _teacher_membership(school, value: str):
    try:
        item = Membership.objects.select_related("user", "school").get(id=value)
    except (Membership.DoesNotExist, ValueError, TypeError):
        raise Rejected("Teacher membership does not exist.")
    if item.school_id != school.id or item.role != Role.TEACHER or not item.is_active:
        raise Rejected("Choose an active Teacher membership in this school.")
    return item


def _principal_secondary_scope(actor: Membership, academic_class: AcademicClass):
    if actor.role == Role.PRINCIPAL and academic_class.section.strip().lower() != "secondary":
        raise Rejected("This Principal can manage only Secondary curriculum and teaching assignments.")


def serialize_subject(item: Subject) -> dict:
    return {
        "id": str(item.id),
        "code": item.code,
        "name": item.name,
        "shortName": item.short_name,
        "section": item.section,
        "isActive": item.is_active,
    }


def serialize_class_subject(item: ClassSubject) -> dict:
    return {
        "id": str(item.id),
        "sessionId": str(item.session_id),
        "classId": str(item.academic_class_id),
        "className": item.academic_class.name,
        "subjectId": str(item.subject_id),
        "subjectCode": item.subject.code,
        "subject": item.subject.name,
        "requirement": item.requirement,
        "periodsPerWeek": item.periods_per_week,
        "isActive": item.is_active,
    }


def serialize_topic(item: CurriculumTopic) -> dict:
    return {
        "id": str(item.id),
        "classSubjectId": str(item.class_subject_id),
        "termId": str(item.term_id),
        "sequence": item.sequence,
        "title": item.title,
        "description": item.description,
    }


def _assignment_version(item: TeachingAssignment) -> int:
    version = 1
    previous = item.previous_assignment
    while previous is not None:
        version += 1
        previous = previous.previous_assignment
    return version


def serialize_teaching_assignment(item: TeachingAssignment) -> dict:
    class_subject = item.class_subject
    return {
        "id": item.external_id,
        "canonicalAssignmentId": str(item.id),
        "sessionId": str(class_subject.session_id),
        "classSubjectId": str(class_subject.id),
        "classId": str(class_subject.academic_class_id),
        "className": class_subject.academic_class.name,
        "subjectId": str(class_subject.subject_id),
        "subjectCode": class_subject.subject.code,
        "subject": class_subject.subject.name,
        "teacherId": str(item.teacher_membership_id),
        "periodsPerWeek": class_subject.periods_per_week,
        "version": _assignment_version(item),
        "startedAt": item.started_at.isoformat(),
        "endedAt": item.ended_at.isoformat() if item.ended_at else None,
    }


def _active_context_for_student(student: Student):
    enrollment = (
        student.enrollments.filter(status=EnrollmentStatus.ACTIVE)
        .order_by("-started_at", "-id")
        .first()
    )
    if enrollment is None:
        return None
    try:
        return enrollment.academic_context
    except EnrollmentAcademicContext.DoesNotExist:
        return None


def subject_eligibility_payload(student: Student) -> dict:
    """Canonical current subject eligibility for Student/Parent private payloads."""

    context = _active_context_for_student(student)
    if context is None:
        return {
            "sessionId": None,
            "classId": None,
            "eligibleSubjects": [],
            "availableElectives": [],
        }

    requirements = list(
        ClassSubject.objects.filter(
            session=context.session,
            academic_class=context.academic_class,
            is_active=True,
            subject__is_active=True,
        )
        .select_related("subject", "academic_class", "session")
        .order_by("subject__name")
    )
    selected_ids = set(
        context.subject_selections.values_list("class_subject_id", flat=True)
    )

    def item_payload(item: ClassSubject) -> dict:
        return {
            "classSubjectId": str(item.id),
            "subjectId": str(item.subject_id),
            "code": item.subject.code,
            "name": item.subject.name,
            "shortName": item.subject.short_name,
            "requirement": item.requirement,
            "periodsPerWeek": item.periods_per_week,
        }

    eligible = [
        item_payload(item)
        for item in requirements
        if item.requirement == CurriculumRequirement.COMPULSORY or item.id in selected_ids
    ]
    electives = [
        item_payload(item)
        for item in requirements
        if item.requirement == CurriculumRequirement.ELECTIVE and item.id not in selected_ids
    ]
    return {
        "sessionId": str(context.session_id),
        "classId": str(context.academic_class_id),
        "eligibleSubjects": eligible,
        "availableElectives": electives,
    }


def _refresh_students_for_class_subject(item: ClassSubject):
    # Lazy imports avoid an academics <-> students workspace import cycle.
    from apps.students.parent_sync import publish_parent_family_links_for_student
    from apps.students.student_sync import publish_student_class_link

    student_ids = EnrollmentAcademicContext.objects.filter(
        session=item.session,
        academic_class=item.academic_class,
        enrollment__status=EnrollmentStatus.ACTIVE,
    ).values_list("enrollment__student_id", flat=True)
    for student in Student.objects.filter(id__in=student_ids):
        publish_student_class_link(student)
        publish_parent_family_links_for_student(student)


def _teacher_assignment_payload(teacher: Membership) -> dict:
    assignments = (
        TeachingAssignment.objects.filter(
            teacher_membership=teacher,
            ended_at__isnull=True,
            class_subject__is_active=True,
        )
        .select_related(
            "class_subject__session",
            "class_subject__academic_class",
            "class_subject__subject",
        )
        .order_by(
            "class_subject__academic_class__level_order",
            "class_subject__subject__name",
        )
    )
    classes = []
    for assignment in assignments:
        requirement = assignment.class_subject
        active_term = requirement.session.terms.filter(
            status=AcademicLifecycleStatus.ACTIVE
        ).first()
        topics = []
        if active_term is not None:
            topics = [
                {
                    "id": str(topic.id),
                    "sequence": topic.sequence,
                    "title": topic.title,
                    "description": topic.description,
                }
                for topic in requirement.topics.filter(term=active_term).order_by(
                    "sequence", "title"
                )
            ]
        classes.append(
            {
                "className": requirement.academic_class.name,
                "subject": requirement.subject.name,
                "room": "",
                "time": "",
                "sessionId": str(requirement.session_id),
                "classId": str(requirement.academic_class_id),
                "subjectId": str(requirement.subject_id),
                "classSubjectId": str(requirement.id),
                "teachingAssignmentId": assignment.external_id,
                "periodsPerWeek": requirement.periods_per_week,
                "currentTermId": str(active_term.id) if active_term else "",
                "currentTerm": active_term.name if active_term else "",
                "topics": topics,
            }
        )
    return {
        "teacherMembershipId": str(teacher.id),
        "classes": classes,
    }


@transaction.atomic
def publish_teacher_assignment_link(teacher: Membership, *, actor=None):
    if teacher.role != Role.TEACHER or not teacher.is_active:
        return
    payload = _teacher_assignment_payload(teacher)
    entity_id = str(teacher.id)
    record = (
        SyncRecord.objects.select_for_update()
        .filter(
            school=teacher.school,
            entity_type=TEACHER_CLASS_LINK_ENTITY,
            entity_id=entity_id,
        )
        .first()
    )
    if record is None:
        SyncRecord.objects.create(
            school=teacher.school,
            entity_type=TEACHER_CLASS_LINK_ENTITY,
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


@transaction.atomic
def upsert_subject(*, membership: Membership, payload: dict) -> Subject:
    school = membership.school
    existing = Subject.objects.select_for_update().filter(
        school=school, id=payload["id"]
    ).first()
    if existing is not None and existing.class_subjects.exists():
        for label, old, new in (
            ("code", existing.code, payload["code"]),
            ("name", existing.name, payload["name"]),
            ("section", existing.section, payload["section"]),
        ):
            if old != new:
                raise Rejected(
                    f"Subject {label} cannot be rewritten after curriculum history exists."
                )
        if not payload["isActive"] and existing.class_subjects.filter(is_active=True).exists():
            raise Rejected("Remove this subject from active class curricula before deactivating it.")
    item, _ = Subject.objects.update_or_create(
        school=school,
        id=payload["id"],
        defaults={
            "code": payload["code"],
            "name": payload["name"],
            "short_name": payload["shortName"],
            "section": payload["section"],
            "is_active": payload["isActive"],
        },
    )
    return item


@transaction.atomic
def upsert_class_subject(*, membership: Membership, payload: dict) -> ClassSubject:
    school = membership.school
    session = _session(school, payload["sessionId"])
    academic_class = _academic_class(school, payload["classId"])
    subject = _subject(school, payload["subjectId"])
    _principal_secondary_scope(membership, academic_class)
    if session.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed academic session curriculum cannot be changed.")
    if not academic_class.is_active:
        raise Rejected("Curriculum can be assigned only to an active class.")
    if not subject.is_active:
        raise Rejected("Curriculum can use only an active subject.")
    if subject.section and subject.section.strip().lower() != academic_class.section.strip().lower():
        raise Rejected("The subject section does not match this academic class.")

    existing = ClassSubject.objects.select_for_update().filter(
        session=session,
        academic_class=academic_class,
        subject=subject,
    ).first()
    if existing is not None and not payload["isActive"]:
        if existing.teaching_assignments.filter(ended_at__isnull=True).exists():
            raise Rejected("End the active teaching assignment before removing this curriculum subject.")
        if existing.student_selections.exists():
            raise Rejected("An elective with recorded student selections cannot be removed from history.")

    item, _ = ClassSubject.objects.update_or_create(
        session=session,
        academic_class=academic_class,
        subject=subject,
        defaults={
            "requirement": payload["requirement"],
            "periods_per_week": payload["periodsPerWeek"],
            "is_active": payload["isActive"],
            "created_by": existing.created_by if existing else membership,
        },
    )
    _refresh_students_for_class_subject(item)
    return item


@transaction.atomic
def upsert_curriculum_topic(*, membership: Membership, payload: dict) -> CurriculumTopic:
    school = membership.school
    class_subject = _class_subject(school, payload["classSubjectId"])
    _principal_secondary_scope(membership, class_subject.academic_class)
    term = _term(school, payload["termId"])
    if term.session_id != class_subject.session_id:
        raise Rejected("Curriculum topic term must belong to the class-subject session.")
    if term.status == AcademicLifecycleStatus.CLOSED:
        existing = CurriculumTopic.objects.filter(id=payload["id"]).first()
        if existing is None:
            raise Rejected("A closed term cannot receive new curriculum topics.")
        if (
            existing.sequence != payload["sequence"]
            or existing.title != payload["title"]
            or existing.description != payload["description"]
        ):
            raise Rejected("A closed term curriculum is historical and cannot be rewritten.")
        return existing
    item, _ = CurriculumTopic.objects.update_or_create(
        id=payload["id"],
        defaults={
            "class_subject": class_subject,
            "term": term,
            "sequence": payload["sequence"],
            "title": payload["title"],
            "description": payload["description"],
        },
    )
    for assignment in class_subject.teaching_assignments.filter(ended_at__isnull=True):
        publish_teacher_assignment_link(assignment.teacher_membership, actor=membership)
    return item


@transaction.atomic
def upsert_teaching_assignment(*, membership: Membership, payload: dict) -> TeachingAssignment:
    school = membership.school
    class_subject = _class_subject(school, payload["classSubjectId"])
    _principal_secondary_scope(membership, class_subject.academic_class)
    if not class_subject.is_active:
        raise Rejected("Teaching can be assigned only to an active class-subject requirement.")
    if class_subject.session.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("Teaching assignments cannot be changed in a closed academic session.")
    teacher = _teacher_membership(school, payload["teacherId"])

    existing = TeachingAssignment.objects.select_for_update().filter(
        school=school, external_id=payload["id"]
    ).first()
    if existing is not None and existing.class_subject_id != class_subject.id:
        raise Rejected("An assignment cannot be moved to another class-subject. Create a new assignment.")

    conflicting = TeachingAssignment.objects.select_for_update().filter(
        class_subject=class_subject,
        ended_at__isnull=True,
    )
    if existing is not None:
        conflicting = conflicting.exclude(pk=existing.pk)
    if conflicting.exists():
        raise Rejected("This class-subject already has an active teacher assignment.")

    now = timezone.now()
    old_teacher = None
    if existing is None:
        item = TeachingAssignment.objects.create(
            school=school,
            external_id=payload["id"],
            class_subject=class_subject,
            teacher_membership=teacher,
            started_at=now,
            assigned_by=membership,
            handover_reason=payload.get("handoverReason", ""),
        )
    elif existing.teacher_membership_id == teacher.id:
        item = existing
    else:
        old_teacher = existing.teacher_membership
        archived = TeachingAssignment.objects.create(
            school=school,
            external_id=f"hist-{uuid.uuid4()}",
            class_subject=existing.class_subject,
            teacher_membership=existing.teacher_membership,
            started_at=existing.started_at,
            ended_at=now,
            assigned_by=existing.assigned_by,
            handover_reason=payload.get("handoverReason", ""),
            previous_assignment=existing.previous_assignment,
        )
        existing.teacher_membership = teacher
        existing.started_at = now
        existing.ended_at = None
        existing.assigned_by = membership
        existing.handover_reason = payload.get("handoverReason", "")
        existing.previous_assignment = archived
        existing.save(
            update_fields=[
                "teacher_membership",
                "started_at",
                "ended_at",
                "assigned_by",
                "handover_reason",
                "previous_assignment",
                "updated_at",
            ]
        )
        item = existing

    if old_teacher is not None:
        publish_teacher_assignment_link(old_teacher, actor=membership)
    publish_teacher_assignment_link(teacher, actor=membership)
    return item


@transaction.atomic
def set_student_elective(*, membership: Membership, payload: dict):
    school = membership.school
    student = Student.objects.select_for_update().filter(
        school=school, student_code=payload["studentId"]
    ).first()
    if student is None:
        raise Rejected("Student does not exist in this school.")
    context = _active_context_for_student(student)
    if context is None:
        raise Rejected("Student has no active academic enrollment context.")
    class_subject = _class_subject(school, payload["classSubjectId"])
    if (
        class_subject.session_id != context.session_id
        or class_subject.academic_class_id != context.academic_class_id
    ):
        raise Rejected("Elective does not belong to the student's current class and session.")
    if class_subject.requirement != CurriculumRequirement.ELECTIVE:
        raise Rejected("Only elective subjects require an individual student selection.")

    if payload["selected"]:
        StudentSubjectSelection.objects.get_or_create(
            enrollment_context=context,
            class_subject=class_subject,
            defaults={"selected_by": membership},
        )
    else:
        StudentSubjectSelection.objects.filter(
            enrollment_context=context, class_subject=class_subject
        ).delete()

    from apps.students.parent_sync import publish_parent_family_links_for_student
    from apps.students.student_sync import publish_student_class_link

    publish_student_class_link(student, actor=membership)
    publish_parent_family_links_for_student(student, actor=membership)
    return subject_eligibility_payload(student)
