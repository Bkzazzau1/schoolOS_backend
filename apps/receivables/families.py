"""Making and changing families. Every change is audited, and a student is never in two active
families at once.

A family is the household a school bills, so it must be right: these services refuse to guess. A
student who already belongs to a family is not moved silently, and a family and a student from
different schools can never be linked.
"""

import secrets

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.students.models import GuardianLink, Student

from . import audit
from .errors import Refused
from .models import Family, FamilyGuardian, FamilyStatus, FamilyStudent

#: Letters and digits with nothing easily mistaken for another (no 0/O, 1/I).
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_NAME = 200


def _new_code() -> str:
    return "FAM-" + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def _clean_name(name) -> str:
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise Refused("Give the family a name.", "family_name_required")
    if len(cleaned) > MAX_NAME:
        raise Refused(f"A family name can be at most {MAX_NAME} characters.", "family_name_too_long")
    return cleaned


def family_of(student: Student) -> Family | None:
    """The family this student belongs to now, if any."""
    membership = FamilyStudent.objects.select_related("family").filter(student=student, is_active=True).first()
    return membership.family if membership else None


def active_students(family: Family):
    return Student.objects.filter(family_memberships__family=family, family_memberships__is_active=True)


def _attach(family: Family, student: Student, actor) -> FamilyStudent:
    if student.school_id != family.school_id:
        raise Refused("That student is not at this school.", "student_not_found")
    current = family_of(student)
    if current is not None:
        if current.id == family.id:
            raise Refused(f"{student.full_name} is already in this family.", "already_member")
        raise Refused(
            f"{student.full_name} already belongs to family {current.code} ({current.display_name}).", "student_in_family"
        )
    try:
        with transaction.atomic():
            return FamilyStudent.objects.create(family=family, student=student, created_by=actor)
    except IntegrityError:  # someone else attached them a moment ago: the database rule caught it
        raise Refused(f"{student.full_name} already belongs to a family.", "student_in_family")


@transaction.atomic
def create_family(school, *, display_name, actor=None, students=(), origin: str = "") -> Family:
    name = _clean_name(display_name)
    family = None
    for _ in range(20):  # a code collision is astronomically unlikely, but never fatal
        try:
            with transaction.atomic():
                family = Family.objects.create(
                    school=school, code=_new_code(), display_name=name, origin=origin, created_by=actor
                )
            break
        except IntegrityError:
            if origin and Family.objects.filter(school=school, origin=origin).exists():
                raise Refused("A family for this already exists.", "family_exists")
    if family is None:
        raise Refused("A family code could not be made. Try again.", "code_unavailable")
    for student in students:
        _attach(family, student, actor)
    audit.record(
        school, "family_created", actor=actor, obj=family,
        code=family.code, name=name, students=[str(s.id) for s in students], origin=origin,
    )
    return family


@transaction.atomic
def add_student(family: Family, student: Student, *, actor=None) -> FamilyStudent:
    family = Family.objects.select_for_update().get(pk=family.pk)
    if family.status != FamilyStatus.ACTIVE:
        raise Refused("This family is closed. Reopen it before adding students.", "family_closed")
    membership = _attach(family, student, actor)
    audit.record(family.school, "family_student_added", actor=actor, obj=family, student=str(student.id))
    return membership


@transaction.atomic
def remove_student(family: Family, student: Student, *, actor=None) -> None:
    """Take a student out of the family. Charges raised while they were in it stay with the family."""
    family = Family.objects.select_for_update().get(pk=family.pk)
    membership = FamilyStudent.objects.filter(family=family, student=student, is_active=True).first()
    if membership is None:
        raise Refused("That student is not in this family.", "not_a_member")
    membership.is_active = False
    membership.left_at = timezone.now()
    membership.save(update_fields=["is_active", "left_at"])
    # A guardian is a payer only for as long as one of their children is in the family.
    for link in FamilyGuardian.objects.filter(family=family, is_active=True, guardian__student=student):
        link.is_active = False
        link.is_primary_payer = False
        link.save(update_fields=["is_active", "is_primary_payer"])
    audit.record(family.school, "family_student_removed", actor=actor, obj=family, student=str(student.id))


