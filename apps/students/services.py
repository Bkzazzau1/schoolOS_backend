import uuid
from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.core.errors import Rejected
from apps.schools.models import School

from .models import (
    AdmissionApplication,
    AdmissionStage,
    EnrollmentStatus,
    GuardianLink,
    LifecycleStatus,
    RegistrationStatus,
    SchoolRosterRevision,
    Student,
    StudentEnrollment,
    StudentLifecycleEvent,
    StudentRegistration,
    StudentStatus,
)


TERMINAL_STUDENT_STATUSES = {
    StudentStatus.TRANSFERRED_OUT,
    StudentStatus.GRADUATED,
    StudentStatus.WITHDRAWN,
    StudentStatus.INACTIVE,
}


def _clean_date(value: str | None):
    if not value:
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise Rejected("dateOfBirth must be a valid date.")
    return parsed


def _section_code(section: str) -> str:
    value = section.strip().lower()
    if "secondary" in value:
        return "SEC"
    if "primary" in value:
        return "PRI"
    return "EYR"


def _generated_admission_number(section: str) -> str:
    return f"SOS/{_section_code(section)}/{timezone.localdate():%y}/{uuid.uuid4().hex[:8].upper()}"


def _generated_student_code() -> str:
    return f"STU-{uuid.uuid4().hex[:12].upper()}"


def _registration_status(value: str) -> str:
    return RegistrationStatus.ACTIVE if value == "Active" else RegistrationStatus.IN_PROGRESS


def _lifecycle_status(value: str) -> str:
    return {
        "Completed": LifecycleStatus.COMPLETED,
        "Cancelled": LifecycleStatus.CANCELLED,
    }.get(value, LifecycleStatus.PENDING)


def billable_student_count(school: School) -> int:
    return StudentEnrollment.objects.filter(
        school=school,
        status=EnrollmentStatus.ACTIVE,
        is_billable=True,
    ).count()


@transaction.atomic
def publish_school_roster_meter(
    school: School,
    *,
    bump_revision: bool = False,
    observation_key: str | None = None,
    reason: str = "roster_observation",
):
    """Publish the canonical roster count to SaaS billing.

    A roster revision changes only when billable membership changes. A daily
    observation can reuse the same revision with a different observation key,
    giving every billing period a fresh authoritative meter even when nobody
    joined or left during that period.
    """

    SchoolRosterRevision.objects.get_or_create(school=school)
    revision = SchoolRosterRevision.objects.select_for_update().get(school=school)
    if bump_revision:
        revision.revision += 1
        revision.save(update_fields=["revision", "updated_at"])

    count = billable_student_count(school)
    if observation_key is None:
        observation_key = (
            f"change:{revision.revision}"
            if bump_revision
            else f"daily:{timezone.localdate().isoformat()}"
        )
    source_version = f"roster:{revision.revision}:{observation_key}"

    from apps.billing.cycle import publish_school_billing_meter

    return publish_school_billing_meter(
        school=school,
        billable_student_count=count,
        source="canonical_student_roster",
        source_version=source_version,
        authoritative=True,
        metadata={
            "rosterRevision": revision.revision,
            "reason": reason,
        },
    )


def bootstrap_school_roster_meter(school: School):
    return publish_school_roster_meter(
        school,
        observation_key="provision",
        reason="school_provisioned",
    )


def refresh_all_roster_meters() -> int:
    total = 0
    for school in School.objects.filter(is_active=True).iterator():
        publish_school_roster_meter(
            school,
            observation_key=f"daily:{timezone.localdate().isoformat()}",
            reason="daily_roster_refresh",
        )
        total += 1
    return total


