import itertools

from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student, StudentRegistration
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