@transaction.atomic
def rename(family: Family, display_name, *, actor=None) -> Family:
    name = _clean_name(display_name)
    before = family.display_name
    family.display_name = name
    family.save(update_fields=["display_name", "updated_at"])
    audit.record(family.school, "family_renamed", actor=actor, obj=family, before=before, after=name)
    return family


@transaction.atomic
def set_status(family: Family, status: str, *, actor=None) -> Family:
    if status not in FamilyStatus.values:
        raise Refused("That is not a family status.", "invalid_status")
    family.status = status
    family.save(update_fields=["status", "updated_at"])
    audit.record(family.school, "family_status_changed", actor=actor, obj=family, status=status)
    return family


def primary_payer(family: Family) -> FamilyGuardian | None:
    return FamilyGuardian.objects.select_related("guardian").filter(family=family, is_active=True, is_primary_payer=True).first()


@transaction.atomic
def link_guardian(family: Family, guardian: GuardianLink, *, primary: bool = False, actor=None) -> FamilyGuardian:
    """Make a guardian on one of the family's students a payer for the family."""
    if guardian.student.school_id != family.school_id:
        raise Refused("That guardian is not at this school.", "guardian_not_found")
    if not FamilyStudent.objects.filter(family=family, student=guardian.student, is_active=True).exists():
        raise Refused("A payer must be a guardian of one of the family's students.", "guardian_not_in_family")
    link, created = FamilyGuardian.objects.get_or_create(family=family, guardian=guardian, defaults={"is_active": True})
    if not created and not link.is_active:
        link.is_active = True
        link.save(update_fields=["is_active"])
    if primary and not link.is_primary_payer:
        FamilyGuardian.objects.filter(family=family, is_primary_payer=True).update(is_primary_payer=False)
        link.is_primary_payer = True
        link.save(update_fields=["is_primary_payer"])
    if created or primary:
        audit.record(
            family.school, "family_guardian_linked", actor=actor, obj=family,
            guardian=str(guardian.id), primary=link.is_primary_payer,
        )
    return link


def link_guardians_of(family: Family, student: Student, *, actor=None) -> None:
    """Make the guardians already on a student's record payers for the family. The first primary
    guardian becomes the primary payer if the family has none yet."""
    has_primary = primary_payer(family) is not None
    for guardian in GuardianLink.objects.filter(student=student).order_by("-is_primary", "created_at", "id"):
        wants_primary = guardian.is_primary and not has_primary
        link_guardian(family, guardian, primary=wants_primary, actor=actor)
        has_primary = has_primary or wants_primary


@transaction.atomic
def ensure_family_for_student(school, student: Student, *, actor=None) -> tuple[Family, bool]:
    """The student's family, making a family of their own if they have none. Never merges anyone: a
    student with no known relatives is simply their own family until a person says otherwise."""
    current = family_of(student)
    if current is not None:
        return current, False
    family = create_family(
        school, display_name=f"{student.surname} family", actor=actor, students=[student], origin=f"student:{student.id}"
    )
    link_guardians_of(family, student, actor=actor)
    return family, True


def search(school, term: str, *, limit: int = 30):
    """Families by their name or code, or by the name, code or admission number of a student in them."""
    term = " ".join(str(term or "").split())[:60]
    families = Family.objects.filter(school=school)
    if term:
        families = families.filter(
            Q(display_name__icontains=term) | Q(code__icontains=term)
            | Q(members__student__first_name__icontains=term) | Q(members__student__surname__icontains=term)
            | Q(members__student__student_code__icontains=term) | Q(members__student__admission_number__icontains=term)
        ).distinct()
    return families.order_by("display_name", "code")[:limit]


def students_without_family(school):
    """Students who could be charged but belong to no family yet - who someone must place before they can be billed."""
    from .applicability import BILLABLE_STATUSES

    placed = FamilyStudent.objects.filter(school=school, is_active=True).values_list("student_id", flat=True)
    return Student.objects.filter(school=school, status__in=BILLABLE_STATUSES).exclude(id__in=placed).order_by("surname", "first_name", "id")
