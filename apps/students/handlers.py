from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import boolean, choice, text
from apps.sync.registry import EntityHandler, MutationContext

from .account_provisioning import provision_registration_accounts
from .models import (
    AdmissionDocumentStatus,
    AdmissionStage,
    RegistrationStatus,
    StudentRegistration,
)
from .services import (
    upsert_admission_from_sync,
    upsert_lifecycle_from_sync,
    upsert_registration_from_sync,
)


ADMISSION_ENTITY = "admission_applicant"
REGISTRATION_ENTITY = "student_registration"
LIFECYCLE_ENTITY = "administrator_student_lifecycle"

_ADMIN_ROLES = frozenset({"administrator"})
_ADMISSION_STAGES = [
    AdmissionStage.NEW,
    AdmissionStage.DOCUMENTS,
    AdmissionStage.SCREENING,
    AdmissionStage.OFFER,
    AdmissionStage.ACCEPTED,
    AdmissionStage.REGISTERED,
]
_STAGE_ORDER = {value: index for index, value in enumerate(_ADMISSION_STAGES)}
_DOCUMENT_STATUSES = set(AdmissionDocumentStatus.values)
_SECTIONS = {"Nursery", "Primary", "Secondary"}
_REGISTRATION_LABELS = {"Admission in progress", "Active"}
_LIFECYCLE_LABELS = {"Pending", "Completed", "Cancelled"}
_LIFECYCLE_WORKFLOWS = {
    "Class change",
    "Promotion",
    "Transfer out",
    "Alumni",
    "Withdrawal",
}


def _optional_text(payload: dict, key: str, *, max_len: int = 250) -> str:
    return text(payload, key, max_len=max_len, required=False)


