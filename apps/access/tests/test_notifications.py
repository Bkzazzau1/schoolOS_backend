from datetime import timedelta

from django.utils import timezone

from apps.access import catalog, services
from apps.access.services import AccessError
from apps.notifications.models import Notification

from .helpers import AccessTestCase

GRANT, BLOCK = "grant", "block"


class PeopleAreToldTests(AccessTestCase):
    """Every change that affects someone tells them, and only them."""

    def inbox(self, member):
        return list(Notification.objects.filter(recipient=member).order_by("id"))

    def only_message(self, member):
        messages = self.inbox(member)
        self.assertEqual(len(messages), 1, [m.message for m in messages])
        return messages[0]

    def nobody_told(self, *members):
        for member in members:
            self.assertEqual(self.inbox(member), [], member.role)

    # -- one person -------------------------------------------------------------------

    def test_a_grant_tells_the_person_and_nobody_else(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "finance.payroll", GRANT, note="Covering Mrs Musa")
        message = self.only_message(teacher)
        self.assertEqual((message.kind, message.title), ("access_changed", "Your access changed"))
        self.assertIn("You can now use Payroll Handoff.", message.message)
        self.assertIn("Note from the owner: Covering Mrs Musa", message.message)
        self.assertEqual(message.data, {"activities": ["finance.payroll"], "effect": "grant"})
        self.assertEqual((message.school, message.read_at), (self.school, None))
        self.nobody_told(self.owner, self.members["principal"], self.other_teacher)

    def test_a_time_limited_grant_says_until_when(self):
        teacher = self.members["teacher"]
        end = timezone.now() + timedelta(days=5)
        services.set_person_override(self.owner, teacher.id, "finance.payroll", GRANT, expires_at=end)
        self.assertIn(" until ", self.only_message(teacher).message)

    def test_a_block_that_waits_says_it_goes_after_their_next_sync(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK)
        message = self.only_message(teacher)
        self.assertIn("CBT Practice will be removed from your account after your next sync", message.message)
        self.assertIn("no later than", message.message)
        self.assertIn("finalizeAt", message.data)
        self.assertEqual(message.data["activities"], ["teacher.cbt"])

    def test_an_immediate_block_says_it_is_gone(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="immediate", note="Exam week")
        message = self.only_message(teacher)
        self.assertIn("You no longer have CBT Practice.", message.message)
        self.assertIn("Exam week", message.message)

    def test_nothing_changes_for_the_person_so_nothing_is_sent(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "principal.approvals", BLOCK)   # never had it
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", GRANT)           # already has it
        self.nobody_told(teacher)

    def test_clearing_tells_them_what_they_now_have(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "finance.payroll", GRANT)
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="immediate")
        Notification.objects.all().delete()
        services.clear_person_override(self.owner, teacher.id, "finance.payroll")
        services.clear_person_override(self.owner, teacher.id, "teacher.cbt")
        texts = [m.message for m in self.inbox(teacher)]
        self.assertEqual(texts, ["You no longer have Payroll Handoff.", "You can now use CBT Practice."])

    def test_cancelling_a_waiting_block_tells_them_it_will_stay(self):
        teacher = self.members["teacher"]
        services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK)
        Notification.objects.all().delete()
        services.clear_person_override(self.owner, teacher.id, "teacher.cbt")
        self.assertIn("will stay on your account", self.only_message(teacher).message)

    def test_reassigning_tells_both_people(self):
        principal, admin = self.members["principal"], self.members["administrator"]
        services.reassign(self.owner, "principal.approvals", principal.id, admin.id, note="On leave")
        self.assertIn("removed from your account after your next sync", self.only_message(principal).message)
        self.assertIn("You can now use Approvals.", self.only_message(admin).message)
        self.nobody_told(self.members["teacher"], self.owner)

    def test_a_refused_change_tells_nobody(self):
        teacher = self.members["teacher"]
        for call in (
            lambda: services.set_person_override(self.owner, teacher.id, "teacher.dashboard", BLOCK),
            lambda: services.set_person_override(self.owner, teacher.id, "owner.access", GRANT),
            lambda: services.set_person_override(self.owner, teacher.id, "teacher.cbt", BLOCK, mode="soon"),
            lambda: services.reassign(self.owner, "teacher.cbt", teacher.id, teacher.id),
        ):
            with self.assertRaises(AccessError):
                call()
        self.assertEqual(Notification.objects.count(), 0)

    # -- a whole role -----------------------------------------------------------------

    def test_changing_a_role_tells_everyone_in_it_and_only_them(self):
        second_teacher_user = self.members["teacher"].user.__class__.objects.create_user("t2@school.ng", "a-long-test-password-1")
        second = self.members["teacher"].__class__.objects.create(user=second_teacher_user, school=self.school, role="teacher")
        keys = catalog.default_keys("teacher") - {"teacher.cbt"} | {"finance.receipts"}
        services.set_role_defaults(self.owner, "teacher", keys)
        for member in (self.members["teacher"], second):
            message = self.only_message(member)
            self.assertIn("You can now use: Receipts.", message.message)
            self.assertIn("No longer available: CBT Practice.", message.message)
            self.assertEqual(message.data["removed"], ["teacher.cbt"])
        self.nobody_told(self.members["principal"], self.members["parent"], self.owner, self.other_teacher)

    def test_setting_a_role_to_what_it_already_is_tells_nobody(self):
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher"))
        self.assertEqual(Notification.objects.count(), 0)

    def test_resetting_a_role_tells_its_people_what_came_back(self):
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher") - {"teacher.cbt"})
        Notification.objects.all().delete()
        services.reset_role_defaults(self.owner, "teacher")
        self.assertIn("You can now use: CBT Practice.", self.only_message(self.members["teacher"]).message)

    def test_a_long_list_is_shortened(self):
        keys = catalog.default_keys("teacher") | {k for k in catalog.ACTIVITIES if k.startswith("finance.") and k != "finance.dashboard"}
        services.set_role_defaults(self.owner, "teacher", keys)
        message = self.only_message(self.members["teacher"]).message
        self.assertRegex(message, r"and \d+ more")

    def test_inactive_members_are_not_told(self):
        teacher = self.members["teacher"]
        teacher.is_active = False
        teacher.save()
        services.set_role_defaults(self.owner, "teacher", catalog.default_keys("teacher") | {"finance.receipts"})
        self.nobody_told(teacher)
