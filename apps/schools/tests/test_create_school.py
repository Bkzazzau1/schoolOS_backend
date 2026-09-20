from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.schools.models import Membership, Role, School

User = get_user_model()


class CreateSchoolCommandTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("create_school", *args, stdout=out)
        return out.getvalue()

    def test_creates_the_school_and_its_owner(self):
        output = self.run_command("BrightGate Academy", "--owner-email", "Owner@School.NG")
        school = School.objects.get(slug="brightgate-academy")
        owner = Membership.objects.get(school=school)
        self.assertEqual((owner.role, owner.user.email, owner.is_active), (Role.PROPRIETOR, "owner@school.ng", True))
        self.assertFalse(owner.user.has_usable_password())
        self.assertIn("changepassword", output)

    def test_it_reports_the_schools_web_address(self):
        with self.settings(PLATFORM_DOMAIN="schoolos.ng"):
            output = self.run_command("BrightGate", "--owner-email", "a@school.ng")
            self.assertIn("brightgate.schoolos.ng", output)
            output = self.run_command("Admin School", "--owner-email", "b@school.ng", "--slug", "admin")
            self.assertIn("no web address", output)
        self.assertIn("no web address", self.run_command("Plain", "--owner-email", "c@school.ng"))

    def test_an_existing_account_becomes_the_owner_and_keeps_its_password(self):
        user = User.objects.create_user("owner@school.ng", "a-long-test-password-1")
        self.run_command("BrightGate", "--owner-email", "owner@school.ng")
        user.refresh_from_db()
        self.assertTrue(user.check_password("a-long-test-password-1"))
        self.assertEqual(Membership.objects.get().user, user)

    def test_a_taken_slug_is_refused_and_creates_nothing(self):
        self.run_command("BrightGate", "--owner-email", "a@school.ng")
        with self.assertRaises(CommandError):
            self.run_command("Bright Gate", "--owner-email", "b@school.ng", "--slug", "brightgate")
        self.assertEqual(School.objects.count(), 1)
        self.assertFalse(User.objects.filter(email="b@school.ng").exists())

    def test_a_name_that_makes_no_slug_needs_one_given(self):
        with self.assertRaises(CommandError):
            self.run_command("!!!", "--owner-email", "a@school.ng")
        self.run_command("!!!", "--owner-email", "a@school.ng", "--slug", "abc")
        self.assertTrue(School.objects.filter(slug="abc").exists())