@transaction.atomic
def upsert_admission_from_sync(*, membership, payload: dict) -> AdmissionApplication:
    application, _ = AdmissionApplication.objects.update_or_create(
        school=membership.school,
        reference=payload["reference"],
        defaults={
            "applicant_name": payload["name"],
            "section": payload["section"],
            "proposed_class": payload["className"],
            "guardian_name": payload["guardian"],
            "guardian_phone": payload["phone"],
            "stage": payload["stage"],
            "source": payload["source"],
            "submitted_label": payload["submitted"],
            "birth_certificate": payload["birthCertificate"],
            "previous_school_report": payload["previousSchoolReport"],
            "guardian_identification": payload["guardianId"],
            "document_request_queued": payload["documentRequestQueued"],
            "closed_reason": payload["closedReason"],
            "created_by": membership,
        },
    )
    return application


def _check_registration_identifiers(
    *,
    school: School,
    registration_id: str,
    admission_number: str,
    student_code: str,
    linked_student_id=None,
):
    duplicate_registration = StudentRegistration.objects.filter(
        school=school,
        admission_number=admission_number,
    ).exclude(registration_id=registration_id)
    if duplicate_registration.exists():
        raise Rejected("This admission number is already in use at this school.")
    duplicate_code = StudentRegistration.objects.filter(
        school=school,
        student_code=student_code,
    ).exclude(registration_id=registration_id)
    if duplicate_code.exists():
        raise Rejected("This student ID is already in use at this school.")

    students = Student.objects.filter(school=school)
    if linked_student_id:
        students = students.exclude(id=linked_student_id)
    if students.filter(admission_number=admission_number).exists():
        raise Rejected("This admission number belongs to another student.")
    if students.filter(student_code=student_code).exists():
        raise Rejected("This student ID belongs to another student.")


def _source_application(school: School, reference: str | None):
    if not reference:
        return None
    application = AdmissionApplication.objects.filter(
        school=school,
        reference=reference,
    ).first()
    if application is None:
        raise Rejected("The source admissions application does not exist on the server.")
    return application


@transaction.atomic
def upsert_registration_from_sync(*, membership, payload: dict) -> StudentRegistration:
    school = membership.school
    existing = StudentRegistration.objects.select_for_update().filter(
        school=school,
        registration_id=payload["registrationId"],
    ).first()

    source_application = _source_application(
        school,
        payload.get("sourceApplicantReference") or None,
    )
    requested_status = _registration_status(payload["status"])
    if requested_status == RegistrationStatus.ACTIVE and source_application is not None:
        if source_application.closed_reason:
            raise Rejected(
                f"This application is closed: {source_application.closed_reason}"
            )
        if source_application.stage not in {
            AdmissionStage.ACCEPTED,
            AdmissionStage.REGISTERED,
        }:
            raise Rejected("The admissions offer must be accepted before registration completes.")

    admission_number = (payload.get("admissionNumber") or "").strip()
    student_code = (payload.get("studentId") or "").strip()
    if existing is not None:
        admission_number = existing.admission_number
        student_code = existing.student_code
    else:
        admission_number = admission_number or _generated_admission_number(
            payload["academicSection"]
        )
        student_code = student_code or _generated_student_code()

    _check_registration_identifiers(
        school=school,
        registration_id=payload["registrationId"],
        admission_number=admission_number,
        student_code=student_code,
        linked_student_id=existing.student_id if existing else None,
    )

    defaults = {
        "source_applicant": source_application,
        "first_name": payload["firstName"],
        "surname": payload["surname"],
        "other_name": payload["otherName"],
        "date_of_birth": _clean_date(payload.get("dateOfBirth")),
        "gender": payload["gender"],
        "academic_section": payload["academicSection"],
        "proposed_class": payload["proposedClass"],
        "previous_school": payload["previousSchool"],
        "address": payload["address"],
        "admission_number": admission_number,
        "student_code": student_code,
        "status": requested_status,
        "primary_guardian": payload["primaryGuardian"],
        "relationship": payload["relationship"],
        "guardian_phone": payload["guardianPhone"],
        "guardian_email": payload["guardianEmail"],
        "family_account_ref": payload["familyAccount"],
        "sibling_link": payload["siblingLink"],
        "birth_certificate_status": payload["birthCertificateStatus"],
        "previous_school_record_status": payload["previousSchoolRecordStatus"],
        "guardian_identification_status": payload["guardianIdentificationStatus"],
        "finance_setup_status": payload["financeSetupStatus"],
        "transport_meal_status": payload["transportMealStatus"],
        "created_by": existing.created_by if existing else membership,
    }
    registration, _ = StudentRegistration.objects.update_or_create(
        school=school,
        registration_id=payload["registrationId"],
        defaults=defaults,
    )

    if requested_status == RegistrationStatus.ACTIVE:
        _activate_registration(registration)
    return registration


