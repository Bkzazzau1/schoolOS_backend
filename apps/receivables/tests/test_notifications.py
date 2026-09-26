from datetime import timedelta

from django.contrib.auth import get_user_model

from apps.notifications.models import Notification
from apps.schools.models import Membership, Role
from apps.students.models import GuardianLink

from .. import adjustments, allocation, collection_accounts, notifications, schedules
from .base import ReceivablesTestCase, CLOCK

N = 100
User = get_user_model()


class NotificationCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.parent = self.members["parent"]
        GuardianLink.objects.filter(student__in=[self.ahmad, self.aisha, self.maryam]).update(account_user=self.parent.user)
        self.finance = self.members["accountant"]
        self.delegate = self.members["principal"]
        self.give_duty(self.delegate)

    def told(self, kind, recipient=None):
        rows = Notification.objects.filter(kind=kind)
        return list(rows.filter(recipient=recipient) if recipient else rows)


class RecipientsTests(NotificationCase):
    def test_the_finance_side_is_the_owner_the_finance_office_and_authorised_delegates_each_once(self):
        found = notifications.finance_recipients(self.school)
        self.assertEqual({m.id for m in found}, {self.owner.id, self.finance.id, self.delegate.id})
        self.assertEqual(len(found), len({m.id for m in found}))

    def test_a_family_is_reached_through_the_signed_in_accounts_of_its_guardians_only(self):
        self.assertEqual([m.id for m in notifications.parent_recipients(self.family)], [self.parent.id])
        stranger = Membership.objects.create(user=User.objects.create_user("x@school.ng", "a-long-test-password-1"), school=self.school, role=Role.PARENT)
        self.assertNotIn(stranger.id, [m.id for m in notifications.parent_recipients(self.family)])
        # a guardian with no signed-in account reaches nobody
        GuardianLink.objects.update(account_user=None)
        self.assertEqual(notifications.parent_recipients(self.family), [])


class PublishingTests(NotificationCase):
    def test_publishing_tells_the_finance_side_and_each_family_and_nobody_else(self):
        self.publish_bello_fees()
        for who in (self.owner, self.finance, self.delegate):
            (note,) = self.told("fees_published", who)
            self.assertIn("3 charge(s) raised for 1 family(ies)", note.message)
        (mine,) = self.told("family_new_fees", self.parent)
        self.assertIn("₦300,000 is now due", mine.message)
        for role in ("teacher", "principal", "administrator", "student"):
            if self.members[role] != self.delegate:
                self.assertEqual(Notification.objects.filter(recipient=self.members[role]).count(), 0, role)

    def test_students_who_could_not_be_charged_are_said_to_the_finance_side(self):
        orphan = self.make_student("Ola", "Alone")
        self.enroll(orphan, self.term.session.school.academic_classes.first(), self.session)
        schedule = schedules.create_schedule(self.school, session=self.session, name="Everyone", actor=self.owner)
        schedules.add_item(schedule, actor=self.owner, code="ALL", name="Levy", amount_minor=1000 * N, due_date=CLOCK + timedelta(days=9))
        schedules.publish(schedule, actor=self.owner)
        (note,) = self.told("fees_published", self.finance)
        self.assertIn("1 student(s) have no family yet and were not charged", note.message)

    def test_refreshing_does_not_announce_again(self):
        schedule = self.publish_bello_fees()
        before = Notification.objects.count()
        schedules.refresh(schedule, actor=self.owner)
        self.assertEqual(Notification.objects.count(), before)


