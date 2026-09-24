import uuid
from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.core.errors import Rejected
from apps.students.models import (
    EnrollmentStatus,
    LifecycleStatus,
    Student,
    StudentEnrollment,
    StudentLifecycleEvent,
)
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.services import _apply_completed_lifecycle
from apps.students.student_sync import publish_student_class_link

from .models import (
    AcademicClass,
    AcademicLifecycleStatus,
    AcademicSession,
    AcademicTerm,
    EnrollmentAcademicContext,
    ProgressionBatch,
    ProgressionBatchStatus,
    ProgressionDecision,
    ProgressionOutcome,
)


def _date(value: str, field: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise Rejected(f"{field} must be a valid date.")
    return parsed


def _same_school(obj, school, label: str):
    if obj is None or obj.school_id != school.id:
        raise Rejected(f"{label} does not belong to this school.")
    return obj


def _session(school, value: str, label: str = "Academic session"):
    try:
        obj = AcademicSession.objects.get(id=value)
    except (AcademicSession.DoesNotExist, ValueError, TypeError):
        raise Rejected(f"{label} does not exist.")
    return _same_school(obj, school, label)


def _academic_class(school, value: str, label: str = "Class"):
    try:
        obj = AcademicClass.objects.get(id=value)
    except (AcademicClass.DoesNotExist, ValueError, TypeError):
        raise Rejected(f"{label} does not exist.")
    return _same_school(obj, school, label)


def _term(session, value: str | None):
    if not value:
        return None
    try:
        term = AcademicTerm.objects.select_related("session").get(id=value)
    except (AcademicTerm.DoesNotExist, ValueError, TypeError):
        raise Rejected("Academic term does not exist.")
    if term.session_id != session.id:
        raise Rejected("Academic term does not belong to this session.")
    return term


def _validate_range(starts_on: date, ends_on: date, label: str):
    if ends_on <= starts_on:
        raise Rejected(f"{label} end date must be after its start date.")


@transaction.atomic
def upsert_academic_session(*, membership, payload: dict) -> AcademicSession:
    school = membership.school
    existing = AcademicSession.objects.select_for_update().filter(
        school=school, id=payload["id"]
    ).first()
    starts_on = _date(payload["startsOn"], "startsOn")
    ends_on = _date(payload["endsOn"], "endsOn")
    _validate_range(starts_on, ends_on, "Academic session")
    status = payload["status"]
    if existing is not None and existing.status == AcademicLifecycleStatus.CLOSED:
        if status != AcademicLifecycleStatus.CLOSED:
            raise Rejected("A closed academic session cannot be reopened.")
    if status == AcademicLifecycleStatus.ACTIVE:
        conflict = AcademicSession.objects.filter(
            school=school, status=AcademicLifecycleStatus.ACTIVE
        )
        if existing is not None:
            conflict = conflict.exclude(pk=existing.pk)
        if conflict.exists():
            raise Rejected("Close the current academic session before activating another one.")

    obj, _ = AcademicSession.objects.update_or_create(
        school=school,
        id=payload["id"],
        defaults={
            "code": payload["code"],
            "name": payload["name"],
            "starts_on": starts_on,
            "ends_on": ends_on,
            "status": status,
            "created_by": existing.created_by if existing else membership,
        },
    )
    return obj


@transaction.atomic
def upsert_academic_term(*, membership, payload: dict) -> AcademicTerm:
    school = membership.school
    session = _session(school, payload["sessionId"])
    starts_on = _date(payload["startsOn"], "startsOn")
    ends_on = _date(payload["endsOn"], "endsOn")
    _validate_range(starts_on, ends_on, "Academic term")
    if starts_on < session.starts_on or ends_on > session.ends_on:
        raise Rejected("Academic term dates must fall within the academic session.")
    status = payload["status"]
    existing = AcademicTerm.objects.select_for_update().filter(
        session=session, id=payload["id"]
    ).first()
    if existing is not None and existing.status == AcademicLifecycleStatus.CLOSED:
        if status != AcademicLifecycleStatus.CLOSED:
            raise Rejected("A closed academic term cannot be reopened.")
    if status == AcademicLifecycleStatus.ACTIVE:
        conflict = AcademicTerm.objects.filter(
            session=session, status=AcademicLifecycleStatus.ACTIVE
        )
        if existing is not None:
            conflict = conflict.exclude(pk=existing.pk)
        if conflict.exists():
            raise Rejected("Close the current academic term before activating another one.")
        if session.status != AcademicLifecycleStatus.ACTIVE:
            raise Rejected("Only a term in the active academic session can be activated.")

    obj, _ = AcademicTerm.objects.update_or_create(
        session=session,
        id=payload["id"],
        defaults={
            "code": payload["code"],
            "name": payload["name"],
            "sequence": payload["sequence"],
            "starts_on": starts_on,
            "ends_on": ends_on,
            "status": status,
        },
    )
    return obj


@transaction.atomic
def upsert_academic_class(*, membership, payload: dict) -> AcademicClass:
    school = membership.school
    existing = AcademicClass.objects.select_for_update().filter(
        school=school, id=payload["id"]
    ).first()
    next_class_id = payload.get("nextClassId") or ""
    next_class = None
    if next_class_id:
        next_class = _academic_class(school, next_class_id, "Next class")
        if existing is not None and next_class.id == existing.id:
            raise Rejected("A class cannot progress to itself. Use Repeat for same-class progression.")
        if next_class.level_order <= payload["levelOrder"]:
            raise Rejected("Next class must be above the current class in the class order.")
    if payload["isTerminal"] and next_class is not None:
        raise Rejected("A terminal class cannot also have a next class.")

    obj, _ = AcademicClass.objects.update_or_create(
        school=school,
        id=payload["id"],
        defaults={
            "code": payload["code"],
            "name": payload["name"],
            "section": payload["section"],
            "level_order": payload["levelOrder"],
            "stream": payload["stream"],
            "next_class": next_class,
            "is_terminal": payload["isTerminal"],
            "is_active": payload["isActive"],
        },
    )
    return obj


def active_session_for_school(school):
    return AcademicSession.objects.filter(
        school=school, status=AcademicLifecycleStatus.ACTIVE
    ).first()


def active_term_for_session(session):
    if session is None:
        return None
    return session.terms.filter(status=AcademicLifecycleStatus.ACTIVE).first()


def match_class_for_enrollment(enrollment: StudentEnrollment):
    return AcademicClass.objects.filter(
        school=enrollment.school,
        is_active=True,
        name__iexact=enrollment.class_name.strip(),
    ).first()


@transaction.atomic
def attach_current_enrollment_context(enrollment: StudentEnrollment, *, source="automatic"):
    if enrollment.status != EnrollmentStatus.ACTIVE:
        return None
    existing = EnrollmentAcademicContext.objects.filter(enrollment=enrollment).first()
    if existing is not None:
        return existing
    session = active_session_for_school(enrollment.school)
    academic_class = match_class_for_enrollment(enrollment)
    if session is None or academic_class is None:
        return None
    return EnrollmentAcademicContext.objects.create(
        enrollment=enrollment,
        session=session,
        academic_class=academic_class,
        entry_term=active_term_for_session(session),
        source=source,
    )


def _serialize_term(term):
    if term is None:
        return None
    return {
        "id": str(term.id),
        "sessionId": str(term.session_id),
        "code": term.code,
        "name": term.name,
        "sequence": term.sequence,
        "startsOn": term.starts_on.isoformat(),
        "endsOn": term.ends_on.isoformat(),
        "status": term.status,
    }


def serialize_session(session):
    return {
        "id": str(session.id),
        "code": session.code,
        "name": session.name,
        "startsOn": session.starts_on.isoformat(),
        "endsOn": session.ends_on.isoformat(),
        "status": session.status,
    }


def serialize_class(academic_class):
    return {
        "id": str(academic_class.id),
        "code": academic_class.code,
        "name": academic_class.name,
        "section": academic_class.section,
        "levelOrder": academic_class.level_order,
        "stream": academic_class.stream,
        "nextClassId": str(academic_class.next_class_id) if academic_class.next_class_id else "",
        "isTerminal": academic_class.is_terminal,
        "isActive": academic_class.is_active,
    }


def academic_context_payload(enrollment: StudentEnrollment):
    try:
        context = enrollment.academic_context
    except EnrollmentAcademicContext.DoesNotExist:
        return None
    session = context.session
    term = context.entry_term
    return {
        "session": serialize_session(session),
        "entryTerm": _serialize_term(term),
        "academicClass": serialize_class(context.academic_class),
        "source": context.source,
    }


def _current_enrollment(student):
    return student.enrollments.filter(status=EnrollmentStatus.ACTIVE).order_by(
        "-started_at", "-id"
    ).first()


def _require_context(student, batch):
    enrollment = _current_enrollment(student)
    if enrollment is None:
        raise Rejected(f"{student.full_name} has no active enrollment.")
    try:
        context = enrollment.academic_context
    except EnrollmentAcademicContext.DoesNotExist:
        raise Rejected(
            f"{student.full_name} has no academic-session context. Run academic placement bootstrap first."
        )
    if context.session_id != batch.from_session_id:
        raise Rejected(f"{student.full_name} is not enrolled in the batch source session.")
    if context.academic_class_id != batch.source_class_id:
        raise Rejected(f"{student.full_name} is no longer in {batch.source_class.name}.")
    return enrollment, context


def _lifecycle_event(*, batch, student, workflow, from_class, to_class="", approved_by="", note="", records_pack_ready=False, status=LifecycleStatus.COMPLETED):
    external_id = f"bulk:{batch.external_id}:{student.student_code}"
    now = timezone.now()
    event, _ = StudentLifecycleEvent.objects.update_or_create(
        school=batch.school,
        external_id=external_id,
        defaults={
            "student": student,
            "workflow": workflow,
            "change": (
                f"{from_class} → {to_class}" if to_class else workflow
            ),
            "from_class": from_class,
            "to_class": to_class,
            "status": status,
            "requested_at": now,
            "completed_at": now if status == LifecycleStatus.COMPLETED else None,
            "approved_by": approved_by,
            "records_pack_ready": records_pack_ready,
            "note": note,
            "created_by": batch.created_by,
        },
    )
    return event


def _attach_new_context(student, session, target_class, source):
    enrollment = _current_enrollment(student)
    if enrollment is None:
        raise Rejected(f"{student.full_name} did not receive a new active enrollment.")
    context, _ = EnrollmentAcademicContext.objects.update_or_create(
        enrollment=enrollment,
        defaults={
            "session": session,
            "academic_class": target_class,
            "entry_term": active_term_for_session(session),
            "source": source,
        },
    )
    return context


def _refresh_private_links(student):
    publish_student_class_link(student)
    publish_parent_family_links_for_student(student)


@transaction.atomic
def upsert_progression_batch(*, membership, payload: dict) -> ProgressionBatch:
    school = membership.school
    from_session = _session(school, payload["fromSessionId"], "Source session")
    to_session = _session(school, payload["toSessionId"], "Destination session")
    source_class = _academic_class(school, payload["sourceClassId"], "Source class")
    if from_session.id == to_session.id:
        raise Rejected("Bulk progression requires a different destination session.")
    if to_session.starts_on <= from_session.starts_on:
        raise Rejected("Destination session must start after the source session.")

    existing = ProgressionBatch.objects.select_for_update().filter(
        school=school, external_id=payload["id"]
    ).first()
    if existing is not None and existing.status == ProgressionBatchStatus.APPLIED:
        if payload["status"] != ProgressionBatchStatus.APPLIED:
            raise Rejected("An applied progression batch is final.")
        return existing
    if existing is not None and existing.status == ProgressionBatchStatus.CANCELLED:
        if payload["status"] != ProgressionBatchStatus.CANCELLED:
            raise Rejected("A cancelled progression batch cannot be reopened.")

    batch, _ = ProgressionBatch.objects.update_or_create(
        school=school,
        external_id=payload["id"],
        defaults={
            "from_session": from_session,
            "to_session": to_session,
            "source_class": source_class,
            "status": payload["status"],
            "approved_by": payload["approvedBy"],
            "note": payload["note"],
            "created_by": existing.created_by if existing else membership,
        },
    )

    submitted_ids = set()
    for item in payload["decisions"]:
        student = Student.objects.filter(
            school=school, student_code=item["studentId"]
        ).first()
        if student is None:
            raise Rejected(f"Student {item['studentId']} does not exist in this school.")
        target = None
        if item["targetClassId"]:
            target = _academic_class(school, item["targetClassId"], "Target class")
        ProgressionDecision.objects.update_or_create(
            batch=batch,
            student=student,
            defaults={
                "outcome": item["outcome"],
                "target_class": target,
                "records_pack_ready": item["recordsPackReady"],
                "note": item["note"],
            },
        )
        submitted_ids.add(student.id)
    batch.decisions.exclude(student_id__in=submitted_ids).delete()

    if payload["status"] == ProgressionBatchStatus.APPLIED and (
        existing is None or existing.status != ProgressionBatchStatus.APPLIED
    ):
        apply_progression_batch(batch)
        batch.status = ProgressionBatchStatus.APPLIED
        batch.applied_at = timezone.now()
        batch.save(update_fields=["status", "applied_at", "updated_at"])
    return batch


@transaction.atomic
def apply_progression_batch(batch: ProgressionBatch):
    batch = ProgressionBatch.objects.select_for_update().select_related(
        "school", "from_session", "to_session", "source_class"
    ).get(pk=batch.pk)
    if batch.status == ProgressionBatchStatus.APPLIED and batch.applied_at:
        return batch
    if not batch.approved_by.strip():
        raise Rejected("Academic approver is required before applying a progression batch.")

    decisions = list(
        batch.decisions.select_related("student", "target_class").order_by(
            "student__surname", "student__first_name", "student__student_code"
        )
    )
    if not decisions:
        raise Rejected("The progression batch has no student decisions.")
    holds = [item for item in decisions if item.outcome == ProgressionOutcome.HOLD]
    if holds:
        raise Rejected("Resolve every Hold decision before applying the progression batch.")

    current_students = {
        context.enrollment.student_id
        for context in EnrollmentAcademicContext.objects.filter(
            session=batch.from_session,
            academic_class=batch.source_class,
            enrollment__status=EnrollmentStatus.ACTIVE,
        ).select_related("enrollment")
    }
    decided_students = {item.student_id for item in decisions}
    if current_students != decided_students:
        missing = current_students - decided_students
        extra = decided_students - current_students
        if missing:
            raise Rejected(
                "Every active student in the source class must have an explicit progression decision."
            )
        if extra:
            raise Rejected("The batch includes a student who is no longer in the source class.")

    for decision in decisions:
        student = Student.objects.select_for_update().get(pk=decision.student_id)
        enrollment, _ = _require_context(student, batch)
        source_name = batch.source_class.name

        if decision.outcome == ProgressionOutcome.PROMOTE:
            target = decision.target_class or batch.source_class.next_class
            if target is None:
                raise Rejected(f"Choose a promotion target for {student.full_name}.")
            if target.school_id != batch.school_id or not target.is_active:
                raise Rejected(f"Promotion target for {student.full_name} is not an active school class.")
            if target.level_order <= batch.source_class.level_order:
                raise Rejected(f"Promotion target for {student.full_name} must be above the source class.")
            event = _lifecycle_event(
                batch=batch,
                student=student,
                workflow="Promotion",
                from_class=source_name,
                to_class=target.name,
                approved_by=batch.approved_by,
                note=decision.note,
            )
            _apply_completed_lifecycle(event, now=timezone.now())
            _attach_new_context(
                student, batch.to_session, target, source=f"bulk:{batch.external_id}:promote"
            )
            _refresh_private_links(student)
            continue

        if decision.outcome == ProgressionOutcome.REPEAT:
            target = decision.target_class or batch.source_class
            if target.id != batch.source_class_id:
                raise Rejected(f"Repeat must keep {student.full_name} in the same class.")
            event = _lifecycle_event(
                batch=batch,
                student=student,
                workflow="Repeat",
                from_class=source_name,
                to_class=source_name,
                approved_by=batch.approved_by,
                note=decision.note,
            )
            _apply_completed_lifecycle(event, now=timezone.now())
            _attach_new_context(
                student, batch.to_session, batch.source_class, source=f"bulk:{batch.external_id}:repeat"
            )
            _refresh_private_links(student)
            continue

        if decision.outcome == ProgressionOutcome.GRADUATE:
            if not batch.source_class.is_terminal:
                raise Rejected(
                    f"{student.full_name} can only be graduated from a terminal class."
                )
            event = _lifecycle_event(
                batch=batch,
                student=student,
                workflow="Alumni",
                from_class=source_name,
                approved_by=batch.approved_by,
                note=decision.note,
            )
            _apply_completed_lifecycle(event, now=timezone.now())
            _refresh_private_links(student)
            continue

        if decision.outcome == ProgressionOutcome.TRANSFER_OUT:
            if not decision.records_pack_ready:
                raise Rejected(
                    f"Mark the records pack ready before transferring {student.full_name}."
                )
            event = _lifecycle_event(
                batch=batch,
                student=student,
                workflow="Transfer out",
                from_class=source_name,
                approved_by=batch.approved_by,
                note=decision.note,
                records_pack_ready=True,
            )
            _apply_completed_lifecycle(event, now=timezone.now())
            _refresh_private_links(student)
            continue

        raise Rejected(f"Unsupported progression outcome for {student.full_name}.")

    return batch


def serialize_progression_batch(batch: ProgressionBatch):
    return {
        "id": batch.external_id,
        "fromSessionId": str(batch.from_session_id),
        "toSessionId": str(batch.to_session_id),
        "sourceClassId": str(batch.source_class_id),
        "status": batch.status,
        "approvedBy": batch.approved_by,
        "note": batch.note,
        "appliedAt": batch.applied_at.isoformat() if batch.applied_at else None,
        "decisions": [
            {
                "studentId": item.student.student_code,
                "studentName": item.student.full_name,
                "outcome": item.outcome,
                "targetClassId": str(item.target_class_id) if item.target_class_id else "",
                "recordsPackReady": item.records_pack_ready,
                "note": item.note,
            }
            for item in batch.decisions.select_related("student", "target_class").all()
        ],
    }