@transaction.atomic
def _activate_registration(registration: StudentRegistration) -> Student:
    now = timezone.now()
    student = registration.student
    new_billable_enrollment = False

    if student is None:
        student = Student.objects.create(
            school=registration.school,
            admission_number=registration.admission_number,
            student_code=registration.student_code,
            first_name=registration.first_name,
            surname=registration.surname,
            other_name=registration.other_name,
            date_of_birth=registration.date_of_birth,
            gender=registration.gender,
            previous_school=registration.previous_school,
            address=registration.address,
            status=StudentStatus.ACTIVE,
            activated_at=now,
        )
        registration.student = student
        registration.save(update_fields=["student", "updated_at"])
        StudentEnrollment.objects.create(
            school=registration.school,
            student=student,
            academic_section=registration.academic_section,
            class_name=registration.proposed_class,
            status=EnrollmentStatus.ACTIVE,
            is_billable=True,
            started_at=now,
            source_registration=registration,
        )
        new_billable_enrollment = True
    else:
        student.first_name = registration.first_name
        student.surname = registration.surname
        student.other_name = registration.other_name
        student.date_of_birth = registration.date_of_birth
        student.gender = registration.gender
        student.previous_school = registration.previous_school
        student.address = registration.address
        student.save(
            update_fields=[
                "first_name",
                "surname",
                "other_name",
                "date_of_birth",
                "gender",
                "previous_school",
                "address",
                "updated_at",
            ]
        )

    GuardianLink.objects.update_or_create(
        student=student,
        phone=registration.guardian_phone,
        defaults={
            "name": registration.primary_guardian,
            "relationship": registration.relationship,
            "email": registration.guardian_email,
            "is_primary": True,
            "family_account_ref": registration.family_account_ref,
            "sibling_link": registration.sibling_link,
        },
    )

    if registration.source_applicant_id:
        AdmissionApplication.objects.filter(id=registration.source_applicant_id).update(
            stage=AdmissionStage.REGISTERED,
            closed_reason="",
        )

    if new_billable_enrollment:
        publish_school_roster_meter(
            registration.school,
            bump_revision=True,
            reason="student_activated",
        )
    return student


def _active_enrollment(student: Student):
    return StudentEnrollment.objects.select_for_update().filter(
        student=student,
        status=EnrollmentStatus.ACTIVE,
    ).first()


def _complete_enrollment(
    enrollment: StudentEnrollment,
    *,
    status: str,
    ended_at,
):
    enrollment.status = status
    enrollment.is_billable = False
    enrollment.ended_at = ended_at
    enrollment.save(update_fields=["status", "is_billable", "ended_at"])


def _academic_section_for_class(class_name: str, fallback: str) -> str:
    value = class_name.strip().casefold()
    if any(token in value for token in ("jss", "sss", "secondary")):
        return "Secondary"
    if "primary" in value or value.startswith("pri"):
        return "Primary"
    if any(token in value for token in ("nursery", "early years", "kindergarten", "creche")):
        return "Early Years"
    return fallback


def _new_active_enrollment(*, student: Student, prior: StudentEnrollment, class_name: str, now):
    return StudentEnrollment.objects.create(
        school=student.school,
        student=student,
        academic_section=_academic_section_for_class(class_name, prior.academic_section),
        class_name=class_name,
        status=EnrollmentStatus.ACTIVE,
        is_billable=True,
        started_at=now,
    )


def _require_current_from_class(event: StudentLifecycleEvent, enrollment: StudentEnrollment) -> None:
    source = event.from_class.strip()
    if source and source.casefold() != enrollment.class_name.strip().casefold():
        raise Rejected(
            "This progression decision is stale because the student's current class has changed. Cancel it and start a new lifecycle decision from the current class."
        )


