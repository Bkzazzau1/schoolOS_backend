import itertools
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.utils import timezone

from apps.academics.models import AcademicClass, AcademicSession, AcademicTerm, EnrollmentAcademicContext
from apps.staff.tests.helpers import StaffTestCase
from apps.students.models import GuardianLink, Student, StudentEnrollment, StudentRegistration
from apps.sync.models import SyncRecord

from apps.bankconnect import ingestion
from apps.bankconnect.models import BankConnection
from apps.bankconnect.providers.base import NormalizedTransaction

from .. import families, ledger, periods, schedules
from ..constants import BILLING_AUTHORITY_DUTY

_numbers = itertools.count(1)

#: The day these tests are set on: inside `make_year`'s First Term, before its fees fall due. Fixed so that what
#: is overdue, in arrears or current never depends on the day the suite happens to be run.
CLOCK = date(2026, 9, 26)


class ReceivablesTestCase(StaffTestCase):
    """A school with one person in every role, and a second, separate school."""

    def setUp(self):
        super().setUp()
        self.today = CLOCK
        #: When students in these tests were enrolled: before the year began, whatever day the suite is run.
        self.started = timezone.make_aware(datetime.combine(date(2026, 8, 27), time(9, 0)))
        clock = mock.patch.object(periods, "school_today", return_value=CLOCK)
        clock.start()
        self.addCleanup(clock.stop)

    def set_today(self, day: date):
        """Move the fee system's clock (for the whole test)."""
        periods.school_today.return_value = day
        self.today = day

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
            status="active", is_billable=billable, started_at=self.started,
        )
        # The academics app may already have placed them (it does when the class name matches); this makes it exact.
        EnrollmentAcademicContext.objects.update_or_create(
            enrollment=enrollment, defaults={"session": session, "academic_class": academic_class},
        )
        return enrollment

    # -- money -------------------------------------------------------------------------------

    def bank_connection(self, school=None):
        """A real (not sandbox) account of the school. Made directly: what is under test is what the ledger does
        with a payment, not how it was fetched."""
        school = school or self.school
        connection = getattr(self, "_connections", {}).get(school.id)
        if connection is None:
            connection = BankConnection.objects.create(
                school=school, provider="gtbank", connection_type="direct_bank_api", bank_name="GTBank", account_name="SCHOOL",
                account_mask="****1111", purpose="tuition", status="connected", is_sandbox=False,
            )
            self._connections = {**getattr(self, "_connections", {}), school.id: connection}
        return connection

    def payment(self, amount_minor, *, school=None, receiving_account="", sender="Musa Bello", direction="credit", **over):
        """A bank payment received by the school, stored but not yet matched to anyone."""
        fields = dict(
            external_transaction_id=f"PAY-{next(_numbers)}", direction=direction, amount_minor=amount_minor,
            transaction_date=timezone.now(), sender_name=sender, receiving_account_reference=receiving_account,
        )
        fields.update(over)
        result = ingestion.ingest(self.bank_connection(school), NormalizedTransaction(**fields))
        assert result.transaction is not None
        return result.transaction

    # -- a school with fees ------------------------------------------------------------------

    def bello_family(self):
        """The Bello household from the worked example: three children, each with their own tuition charge."""
        session, term, primary, jss = self.make_year()
        self.session, self.term = session, term
        self.ahmad = self.make_student("Ahmad", "Bello", guardian="Musa Bello", phone="08031111111")
        self.aisha = self.make_student("Aisha", "Bello", guardian="Musa Bello", phone="08031111111")
        self.maryam = self.make_student("Maryam", "Bello", guardian="Musa Bello", phone="08031111111")
        for student, klass in ((self.ahmad, jss), (self.aisha, primary), (self.maryam, primary)):
            self.enroll(student, klass, session)
        self.family = families.create_family(self.school, display_name="Bello family", actor=self.owner, students=[self.ahmad, self.aisha, self.maryam])
        for student in (self.ahmad, self.aisha, self.maryam):
            families.link_guardians_of(self.family, student)
        return self.family

    def publish_bello_fees(self, due=None):
        """Ahmad owes 120,000, Aisha 100,000, Maryam 80,000 (in kobo), all due on `due` (default: a month from now)."""
        due = due or (self.today + timedelta(days=20))
        schedule = schedules.create_schedule(self.school, session=self.session, term=self.term, name="First Term", actor=self.owner)
        for code, student, amount in (("TUI-AHMAD", self.ahmad, 12_000_000), ("TUI-AISHA", self.aisha, 10_000_000), ("TUI-MARYAM", self.maryam, 8_000_000)):
            schedules.add_item(schedule, actor=self.owner, code=code, name="Tuition", category="tuition", amount_minor=amount, due_date=due, scope="student", student=student)
        schedules.publish(schedule, actor=self.owner)
        return schedule

    def charge(self, student, code=None):
        from ..models import StudentReceivable

        query = StudentReceivable.objects.filter(student=student)
        return query.get(item_code=code) if code else query.get()

    def assertLedgerHolds(self, family=None):
        problems = ledger.verify_family(family or self.family)
        self.assertEqual(problems, [], problems)
