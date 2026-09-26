import itertools
from datetime import date

from django.utils import timezone

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm, EnrollmentAcademicContext
from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student, StudentEnrollment, StudentRegistration
from apps.sync.models import SyncRecord

from ..constants import BILLING_AUTHORITY_DUTY

_numbers = itertools.count(1)


class ReceivablesTestCase(StaffTestCase):
    """A school with one person in every role, and a second, separate school."""

    def give_duty(self, member, duty=BILLING_AUTHORITY_DUTY, status="active", school=None):
        SyncRecord.objects.update_or_create(
            school=school or self.school, entity_type="owner_job_assignment", entity_id=f"JOB-{member.id}-{duty}",
            defaults={"payload": {"registeredStaffId": f"S-{member.id}", "duties": [duty], "status": status,
                                   "membershipId": str(member.id)}},
        )

    # -- students ----------------------------------------------------------------------------

    def make_student(self, first, surname, *, school=None, status="active", guardian=None, phone=None, ref="", sibling="",
                     with_registration=False):
        n = next(_numbers)
        school = school or self.school
        student = Student.objects.create(
            school=school, student_code=f"STU-{n:05d}", admission_number=f"ADM/{n:05d}",
            first_name=first, surname=surname, status=status,
        )
        if guardian:
            GuardianLink.objects.create(
                student=student, name=guardian, phone=phone or f"0803{n:07d}", is_primary=True,
                family_account_ref=ref, sibling_link=sibling,
            )
        if with_registration:
            StudentRegistration.objects.create(
                school=school, registration_id=f"REG-{n:05d}", student=student, first_name=first, surname=surname,
                gender="F", academic_section="Primary", proposed_class="Primary 3", admission_number=student.admission_number,
                student_code=student.student_code, primary_guardian=guardian or "", guardian_phone=phone or "",
                family_account_ref=ref, sibling_link=sibling,
            )
        return student

    # -- the school year ---------------------------------------------------------------------

    def make_year(self, school=None):
        """An active session and term, and two classes in two sections."""
        school = school or self.school
        session = AcademicSession.objects.create(
            school=school, code="2026/27", name="2026/2027", starts_on=date(2026, 9, 7), ends_on=date(2027, 7, 20), status="active",
        )
        term = AcademicTerm.objects.create(
            session=session, code="T1", name="First Term", sequence=1, starts_on=date(2026, 9, 7), ends_on=date(2026, 12, 18), status="active",
        )
        primary = AcademicClass.objects.create(school=school, code="PRI3", name="Primary 3", section="Primary", level_order=3)
        jss = AcademicClass.objects.create(school=school, code="JSS1", name="JSS 1", section="Secondary", level_order=7)
        return session, term, primary, jss

    def enroll(self, student, academic_class, session, *, billable=True):
        enrollment = StudentEnrollment.objects.create(
            school=student.school, student=student, academic_section=academic_class.section, class_name=academic_class.name,
            status="active", is_billable=billable, started_at=timezone.now(),
        )
        # The academics app may already have placed them (it does when the class name matches); this makes it exact.
        EnrollmentAcademicContext.objects.update_or_create(
            enrollment=enrollment, defaults={"session": session, "academic_class": academic_class},
        )
        return enrollment