@transaction.atomic
def upsert_lifecycle_from_sync(*, membership, payload: dict) -> StudentLifecycleEvent:
    school = membership.school
    student_code = payload["studentId"]
    student = Student.objects.select_for_update().filter(
        school=school,
        student_code=student_code,
    ).first()
    if student is None:
        raise Rejected("This lifecycle change does not match a canonical student record.")

    existing = StudentLifecycleEvent.objects.select_for_update().filter(
        school=school,
        external_id=payload["id"],
    ).first()
    previous_status = existing.status if existing else None
    status = _lifecycle_status(payload["status"])
    now = timezone.now()
    requested_at = existing.requested_at if existing else now

    event, _ = StudentLifecycleEvent.objects.update_or_create(
        school=school,
        external_id=payload["id"],
        defaults={
            "student": student,
            "workflow": payload["workflow"],
            "change": payload["change"],
            "from_class": payload["fromClass"],
            "to_class": payload["toClass"],
            "status": status,
            "requested_at": requested_at,
            "completed_at": now if status in {LifecycleStatus.COMPLETED, LifecycleStatus.CANCELLED} else None,
            "approved_by": payload["approvedBy"],
            "records_pack_ready": payload["recordsPackReady"],
            "note": payload["note"],
            "created_by": existing.created_by if existing else membership,
        },
    )

    if event.workflow == "Transfer out" and status == LifecycleStatus.PENDING:
        if student.status == StudentStatus.ACTIVE:
            student.status = StudentStatus.TRANSFER_PENDING
            student.save(update_fields=["status", "updated_at"])
    elif event.workflow == "Transfer out" and status == LifecycleStatus.CANCELLED:
        if student.status == StudentStatus.TRANSFER_PENDING:
            student.status = StudentStatus.ACTIVE
            student.save(update_fields=["status", "updated_at"])

    if status == LifecycleStatus.COMPLETED and previous_status != LifecycleStatus.COMPLETED:
        _apply_completed_lifecycle(event, now=now)
    return event