class PaymentTests(NotificationCase):
    def setUp(self):
        super().setUp()
        self.publish_bello_fees()
        Notification.objects.all().delete()

    def pay(self, amount, sandbox=False):
        tx = self.payment(amount)
        if sandbox:
            type(tx).objects.filter(pk=tx.pk).update(is_sandbox=True)
            tx.refresh_from_db()
        return allocation.allocate(tx, self.family)

    def test_a_part_payment_tells_the_parent_what_is_still_due(self):
        self.pay(100_000 * N)
        (note,) = self.told("family_payment", self.parent)
        self.assertEqual(note.title, "Payment received")
        self.assertIn("₦100,000", note.message)
        self.assertIn("₦200,000 is still due", note.message)
        self.assertEqual(self.told("family_payment", self.finance), [])  # the finance side hears of matches elsewhere

    def test_settling_everything_says_so(self):
        self.pay(300_000 * N)
        (note,) = self.told("family_fees_settled", self.parent)
        self.assertIn("paid everything due", note.message)
        self.assertEqual(self.told("family_payment", self.parent), [])

    def test_an_overpayment_tells_the_finance_side_it_is_held_as_credit(self):
        self.pay(320_000 * N)
        for who in (self.owner, self.finance, self.delegate):
            (note,) = self.told("family_credit", who)
            self.assertIn("₦20,000", note.message)
            self.assertIn("held as family credit", note.message)

    def test_money_on_a_family_that_owes_nothing_is_said_to_be_held_as_credit(self):
        self.pay(300_000 * N)
        Notification.objects.all().delete()
        self.pay(10_000 * N)
        (note,) = self.told("family_payment", self.parent)
        self.assertIn("Nothing was due, so it is held as credit", note.message)

    def test_test_data_is_labelled_so_nobody_mistakes_it_for_real_money(self):
        self.pay(50_000 * N, sandbox=True)
        (note,) = self.told("family_payment", self.parent)
        self.assertTrue(note.title.startswith("[Sandbox test data] "))

    def test_nothing_is_sent_for_a_payment_that_allocates_nothing(self):
        tx = self.payment(50_000 * N)
        allocation.allocate(tx, self.family)
        Notification.objects.all().delete()
        allocation.allocate(tx, self.family)  # asked again: nothing to do, nothing to say
        self.assertEqual(Notification.objects.count(), 0)


class AccountTests(NotificationCase):
    def test_a_collection_account_waking_up_is_told_to_the_finance_side(self):
        self.publish_bello_fees()
        collection_accounts.register(self.family, provider="monnify", account_number="8012345678", actor=self.owner)
        allocation.allocate(self.payment(300_000 * N), self.family)
        Notification.objects.all().delete()
        s = schedules.create_schedule(self.school, session=self.session, name="Next", actor=self.owner)
        schedules.add_item(s, actor=self.owner, code="N", name="Next", amount_minor=10_000 * N, due_date=CLOCK + timedelta(days=30), scope="student", student=self.ahmad)
        schedules.publish(s, actor=self.owner)
        for who in (self.owner, self.finance, self.delegate):
            self.assertEqual(len(self.told("collection_account_active", who)), 1, who.role)
        self.assertEqual(self.told("collection_account_active", self.parent), [])

    def test_going_dormant_is_not_a_notification_it_is_just_the_family_being_settled(self):
        self.publish_bello_fees()
        collection_accounts.register(self.family, provider="monnify", account_number="8012345678", actor=self.owner)
        Notification.objects.all().delete()
        allocation.allocate(self.payment(300_000 * N), self.family)
        self.assertEqual(self.told("collection_account_active"), [])

    def test_a_scholarship_that_clears_a_debt_is_quiet(self):
        self.publish_bello_fees()
        Notification.objects.all().delete()
        for student in (self.ahmad, self.aisha, self.maryam):
            r = self.charge(student)
            adjustments.adjust(r, kind="waiver", amount_minor=r.gross_amount_minor, reason="Free places", actor=self.owner)
        self.assertEqual(Notification.objects.filter(kind__in=("family_payment", "family_fees_settled")).count(), 0)


class SchoolBoundaryTests(NotificationCase):
    def test_another_school_hears_nothing(self):
        self.publish_bello_fees()
        allocation.allocate(self.payment(320_000 * N), self.family)
        self.assertEqual(Notification.objects.filter(school=self.other_school).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.other_owner).count(), 0)
