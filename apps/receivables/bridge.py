"""Bringing the older free-text family references into the canonical Family model - carefully.

Student registrations and guardian records carry `family_account_ref` and `sibling_link`. They were
typed at admission, from a dropdown of labels ("Create new family account" is the default), so they
are NOT reliable identifiers: two families can be typed the same, one family can be typed two ways,
and "Create new family account" says nothing about who the family is. This module therefore:

* groups students ONLY where they share exactly the same explicit reference, and only if no student
  in the group gives a different one - never by name, phone or similarity;
* leaves everyone else unlinked and says so in a report, rather than guessing;
* optionally gives each remaining student a family of their own (never merged with anyone), which a
  person can join up later;
* can be run again: a group it made is found by its `origin` and extended, not duplicated.

The old fields stay where they are for compatibility. From here on the canonical Family is the
source of truth for anything to do with money.
"""

from dataclasses import dataclass, field

from django.db import transaction

from apps.students.models import GuardianLink, Student, StudentRegistration, StudentStatus

from . import audit, families
from .models import Family, FamilyStatus, FamilyStudent

#: What the registration form says when it means "no existing family". Compared without case.
NOT_A_REFERENCE = {"", "create new family account", "new", "none", "n/a", "na", "-", "unknown"}
ELIGIBLE = (StudentStatus.ACTIVE, StudentStatus.TRANSFER_PENDING)


def _usable(reference) -> str:
    cleaned = " ".join(str(reference or "").split())
    return "" if cleaned.casefold() in NOT_A_REFERENCE else cleaned


@dataclass
class Group:
    reference: str
    students: list = field(default_factory=list)
    #: The family already made for this reference by an earlier run, if there is one.
    existing: Family | None = None


@dataclass
class Report:
    already_in_family: int = 0
    groups: list = field(default_factory=list)
    #: Students whose records name two different references: left alone.
    conflicts: list = field(default_factory=list)
    #: Students with no usable reference.
    without_reference: list = field(default_factory=list)
    #: Of those, the ones whose `sibling_link` says they have a sibling - for a person to look at.
    sibling_hints: list = field(default_factory=list)
    families_created: int = 0
    students_linked: int = 0


def analyse(school) -> Report:
    """What the bridge would do, without doing anything."""
    report = Report()
    students = list(Student.objects.filter(school=school, status__in=ELIGIBLE).order_by("surname", "first_name", "id"))
    in_family = set(FamilyStudent.objects.filter(school=school, is_active=True).values_list("student_id", flat=True))
    registrations = {r.student_id: r for r in StudentRegistration.objects.filter(school=school, student__isnull=False)}
    guardians: dict = {}
    for link in GuardianLink.objects.filter(student__school=school):
        guardians.setdefault(link.student_id, []).append(link)

    by_reference: dict[str, Group] = {}
    for student in students:
        if student.id in in_family:
            report.already_in_family += 1
            continue
        registration = registrations.get(student.id)
        links = guardians.get(student.id, [])
        references = {r for r in [_usable(registration.family_account_ref) if registration else ""] + [_usable(g.family_account_ref) for g in links] if r}
        if len(references) > 1:
            report.conflicts.append((student, sorted(references)))
        elif len(references) == 1:
            reference = next(iter(references))
            by_reference.setdefault(reference, Group(reference)).students.append(student)
        else:
            report.without_reference.append(student)
            hinted = (registration and registration.sibling_link.strip()) or any(g.sibling_link.strip() for g in links)
            if hinted:
                report.sibling_hints.append(student)

    existing = {f.origin: f for f in Family.objects.filter(school=school).exclude(origin="")}
    for reference, group in sorted(by_reference.items()):
        group.existing = existing.get(f"bridge:ref:{reference}")
        report.groups.append(group)
    return report


@transaction.atomic
def apply(school, *, singletons: bool = False, actor=None) -> Report:
    """Make the families the report describes. With `singletons`, also give each student who has no
    usable reference a family of their own."""
    report = analyse(school)
    for group in report.groups:
        family = group.existing
        if family is None:
            family = families.create_family(school, display_name=group.reference, actor=actor, origin=f"bridge:ref:{group.reference}")
            report.families_created += 1
        elif family.status != FamilyStatus.ACTIVE:
            continue  # closed on purpose by a person: leave it closed
        for student in group.students:
            families.add_student(family, student, actor=actor)
            families.link_guardians_of(family, student, actor=actor)
            report.students_linked += 1
    left_unlinked = len(report.without_reference)
    if singletons:
        for student in report.without_reference:
            _, created = families.ensure_family_for_student(school, student, actor=actor)
            report.families_created += created
            report.students_linked += created
            left_unlinked -= created
    audit.record(
        school, "families_bridged", actor=actor, object_type="School", object_id=school.id,
        created=report.families_created, linked=report.students_linked, conflicts=len(report.conflicts),
        left_unlinked=left_unlinked,
    )
    return report