@transaction.atomic
def _apply_completed_lifecycle(event: StudentLifecycleEvent, *, now):
    student = Student.objects.select_for_update().get(pk=event.student_id)
    enrollment = _active_enrollment(student)
    if enrollment is None:
        raise Rejected("This student has no active enrollment to change.")

    if event.workflow in {"Promotion", "Repeat", "Class change"}:
        _require_current_from_class(event, enrollment)

    if event.workflow == "Promotion":
        if not event.approved_by.strip():
            raise Rejected("A promotion requires the academic approver's name.")
        destination = event.to_class.strip()
        if not destination:
            raise Rejected("A promotion requires the destination class.")
        if destination.casefold() == enrollment.class_name.strip().casefold():
            raise Rejected(
                "Promotion must move to a different class. Use Repeat when the student remains in the same class."
            )
        _complete_enrollment(enrollment, status=EnrollmentStatus.COMPLETED, ended_at=now)
        _new_active_enrollment(
            student=student,
            prior=enrollment,
            class_name=destination,
            now=now,
        )
        if student.status == StudentStatus.TRANSFER_PENDING:
            student.status = StudentStatus.ACTIVE
            student.save(update_fields=["status", "updated_at"])
        return

    if event.workflow == "Repeat":
        if not event.approved_by.strip():
            raise Rejected("A repeat decision requires the academic approver's name.")
        requested_class = event.to_class.strip() or enrollment.class_name.strip()
        if requested_class.casefold() != enrollment.class_name.strip().casefold():
            raise Rejected(
                "Repeat must keep the student in the same class. Use Promotion or Class change for a different class."
            )
        _complete_enrollment(enrollment, status=EnrollmentStatus.COMPLETED, ended_at=now)
        _new_active_enrollment(
            student=student,
            prior=enrollment,
            class_name=enrollment.class_name,
            now=now,
        )
        if student.status == StudentStatus.TRANSFER_PENDING:
            student.status = StudentStatus.ACTIVE
            student.save(update_fields=["status", "updated_at"])
        return

    if event.workflow == "Class change":
        destination = event.to_class.strip()
        if not destination:
            raise Rejected("A class change requires the destination class.")
        if destination.casefold() == enrollment.class_name.strip().casefold():
            raise Rejected(
                "The student is already in that class. Use Repeat only for an academic repeat decision."
            )
        _complete_enrollment(enrollment, status=EnrollmentStatus.COMPLETED, ended_at=now)
        _new_active_enrollment(
            student=student,
            prior=enrollment,
            class_name=destination,
            now=now,
        )
        return

    if event.workflow == "Transfer out":
        if not event.records_pack_ready:
            raise Rejected("Prepare the records pack before completing the transfer.")
        _complete_enrollment(enrollment, status=EnrollmentStatus.TRANSFERRED_OUT, ended_at=now)
        student.status = StudentStatus.TRANSFERRED_OUT
        student.save(update_fields=["status", "updated_at"])
        publish_school_roster_meter(
            student.school,
            bump_revision=True,
            reason="student_transferred_out",
        )
        return

    if event.workflow == "Alumni":
        _complete_enrollment(enrollment, status=EnrollmentStatus.GRADUATED, ended_at=now)
        student.status = StudentStatus.GRADUATED
        student.save(update_fields=["status", "updated_at"])
        publish_school_roster_meter(
            student.school,
            bump_revision=True,
            reason="student_graduated",
        )
        return

    if event.workflow == "Withdrawal":
        _complete_enrollment(enrollment, status=EnrollmentStatus.WITHDRAWN, ended_at=now)
        student.status = StudentStatus.WITHDRAWN
        student.save(update_fields=["status", "updated_at"])
        publish_school_roster_meter(
            student.school,
            bump_revision=True,
            reason="student_withdrawn",
        )
        return

    raise Rejected("This lifecycle workflow is not supported by the canonical roster.")


def current_enrollment(student: Student):
    return student.enrollments.filter(status=EnrollmentStatus.ACTIVE).order_by("-started_at").first()


def primary_guardian(student: Student):
    return student.guardians.filter(is_primary=True).order_by("created_at").first()


def serialize_student(student: Student) -> dict:
    enrollment = current_enrollment(student)
    guardian = primary_guardian(student)
    return {
        "id": str(student.id),
        "studentId": student.student_code,
        "admissionNumber": student.admission_number,
        "name": student.full_name,
        "firstName": student.first_name,
        "surname": student.surname,
        "otherName": student.other_name,
        "dateOfBirth": student.date_of_birth.isoformat() if student.date_of_birth else None,
        "gender": student.gender,
        "status": student.status,
        "academicSection": enrollment.academic_section if enrollment else None,
        "className": enrollment.class_name if enrollment else None,
        "primaryGuardian": guardian.name if guardian else None,
        "guardianPhone": guardian.phone if guardian else None,
        "billable": bool(enrollment and enrollment.is_billable),
        "activatedAt": student.activated_at.isoformat() if student.activated_at else None,
    }


def serialize_admission(application: AdmissionApplication) -> dict:
    return {
        "id": str(application.id),
        "reference": application.reference,
        "name": application.applicant_name,
        "section": application.section,
        "className": application.proposed_class,
        "guardian": application.guardian_name,
        "phone": application.guardian_phone,
        "stage": application.stage,
        "source": application.source,
        "submitted": application.submitted_label,
        "birthCertificate": application.birth_certificate,
        "previousSchoolReport": application.previous_school_report,
        "guardianId": application.guardian_identification,
        "documentRequestQueued": application.document_request_queued,
        "closedReason": application.closed_reason,
    }


def serialize_roster_summary(school: School) -> dict:
    revision = SchoolRosterRevision.objects.filter(school=school).first()
    return {
        "schoolId": str(school.id),
        "billableStudents": billable_student_count(school),
        "rosterRevision": revision.revision if revision else 0,
        "capturedAt": timezone.now().isoformat(),
    }