class AdmissionApplicantHandler(EntityHandler):
    entity_type = ADMISSION_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return payload if membership.role in {"administrator", "proprietor"} else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        reference = text(p, "reference", max_len=128)
        if reference != ctx.entity_id:
            raise Rejected("reference must match the record id.")

        stage = choice(p.get("stage"), _ADMISSION_STAGES, "stage")
        if ctx.existing is not None:
            previous_stage = ctx.existing.get("stage", AdmissionStage.NEW)
            if _STAGE_ORDER.get(stage, -1) < _STAGE_ORDER.get(previous_stage, -1):
                raise Rejected("Admissions stages cannot move backward.")
            if ctx.existing.get("closedReason") and not p.get("closedReason"):
                raise Rejected(
                    "A closed application cannot be reopened by editing the sync record."
                )

        if stage == AdmissionStage.REGISTERED:
            has_active_registration = StudentRegistration.objects.filter(
                school=ctx.membership.school,
                source_applicant__reference=reference,
                status=RegistrationStatus.ACTIVE,
            ).exists()
            if not has_active_registration:
                raise Rejected(
                    "An applicant becomes Registered only after canonical student activation succeeds."
                )

        section = text(p, "section", max_len=80)
        if section not in _SECTIONS:
            raise Rejected("section is not a valid school section.")

        return {
            "reference": reference,
            "name": text(p, "name", max_len=240),
            "section": section,
            "className": text(p, "className", max_len=120),
            "guardian": text(p, "guardian", max_len=200),
            "phone": text(p, "phone", max_len=40),
            "stage": stage,
            "submitted": text(p, "submitted", max_len=40),
            "source": text(p, "source", max_len=80),
            "birthCertificate": choice(
                p.get("birthCertificate"), _DOCUMENT_STATUSES, "birthCertificate"
            ),
            "previousSchoolReport": choice(
                p.get("previousSchoolReport"),
                _DOCUMENT_STATUSES,
                "previousSchoolReport",
            ),
            "guardianId": choice(
                p.get("guardianId"), _DOCUMENT_STATUSES, "guardianId"
            ),
            "documentRequestQueued": boolean(p, "documentRequestQueued"),
            "closedReason": _optional_text(p, "closedReason"),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        upsert_admission_from_sync(membership=ctx.membership, payload=stored)


class StudentRegistrationHandler(EntityHandler):
    entity_type = REGISTRATION_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return (
            payload
            if membership.role in {"administrator", "proprietor", "principal"}
            else None
        )

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        registration_id = text(p, "registrationId", max_len=128)
        if registration_id != ctx.entity_id:
            raise Rejected("registrationId must match the record id.")

        status = choice(p.get("status"), _REGISTRATION_LABELS, "status")
        existing = ctx.existing
        if existing is not None:
            was_active = existing.get("status") == "Active"
            if was_active and status != "Active":
                raise Rejected(
                    "An active student registration cannot return to admission in progress."
                )
            for key in ("admissionNumber", "studentId", "sourceApplicantReference"):
                if existing.get(key, "") != p.get(key, ""):
                    raise Rejected(
                        "Permanent student identifiers and source application cannot be rewritten."
                    )
            if was_active:
                for key in ("academicSection", "proposedClass"):
                    if existing.get(key, "") != p.get(key, ""):
                        raise Rejected(
                            "Active placement cannot be rewritten here; use the student lifecycle workflow."
                        )

        cleaned = {
            "registrationId": registration_id,
            "firstName": text(p, "firstName", max_len=120),
            "surname": text(p, "surname", max_len=120),
            "otherName": _optional_text(p, "otherName", max_len=160),
            "dateOfBirth": _optional_text(p, "dateOfBirth", max_len=20),
            "gender": _optional_text(p, "gender", max_len=40),
            "academicSection": text(p, "academicSection", max_len=80),
            "proposedClass": text(p, "proposedClass", max_len=120),
            "previousSchool": _optional_text(p, "previousSchool", max_len=200),
            "address": _optional_text(p, "address", max_len=1000),
            "admissionNumber": _optional_text(p, "admissionNumber", max_len=80),
            "studentId": _optional_text(p, "studentId", max_len=80),
            "status": status,
            "primaryGuardian": text(p, "primaryGuardian", max_len=200),
            "relationship": _optional_text(p, "relationship", max_len=60),
            "guardianPhone": text(p, "guardianPhone", max_len=40),
            "guardianEmail": _optional_text(p, "guardianEmail", max_len=254),
            "familyAccount": _optional_text(p, "familyAccount", max_len=160),
            "siblingLink": _optional_text(p, "siblingLink", max_len=160),
            "birthCertificateStatus": _optional_text(
                p, "birthCertificateStatus", max_len=120
            ),
            "previousSchoolRecordStatus": _optional_text(
                p, "previousSchoolRecordStatus", max_len=120
            ),
            "guardianIdentificationStatus": _optional_text(
                p, "guardianIdentificationStatus", max_len=120
            ),
            "financeSetupStatus": _optional_text(
                p, "financeSetupStatus", max_len=120
            ),
            "transportMealStatus": _optional_text(
                p, "transportMealStatus", max_len=120
            ),
            "sourceApplicantReference": _optional_text(
                p, "sourceApplicantReference", max_len=128
            ),
        }

        if existing is not None:
            for key in ("admissionNumber", "studentId", "sourceApplicantReference"):
                cleaned[key] = existing.get(key, cleaned[key])
            if existing.get("status") == "Active":
                cleaned["academicSection"] = existing.get(
                    "academicSection", cleaned["academicSection"]
                )
                cleaned["proposedClass"] = existing.get(
                    "proposedClass", cleaned["proposedClass"]
                )
        return cleaned

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        registration = upsert_registration_from_sync(
            membership=ctx.membership,
            payload=stored,
        )
        if stored["status"] != "Active":
            return
        if registration.status != RegistrationStatus.ACTIVE or registration.student_id is None:
            raise Rejected("The student registration could not be activated.")

        student = registration.student
        guardian = student.guardians.filter(phone=registration.guardian_phone).first()
        if guardian is None:
            raise Rejected("The primary guardian could not be linked to the student account.")
        credentials = provision_registration_accounts(registration, student, guardian)

        # Enrich the same accepted sync version after canonical activation. This
        # direct update deliberately does not allocate another sync sequence: it
        # is server-owned output of this mutation, not a second mutation.
        from apps.sync.models import SyncRecord

        enriched = {
            **stored,
            "admissionNumber": registration.admission_number,
            "studentId": registration.student_code,
            "canonicalActive": True,
            "canonicalStudentId": str(registration.student_id),
            "credentialsProvisioned": True,
            **credentials,
        }
        SyncRecord.objects.filter(
            school=ctx.membership.school,
            entity_type=self.entity_type,
            entity_id=ctx.entity_id,
        ).update(payload=enriched)


class StudentLifecycleHandler(EntityHandler):
    entity_type = LIFECYCLE_ENTITY
    roles = _ADMIN_ROLES

    def visible(self, membership, payload):
        return (
            payload
            if membership.role in {"administrator", "proprietor", "principal"}
            else None
        )

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        external_id = text(p, "id", max_len=128)
        if external_id != ctx.entity_id:
            raise Rejected("id must match the lifecycle record id.")

        workflow = choice(p.get("workflow"), _LIFECYCLE_WORKFLOWS, "workflow")
        status = choice(p.get("status"), _LIFECYCLE_LABELS, "status")
        student_id = text(p, "studentId", max_len=80)
        existing = ctx.existing

        if existing is None and status != "Pending":
            raise Rejected("A lifecycle workflow must start as Pending.")
        if existing is not None:
            for key, value in (
                ("studentId", student_id),
                ("workflow", workflow),
                ("fromClass", p.get("fromClass", "")),
                ("toClass", p.get("toClass", "")),
            ):
                if existing.get(key, "") != value:
                    raise Rejected(
                        "The student, workflow and class path of a lifecycle event are immutable."
                    )
            if (
                existing.get("status") in {"Completed", "Cancelled"}
                and status != existing.get("status")
            ):
                raise Rejected("A completed or cancelled lifecycle event is final.")

        approved_by = _optional_text(p, "approvedBy", max_len=200)
        records_pack_ready = boolean(p, "recordsPackReady")
        to_class = _optional_text(p, "toClass", max_len=120)
        if status == "Completed":
            if workflow == "Promotion" and not approved_by:
                raise Rejected("A promotion requires the academic approver's name.")
            if workflow in {"Promotion", "Class change"} and not to_class:
                raise Rejected("This class movement requires a destination class.")
            if workflow == "Transfer out" and not records_pack_ready:
                raise Rejected(
                    "Prepare the records pack before completing the transfer."
                )

        requested_at = existing.get("requestedAt") if existing else ctx.now
        completed_at = ctx.now if status in {"Completed", "Cancelled"} else ""
        if existing is not None and existing.get("completedAt"):
            completed_at = existing["completedAt"]

        return {
            "id": external_id,
            "studentName": text(p, "studentName", max_len=240),
            "workflow": workflow,
            "change": _optional_text(p, "change", max_len=250),
            "status": status,
            "studentId": student_id,
            "fromClass": _optional_text(p, "fromClass", max_len=120),
            "toClass": to_class,
            "requestedAt": requested_at,
            "completedAt": completed_at,
            "approvedBy": approved_by,
            "recordsPackReady": records_pack_ready,
            "note": _optional_text(p, "note", max_len=1000),
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        upsert_lifecycle_from_sync(membership=ctx.membership, payload=stored)


HANDLERS = [
    AdmissionApplicantHandler(),
    StudentRegistrationHandler(),
    StudentLifecycleHandler(),
]
