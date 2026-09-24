import uuid

from django.core.management.base import BaseCommand

from apps.academics.curriculum_services import (
    serialize_teaching_assignment,
    upsert_teaching_assignment,
)
from apps.academics.models import (
    AcademicLifecycleStatus,
    ClassSubject,
    TeachingAssignment,
)
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord


class Command(BaseCommand):
    help = (
        "Materialize legacy Principal teaching-assignment sync records into the "
        "canonical curriculum/Teacher-membership model. Safe to run repeatedly."
    )

    def handle(self, *args, **options):
        materialized = 0
        confirmed = 0
        skipped = 0
        for record in (
            SyncRecord.objects.filter(
                entity_type="principal_teaching_assignment",
                deleted=False,
            )
            .select_related("school")
            .iterator()
        ):
            school = record.school
            existing = TeachingAssignment.objects.filter(
                school=school,
                external_id=record.entity_id,
            ).first()
            if existing is not None:
                canonical = TeachingAssignment.objects.select_related(
                    "class_subject__session",
                    "class_subject__academic_class",
                    "class_subject__subject",
                    "teacher_membership",
                    "previous_assignment",
                ).get(pk=existing.pk)
                self._replace_payload(
                    record,
                    serialize_teaching_assignment(canonical),
                )
                confirmed += 1
                continue

            actor = (
                Membership.objects.filter(
                    school=school,
                    role=Role.PRINCIPAL,
                    is_active=True,
                ).first()
                or Membership.objects.filter(
                    school=school,
                    role=Role.PROPRIETOR,
                    is_active=True,
                ).first()
            )
            if actor is None:
                skipped += 1
                self.stderr.write(
                    f"SKIP {record.school_id} / {record.entity_id}: "
                    "no active Principal/Proprietor actor"
                )
                continue

            try:
                class_subject = self._class_subject(record)
            except ValueError as exc:
                skipped += 1
                self.stderr.write(
                    f"SKIP {record.school_id} / {record.entity_id}: {exc}"
                )
                continue

            teacher = self._teacher(record)
            if class_subject is None:
                skipped += 1
                self.stderr.write(
                    f"SKIP {record.school_id} / {record.entity_id}: "
                    "canonical curriculum requirement is unresolved"
                )
                continue
            if teacher is None:
                skipped += 1
                self.stderr.write(
                    f"SKIP {record.school_id} / {record.entity_id}: "
                    "linked active Teacher membership is unresolved"
                )
                continue
            if len(record.entity_id) > 64:
                skipped += 1
                self.stderr.write(
                    f"SKIP {record.school_id} / {record.entity_id}: "
                    "assignment id exceeds canonical length"
                )
                continue

            assignment = upsert_teaching_assignment(
                membership=actor,
                payload={
                    "id": record.entity_id,
                    "classSubjectId": str(class_subject.id),
                    "teacherId": str(teacher.id),
                    "periodsPerWeek": class_subject.periods_per_week,
                    "handoverReason": "Legacy SchoolOS assignment materialization",
                },
            )
            assignment = TeachingAssignment.objects.select_related(
                "class_subject__session",
                "class_subject__academic_class",
                "class_subject__subject",
                "teacher_membership",
                "previous_assignment",
            ).get(pk=assignment.pk)
            self._replace_payload(
                record,
                serialize_teaching_assignment(assignment),
            )
            materialized += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Teaching assignment bootstrap complete: "
                f"{materialized} materialized, {confirmed} confirmed, "
                f"{skipped} skipped."
            )
        )

    def _class_subject(self, record):
        payload = record.payload
        explicit = _uuid_or_none(payload.get("classSubjectId"))
        if explicit is not None:
            item = ClassSubject.objects.filter(
                id=explicit,
                session__school=record.school,
                is_active=True,
            ).first()
            if item is not None:
                return item

        class_name = (payload.get("className") or "").strip()
        subject_name = (payload.get("subject") or "").strip()
        if not class_name or not subject_name:
            return None

        session_id = _uuid_or_none(payload.get("sessionId"))
        matches = ClassSubject.objects.filter(
            session__school=record.school,
            session__status=AcademicLifecycleStatus.ACTIVE,
            academic_class__name__iexact=class_name,
            subject__name__iexact=subject_name,
            is_active=True,
        )
        if session_id is not None:
            matches = matches.filter(session_id=session_id)

        candidates = list(matches[:2])
        if not candidates:
            return None
        if len(candidates) > 1:
            raise ValueError(
                "multiple canonical ClassSubject records match the legacy "
                f"class/subject ({class_name!r} / {subject_name!r}); "
                "add an explicit classSubjectId before retrying"
            )
        return candidates[0]

    def _teacher(self, record):
        raw = str(record.payload.get("teacherId") or "").strip()
        if not raw:
            return None
        direct_id = _uuid_or_none(raw)
        direct = None
        if direct_id is not None:
            direct = Membership.objects.filter(
                id=direct_id,
                school=record.school,
                role=Role.TEACHER,
                is_active=True,
            ).first()
        if direct is not None:
            return direct

        profile = SyncRecord.objects.filter(
            school=record.school,
            entity_type="owner_staff_profile",
            entity_id=raw,
            deleted=False,
        ).first()
        linked = str(
            (profile.payload if profile else {}).get("linkedMembershipId") or ""
        ).strip()
        linked_id = _uuid_or_none(linked)
        if linked_id is None:
            return None
        return Membership.objects.filter(
            id=linked_id,
            school=record.school,
            role=Role.TEACHER,
            is_active=True,
        ).first()

    @staticmethod
    def _replace_payload(record, payload):
        if record.payload == payload and not record.deleted:
            return
        record.payload = payload
        record.deleted = False
        record.version += 1
        record.save(update_fields=["payload", "deleted", "version"])


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
