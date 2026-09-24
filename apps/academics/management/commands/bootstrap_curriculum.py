import json
import uuid
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.academics.curriculum_services import upsert_class_subject, upsert_subject
from apps.academics.models import AcademicClass, AcademicLifecycleStatus, AcademicSession, CurriculumRequirement, Subject
from apps.schools.models import Membership, Role, School


class Command(BaseCommand):
    help = "Bootstrap an explicit school curriculum from JSON. Safe to rerun; no default curriculum is invented."

    def add_arguments(self, parser):
        parser.add_argument("--school", required=True, help="School UUID or slug")
        parser.add_argument("--session", required=True, help="Academic session UUID or code")
        parser.add_argument("--file", required=True, help="Curriculum JSON manifest")
        parser.add_argument("--actor-membership", help="Optional active Administrator/Proprietor/Principal membership UUID")

    @transaction.atomic
    def handle(self, *args, **options):
        school = self._school(options["school"])
        session = self._session(school, options["session"])
        if session.status == AcademicLifecycleStatus.CLOSED:
            raise CommandError("Closed academic sessions are historical and cannot be changed.")
        actor = self._actor(school, options.get("actor_membership"))
        manifest = self._manifest(options["file"])
        subjects = manifest.get("subjects")
        curriculum = manifest.get("curriculum")
        if not isinstance(subjects, list) or not isinstance(curriculum, list):
            raise CommandError("Manifest must contain 'subjects' and 'curriculum' lists.")

        by_code = {}
        for index, raw in enumerate(subjects, 1):
            if not isinstance(raw, dict):
                raise CommandError(f"subjects[{index}] must be an object.")
            code = self._text(raw, "code", f"subjects[{index}]")
            name = self._text(raw, "name", f"subjects[{index}]")
            matches = list(Subject.objects.filter(school=school, code__iexact=code)[:2])
            if len(matches) > 1:
                raise CommandError(f"Subject code {code!r} is ambiguous.")
            existing = matches[0] if matches else None
            if existing is not None and existing.name.lower() != name.lower():
                raise CommandError(f"Subject {code!r} already exists as {existing.name!r}; refusing identity rewrite.")
            subject = upsert_subject(
                membership=actor,
                payload={
                    "id": str(existing.id if existing else uuid.uuid4()),
                    "code": code,
                    "name": name,
                    "shortName": str(raw.get("shortName") or "").strip(),
                    "section": str(raw.get("section") or "").strip(),
                    "isActive": self._bool(raw, "isActive", True, f"subjects[{index}]"),
                },
            )
            key = subject.code.lower()
            if key in by_code:
                raise CommandError(f"Duplicate subject code in manifest: {subject.code!r}.")
            by_code[key] = subject

        seen = set()
        for index, raw in enumerate(curriculum, 1):
            if not isinstance(raw, dict):
                raise CommandError(f"curriculum[{index}] must be an object.")
            class_code = self._text(raw, "classCode", f"curriculum[{index}]")
            subject_code = self._text(raw, "subjectCode", f"curriculum[{index}]")
            requirement = self._text(raw, "requirement", f"curriculum[{index}]").lower()
            if requirement not in {CurriculumRequirement.COMPULSORY, CurriculumRequirement.ELECTIVE}:
                raise CommandError(f"curriculum[{index}].requirement must be compulsory or elective.")
            periods = raw.get("periodsPerWeek")
            if not isinstance(periods, int) or isinstance(periods, bool) or periods < 1:
                raise CommandError(f"curriculum[{index}].periodsPerWeek must be a positive integer.")
            academic_class = AcademicClass.objects.filter(school=school, code__iexact=class_code).first()
            if academic_class is None:
                raise CommandError(f"Unknown class code {class_code!r}.")
            subject = by_code.get(subject_code.lower()) or Subject.objects.filter(school=school, code__iexact=subject_code).first()
            if subject is None:
                raise CommandError(f"Unknown subject code {subject_code!r}.")
            identity = (academic_class.id, subject.id)
            if identity in seen:
                raise CommandError(f"Duplicate curriculum entry for {class_code}/{subject_code}.")
            seen.add(identity)
            upsert_class_subject(
                membership=actor,
                payload={
                    "sessionId": str(session.id),
                    "classId": str(academic_class.id),
                    "subjectId": str(subject.id),
                    "requirement": requirement,
                    "periodsPerWeek": periods,
                    "isActive": self._bool(raw, "isActive", True, f"curriculum[{index}]"),
                },
            )

        self.stdout.write(self.style.SUCCESS(f"Curriculum bootstrap complete for {school.name} / {session.name}."))

    @staticmethod
    def _text(raw, key, location):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CommandError(f"{location}.{key} is required text.")
        return value.strip()

    @staticmethod
    def _bool(raw, key, default, location):
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise CommandError(f"{location}.{key} must be a boolean.")
        return value

    @staticmethod
    def _manifest(value):
        path = Path(value)
        if not path.is_file():
            raise CommandError(f"Manifest not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read manifest: {exc}") from exc
        if not isinstance(data, dict):
            raise CommandError("Manifest root must be an object.")
        return data

    @staticmethod
    def _school(value):
        try:
            school = School.objects.filter(id=uuid.UUID(str(value))).first()
        except (ValueError, TypeError, AttributeError):
            school = None
        school = school or School.objects.filter(slug=value).first()
        if school is None:
            raise CommandError(f"School {value!r} not found.")
        return school

    @staticmethod
    def _session(school, value):
        try:
            session = AcademicSession.objects.filter(school=school, id=uuid.UUID(str(value))).first()
        except (ValueError, TypeError, AttributeError):
            session = None
        session = session or AcademicSession.objects.filter(school=school, code=value).first()
        if session is None:
            raise CommandError(f"Session {value!r} not found in {school.name}.")
        return session

    @staticmethod
    def _actor(school, explicit):
        if explicit:
            try:
                actor_id = uuid.UUID(str(explicit))
            except (ValueError, TypeError, AttributeError) as exc:
                raise CommandError("--actor-membership must be a UUID.") from exc
            actor = Membership.objects.filter(id=actor_id, school=school, is_active=True).first()
            if actor is None or actor.role not in {Role.ADMINISTRATOR, Role.PROPRIETOR, Role.PRINCIPAL}:
                raise CommandError("Actor must be an active Administrator, Proprietor, or Principal in this school.")
            return actor
        for role in (Role.ADMINISTRATOR, Role.PROPRIETOR, Role.PRINCIPAL):
            actor = Membership.objects.filter(school=school, role=role, is_active=True).order_by("created_at").first()
            if actor is not None:
                return actor
        raise CommandError("No active Administrator, Proprietor, or Principal can own the bootstrap changes.")
