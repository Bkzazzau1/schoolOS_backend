from django.db import transaction

from apps.core.errors import Rejected
from apps.schools.models import Membership, Role
from apps.staff.constants import DIRECTORY as STAFF_DIRECTORY, PROFILE as STAFF_PROFILE
from apps.students.models import EnrollmentStatus, Student
from apps.sync.models import SyncRecord

from .curriculum_models import (
    ClassSubject,
    StudentSubjectSelection,
    Subject,
    SubjectRequirement,
    TeachingAssignment,
    TeachingAssignmentEvent,
)
from .models import (
    AcademicClass,
    AcademicLifecycleStatus,
    AcademicSession,
    EnrollmentAcademicContext,
)


TEACHER_CLASS_LINK_ENTITY = "teacher_class_assignment"


def _same_school(obj, school, label):
    candidate_school_id = getattr(obj, "school_id", None)
    if candidate_school_id is None and hasattr(obj, "session"):
        candidate_school_id = obj.session.school_id
    if candidate_school_id != school.id:
        raise Rejected(f"{label} does not belong to this school.")
    return obj


def _subject(school, value):
    try:
        obj = Subject.objects.get(id=value)
    except (Subject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Subject does not exist.")
    return _same_school(obj, school, "Subject")


def _session(school, value):
    try:
        obj = AcademicSession.objects.get(id=value)
    except (AcademicSession.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic session does not exist.")
    return _same_school(obj, school, "Academic session")


def _academic_class(school, value):
    try:
        obj = AcademicClass.objects.get(id=value)
    except (AcademicClass.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic class does not exist.")
    return _same_school(obj, school, "Academic class")


def _class_subject(school, value):
    try:
        obj = ClassSubject.objects.select_related(
            "session", "academic_class", "subject"
        ).get(id=value)
    except (ClassSubject.DoesNotExist, ValueError, TypeError):
        raise Rejected("Class subject does not exist.")
    if obj.session.school_id != school.id:
        raise Rejected("Class subject does not belong to this school.")
    return obj


def serialize_subject(subject):
    return {
        "id": str(subject.id),
        "code": subject.code,
        "name": subject.name,
        "description": subject.description,
        "isActive": subject.is_active,
    }


def serialize_class_subject(offering):
    return {
        "id": str(offering.id),
        "sessionId": str(offering.session_id),
        "classId": str(offering.academic_class_id),
        "className": offering.academic_class.name,
        "subjectId": str(offering.subject_id),
        "subjectCode": offering.subject.code,
        "subjectName": offering.subject.name,
        "requirement": offering.requirement,
        "periodsPerWeek": offering.periods_per_week,
        "isActive": offering.is_active,
    }


def serialize_teaching_assignment(assignment):
    offering = assignment.class_subject
    return {
        "id": str(assignment.id),
        "classSubjectId": str(offering.id),
        "sessionId": str(offering.session_id),
        "classId": str(offering.academic_class_id),
        "className": offering.academic_class.name,
        "subjectId": str(offering.subject_id),
        "subject": offering.subject.name,
        "teacherId": assignment.teacher_staff_id,
        "teacherStaffId": assignment.teacher_staff_id,
        "teacherMembershipId": str(assignment.teacher_membership_id)
        if assignment.teacher_membership_id
        else "",
        "periodsPerWeek": offering.periods_per_week,
        "isActive": assignment.is_active,
        "accessReady": assignment.teacher_membership_id is not None,
    }


def _staff_profile(school, staff_id):
    directory = SyncRecord.objects.filter(
        school=school,
        entity_type=STAFF_DIRECTORY,
        entity_id=staff_id,
        deleted=False,
    ).first()
    if directory is None:
        raise Rejected("Choose a staff member in this school's staff directory.")
    profile = SyncRecord.objects.filter(
        school=school,
        entity_type=STAFF_PROFILE,
        entity_id=staff_id,
        deleted=False,
    ).first()
    return profile


def resolve_teacher_membership(school, staff_id):
    profile = _staff_profile(school, staff_id)
    if profile is None:
        return None
    linked = str(profile.payload.get("linkedMembershipId") or "").strip()
    if not linked:
        return None
    try:
        membership = Membership.objects.get(
            id=linked,
            school=school,
            role=Role.TEACHER,
            is_active=True,
        )
    except (Membership.DoesNotExist, ValueError, TypeError):
        raise Rejected(
            "The linked staff account is not an active Teacher membership. Correct staff onboarding before assigning teaching access."
        )
    return membership


@transaction.atomic
def upsert_subject(*, membership, payload):
    school = membership.school
    existing = Subject.objects.select_for_update().filter(
        school=school, id=payload["id"]
    ).first()
    if existing is not None and existing.class_offerings.exists():
        if existing.code != payload["code"] or existing.name != payload["name"]:
            raise Rejected(
                "Subject code/name cannot be rewritten after the subject has curriculum history."
            )
        if not payload["isActive"] and existing.class_offerings.filter(is_active=True).exists():
            raise Rejected("Remove the subject from active class curricula before deactivating it.")
    obj, _ = Subject.objects.update_or_create(
        school=school,
        id=payload["id"],
        defaults={
            "code": payload["code"],
            "name": payload["name"],
            "description": payload["description"],
            "is_active": payload["isActive"],
        },
    )
    return obj


def _refresh_students_for_offering(offering):
    from apps.students.parent_sync import publish_parent_family_links_for_student
    from apps.students.student_sync import publish_student_class_link

    student_ids = EnrollmentAcademicContext.objects.filter(
        session=offering.session,
        academic_class=offering.academic_class,
        enrollment__status=EnrollmentStatus.ACTIVE,
    ).values_list("enrollment__student_id", flat=True)
    for student in Student.objects.filter(id__in=student_ids):
        publish_student_class_link(student)
        publish_parent_family_links_for_student(student)


@transaction.atomic
def upsert_class_subject(*, membership, payload):
    school = membership.school
    session = _session(school, payload["sessionId"])
    academic_class = _academic_class(school, payload["classId"])
    subject = _subject(school, payload["subjectId"])
    if session.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed academic session's curriculum cannot be changed.")
    if not subject.is_active and payload["isActive"]:
        raise Rejected("An inactive subject cannot be added to an active curriculum.")
    periods = payload["periodsPerWeek"]
    if periods < 1 or periods > 30:
        raise Rejected("Periods per week must be between 1 and 30.")
    existing = ClassSubject.objects.select_for_update().filter(
        session=session,
        id=payload["id"],
    ).first()
    if existing is not None:
        if existing.academic_class_id != academic_class.id or existing.subject_id != subject.id:
            raise Rejected("Class and subject are immutable for an existing curriculum record.")
        if not payload["isActive"] and hasattr(existing, "teaching_assignment"):
            if existing.teaching_assignment.is_active:
                raise Rejected("Deactivate or transfer the teaching responsibility before removing this class subject.")
    obj, _ = ClassSubject.objects.update_or_create(
        session=session,
        id=payload["id"],
        defaults={
            "academic_class": academic_class,
            "subject": subject,
            "requirement": payload["requirement"],
            "periods_per_week": periods,
            "is_active": payload["isActive"],
        },
    )
    _refresh_students_for_offering(obj)
    if hasattr(obj, "teaching_assignment") and obj.teaching_assignment.teacher_membership_id:
        publish_teacher_class_assignment(obj.teaching_assignment.teacher_membership)
    return obj


def eligible_subjects_for_student(student):
    enrollment = student.enrollments.filter(status=EnrollmentStatus.ACTIVE).order_by(
        "-started_at", "-id"
    ).first()
    if enrollment is None:
        return []
    try:
        context = enrollment.academic_context
    except EnrollmentAcademicContext.DoesNotExist:
        return []
    offerings = list(
        ClassSubject.objects.filter(
            session=context.session,
            academic_class=context.academic_class,
            is_active=True,
            subject__is_active=True,
        ).select_related("subject")
    )
    selected_ids = set(
        StudentSubjectSelection.objects.filter(
            student=student,
            class_subject__in=offerings,
            selected=True,
        ).values_list("class_subject_id", flat=True)
    )
    values = []
    for offering in offerings:
        eligible = (
            offering.requirement == SubjectRequirement.COMPULSORY
            or offering.id in selected_ids
        )
        if not eligible:
            continue
        values.append(
            {
                "classSubjectId": str(offering.id),
                "subjectId": str(offering.subject_id),
                "code": offering.subject.code,
                "name": offering.subject.name,
                "requirement": offering.requirement,
                "periodsPerWeek": offering.periods_per_week,
            }
        )
    values.sort(key=lambda item: item["name"].lower())
    return values


@transaction.atomic
def upsert_student_subject_selection(*, membership, payload):
    school = membership.school
    offering = _class_subject(school, payload["classSubjectId"])
    if offering.requirement != SubjectRequirement.ELECTIVE:
        raise Rejected("Compulsory subjects are automatic and cannot be individually selected.")
    if not offering.is_active:
        raise Rejected("An inactive elective cannot be selected.")
    student = Student.objects.select_for_update().filter(
        school=school, student_code=payload["studentId"]
    ).first()
    if student is None:
        raise Rejected("Student does not exist in this school.")
    active = student.enrollments.filter(status=EnrollmentStatus.ACTIVE).order_by(
        "-started_at", "-id"
    ).first()
    if active is None:
        raise Rejected("Student has no active enrollment.")
    try:
        context = active.academic_context
    except EnrollmentAcademicContext.DoesNotExist:
        raise Rejected("Student has no canonical academic-session context.")
    if context.session_id != offering.session_id or context.academic_class_id != offering.academic_class_id:
        raise Rejected("This elective does not belong to the student's current class/session.")
    selection, _ = StudentSubjectSelection.objects.update_or_create(
        student=student,
        class_subject=offering,
        defaults={"selected": payload["selected"], "selected_by": membership},
    )
    _refresh_students_for_offering(offering)
    return selection


def _teacher_payload(assignment):
    offering = assignment.class_subject
    return {
        "classSubjectId": str(offering.id),
        "sessionId": str(offering.session_id),
        "classId": str(offering.academic_class_id),
        "className": offering.academic_class.name,
        "subjectId": str(offering.subject_id),
        "subject": offering.subject.name,
        "periodsPerWeek": offering.periods_per_week,
        "room": "",
        "time": "",
    }


@transaction.atomic
def publish_teacher_class_assignment(membership):
    if membership is None or membership.role != Role.TEACHER:
        return
    assignments = TeachingAssignment.objects.filter(
        teacher_membership=membership,
        is_active=True,
        class_subject__is_active=True,
        class_subject__session__status=AcademicLifecycleStatus.ACTIVE,
    ).select_related(
        "class_subject__session",
        "class_subject__academic_class",
        "class_subject__subject",
    )
    payload = {
        "teacherMembershipId": str(membership.id),
        "classes": [_teacher_payload(item) for item in assignments],
    }
    record = SyncRecord.objects.select_for_update().filter(
        school=membership.school,
        entity_type=TEACHER_CLASS_LINK_ENTITY,
        entity_id=str(membership.id),
    ).first()
    if record is None:
        SyncRecord.objects.create(
            school=membership.school,
            entity_type=TEACHER_CLASS_LINK_ENTITY,
            entity_id=str(membership.id),
            payload=payload,
            version=1,
            deleted=False,
        )
        return
    if record.payload == payload and not record.deleted:
        return
    record.payload = payload
    record.deleted = False
    record.version += 1
    record.save(update_fields=["payload", "deleted", "version"])


@transaction.atomic
def upsert_teaching_assignment(*, membership, payload):
    school = membership.school
    offering = _class_subject(school, payload["classSubjectId"])
    if offering.academic_class.section.strip().lower() != "secondary":
        raise Rejected(
            "Principal teaching assignments are limited to the Secondary section."
        )
    if not offering.is_active:
        raise Rejected("Teaching responsibility cannot be assigned to an inactive class subject.")
    if offering.session.status == AcademicLifecycleStatus.CLOSED:
        raise Rejected("A closed session's teaching assignments are historical and cannot be changed.")
    teacher_staff_id = payload["teacherStaffId"]
    teacher_membership = resolve_teacher_membership(school, teacher_staff_id)
    existing = TeachingAssignment.objects.select_for_update().filter(
        class_subject=offering
    ).first()
    old_membership = existing.teacher_membership if existing else None
    old_staff_id = existing.teacher_staff_id if existing else ""
    changed_teacher = existing is not None and old_staff_id != teacher_staff_id
    if changed_teacher and not payload["transferReason"].strip():
        raise Rejected("A teacher reassignment requires a transfer reason.")

    if existing is None:
        assignment = TeachingAssignment.objects.create(
            class_subject=offering,
            teacher_staff_id=teacher_staff_id,
            teacher_membership=teacher_membership,
            assigned_by=membership,
            is_active=payload["isActive"],
        )
        TeachingAssignmentEvent.objects.create(
            assignment=assignment,
            from_teacher_staff_id="",
            to_teacher_staff_id=teacher_staff_id,
            reason=payload["transferReason"].strip(),
            actor=membership,
        )
    else:
        assignment = existing
        assignment.teacher_staff_id = teacher_staff_id
        assignment.teacher_membership = teacher_membership
        assignment.is_active = payload["isActive"]
        assignment.assigned_by = membership
        assignment.save(
            update_fields=[
                "teacher_staff_id",
                "teacher_membership",
                "is_active",
                "assigned_by",
                "updated_at",
            ]
        )
        if changed_teacher:
            TeachingAssignmentEvent.objects.create(
                assignment=assignment,
                from_teacher_staff_id=old_staff_id,
                to_teacher_staff_id=teacher_staff_id,
                reason=payload["transferReason"].strip(),
                actor=membership,
            )

    if old_membership is not None and (
        teacher_membership is None or old_membership.id != teacher_membership.id
    ):
        publish_teacher_class_assignment(old_membership)
    if teacher_membership is not None:
        publish_teacher_class_assignment(teacher_membership)
    return assignment


def refresh_teacher_links_for_school(school):
    memberships = Membership.objects.filter(
        school=school, role=Role.TEACHER, is_active=True
    )
    for membership in memberships:
        publish_teacher_class_assignment(membership)
