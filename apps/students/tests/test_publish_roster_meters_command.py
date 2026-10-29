"""`publish_roster_meters` is the daily job that keeps every active school's authoritative
billing meter fresh (Phase C, section 11) - apps.billing's automatic cycle runner can only
issue an invoice once every active school has one for the current period, so a gap here is a
silent reason billing never runs, not just a students-app concern."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import SchoolBillingMeterSnapshot
from apps.schools.models import School
from apps.students.models import EnrollmentStatus, Student, StudentEnrollment
from apps.sync.models import SyncRecord


def _school(name="BrightGate Academy", *, is_active=True):
    return School.objects.create(name=name, slug=name.lower().replace(" ", "-"), is_active=is_active)


class PublishRosterMetersCommandTests(TestCase):
    def test_publishes_a_fresh_authoritative_meter_for_every_active_school(self):
        school = _school()
        student = Student.objects.create(school=school, student_code="STU-1", admission_number="ADM-1", first_name="Test", surname="Student")
        StudentEnrollment.objects.create(school=school, student=student, academic_section="Secondary", class_name="JSS 1", status=EnrollmentStatus.ACTIVE, is_billable=True, started_at=timezone.now())

        out = StringIO()
        call_command("publish_roster_meters", stdout=out)

        meter = SchoolBillingMeterSnapshot.objects.filter(school=school, source="canonical_student_roster").order_by("-measured_at").first()
        self.assertIsNotNone(meter)
        self.assertEqual((meter.billable_student_count, meter.authoritative), (1, True))
        self.assertIn("1 active schools", out.getvalue())

    def test_an_inactive_school_is_never_published_for(self):
        _school("Closed Academy", is_active=False)
        out = StringIO()
        call_command("publish_roster_meters", stdout=out)
        self.assertFalse(SchoolBillingMeterSnapshot.objects.exists())

    def test_running_it_twice_the_same_day_is_idempotent(self):
        _school()
        call_command("publish_roster_meters", stdout=StringIO())
        first_count = SchoolBillingMeterSnapshot.objects.count()
        call_command("publish_roster_meters", stdout=StringIO())
        self.assertEqual(SchoolBillingMeterSnapshot.objects.count(), first_count)

    def test_a_school_with_unmaterialized_legacy_registrations_is_withheld_but_others_still_publish(self):
        ready_school = _school("Ready Academy")
        blocked_school = _school("Blocked Academy")
        SyncRecord.objects.create(school=blocked_school, entity_type="student_registration", entity_id="reg-1", payload={}, deleted=False)

        out, err = StringIO(), StringIO()
        with self.assertRaises(CommandError):
            call_command("publish_roster_meters", stdout=out, stderr=err)

        self.assertTrue(SchoolBillingMeterSnapshot.objects.filter(school=ready_school).exists())
        self.assertFalse(SchoolBillingMeterSnapshot.objects.filter(school=blocked_school).exists())
