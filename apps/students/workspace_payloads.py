from .models import EnrollmentStatus, LifecycleStatus


_CLASS_PROGRESS_WORKFLOWS = {
    "Promotion",
    "Class change",
    "Transfer out",
    "Alumni",
    "Withdrawal",
}


def _iso(value):
    return value.isoformat() if value else None


def _enrollment_history(student) -> list[dict]:
    return [
        {
            "id": str(item.id),
            "academicSection": item.academic_section,
            "className": item.class_name,
            "status": item.status,
            "billable": item.is_billable,
            "startedAt": _iso(item.started_at),
            "endedAt": _iso(item.ended_at),
        }
        for item in student.enrollments.order_by("-started_at", "-id")
    ]


def _progression_history(student) -> list[dict]:
    return [
        {
            "id": item.external_id,
            "workflow": item.workflow,
            "change": item.change,
            "status": item.status,
            "fromClass": item.from_class,
            "toClass": item.to_class,
            "requestedAt": _iso(item.requested_at),
            "completedAt": _iso(item.completed_at),
            "approvedBy": item.approved_by,
        }
        for item in student.lifecycle_events.filter(
            workflow__in=_CLASS_PROGRESS_WORKFLOWS,
        ).order_by("-requested_at", "-id")
    ]


def _current_enrollment(student):
    return (
        student.enrollments.filter(status=EnrollmentStatus.ACTIVE)
        .order_by("-started_at", "-id")
        .first()
    )


def _primary_guardian(student):
    return (
        student.guardians.filter(is_primary=True)
        .order_by("created_at", "id")
        .first()
    )


def student_workspace_payload(student) -> dict:
    """Private canonical profile delivered only to this Student membership."""

    current = _current_enrollment(student)
    guardian = _primary_guardian(student)
    return {
        "canonicalStudentId": str(student.id),
        "studentId": student.student_code,
        "admissionNumber": student.admission_number,
        "name": student.full_name,
        "firstName": student.first_name,
        "surname": student.surname,
        "otherName": student.other_name,
        "dateOfBirth": _iso(student.date_of_birth),
        "gender": student.gender,
        "address": student.address,
        "status": student.status,
        "activatedAt": _iso(student.activated_at),
        "academicSection": current.academic_section if current else None,
        "className": current.class_name if current else None,
        "enrollmentActive": current is not None,
        "primaryGuardian": guardian.name if guardian else None,
        "guardianRelationship": guardian.relationship if guardian else None,
        "enrollmentHistory": _enrollment_history(student),
        "progressionHistory": _progression_history(student),
    }


def parent_child_workspace_payload(student) -> dict:
    """Canonical child summary safe for the linked Parent membership."""

    current = _current_enrollment(student)
    return {
        "canonicalStudentId": str(student.id),
        # Parent repositories historically key children by school Student ID.
        "id": student.student_code,
        "studentId": student.student_code,
        "admissionNumber": student.admission_number,
        "name": student.full_name,
        "status": student.status,
        "active": bool(current and current.status == EnrollmentStatus.ACTIVE),
        "academicSection": current.academic_section if current else None,
        "className": current.class_name if current else None,
        "enrollmentHistory": _enrollment_history(student),
        "progressionHistory": _progression_history(student),
    }
