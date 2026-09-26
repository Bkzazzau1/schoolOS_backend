"""Who a fee applies to - decided from the school's own academic records, never from class names typed
by a client.

A student's class for a session is the `EnrollmentAcademicContext` the academics app keeps for their
active enrolment. A fee aimed at a section or a class reaches only students that record places there.
A billable student with no such record for the session cannot be placed, so they are REPORTED (and not
billed) instead of guessed at: the school fixes the placement, and the fee then reaches them.
"""

from dataclasses import dataclass, field

from apps.academics.models import EnrollmentAcademicContext
from apps.students.models import EnrollmentStatus, Student, StudentStatus

from . import periods
from .models import FeeScope

#: Students who can be charged. A student who has left is not billed new fees.
BILLABLE_STATUSES = (StudentStatus.ACTIVE, StudentStatus.TRANSFER_PENDING)


@dataclass
class Resolution:
    #: item id -> the students that item applies to
    by_item: dict = field(default_factory=dict)
    #: billable students with no academic placement for the session: not charged, and reported
    unclassified: list = field(default_factory=list)
    #: students who entered after the schedule's term began, so owe nothing for it (not an error: just not charged)
    joined_later: list = field(default_factory=list)


def _placements(schedule):
    return list(
        EnrollmentAcademicContext.objects.filter(
            session=schedule.session,
            enrollment__school=schedule.school,
            enrollment__status=EnrollmentStatus.ACTIVE,
            enrollment__is_billable=True,
            enrollment__student__status__in=BILLABLE_STATUSES,
        ).select_related("enrollment", "enrollment__student", "academic_class", "entry_term")
    )


def resolve(schedule) -> Resolution:
    everyone = _placements(schedule)
    placed_ids = {p.enrollment.student_id for p in everyone}
    # A fee for a TERM reaches only students who were enrolled by then: entering in Term 2 means owing nothing for Term 1.
    later = [p for p in everyone if periods.joined_after(p, schedule.term)]
    placements = [p for p in everyone if p not in later]
    unclassified = list(
        Student.objects.filter(
            school=schedule.school, status__in=BILLABLE_STATUSES,
            enrollments__status=EnrollmentStatus.ACTIVE, enrollments__is_billable=True,
        ).exclude(id__in=placed_ids).distinct().order_by("surname", "first_name", "id")
    )
    result = Resolution(
        unclassified=unclassified,
        joined_later=sorted((p.enrollment.student for p in later), key=lambda s: (s.surname, s.first_name, str(s.id))),
    )
    for item in schedule.items.select_related("academic_class", "student"):
        if item.scope == FeeScope.ALL:
            students = [p.enrollment.student for p in placements]
        elif item.scope == FeeScope.SECTION:
            wanted = item.section.strip().casefold()
            students = [p.enrollment.student for p in placements if p.academic_class.section.strip().casefold() == wanted]
        elif item.scope == FeeScope.CLASS:
            students = [p.enrollment.student for p in placements if p.academic_class_id == item.academic_class_id]
        else:  # one named student: the person choosing said so, so no placement is needed
            students = [item.student] if item.student.status in BILLABLE_STATUSES else []
        result.by_item[item.id] = sorted(students, key=lambda s: (s.surname, s.first_name, str(s.id)))
    return result
