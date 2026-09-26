from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.schools.models import Membership, Role

from .. import families, plans, schedules
from ..errors import Refused
from ..models import FeeItem, FinanceAuditEvent, StudentReceivable
from .base import ReceivablesTestCase

DUE = date(2026, 10, 15)


class ScheduleTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.primary, self.jss = self.make_year()
        self.authority = self.members["principal"]
        self.give_duty(self.authority)

    def draft(self, name="First Term 2026", **kw):
        return schedules.create_schedule(self.school, session=self.session, term=self.term, name=name, actor=kw.pop("actor", self.owner), **kw)

    def item(self, schedule, code="TUITION", amount=12_500_000, **over):
        fields = dict(code=code, name=code.title(), category="tuition", amount_minor=amount, due_date=DUE)
        fields.update(over)
        return schedules.add_item(schedule, actor=self.owner, **fields)

    def pupil(self, first, surname, academic_class=None, *, family=None, status="active"):
        student = self.make_student(first, surname, status=status)
        if academic_class:
            self.enroll(student, academic_class, self.session)
        if family is not False:
            (families.add_student(family, student) if family else families.ensure_family_for_student(self.school, student))
        return student


class AuthorityTests(ScheduleTestCase):
    def test_the_owner_and_a_delegate_can_draft_and_the_finance_office_alone_cannot(self):
        self.assertEqual(self.draft("By owner").created_by, self.owner)
        self.assertEqual(self.draft("By delegate", actor=self.authority).created_by, self.authority)
        for role in ("accountant", "administrator", "teacher", "parent", "student"):
            with self.assertRaises(Refused) as raised:
                schedules.create_schedule(self.school, session=self.session, name="X", actor=self.members[role])
            self.assertEqual(raised.exception.code, "not_billing_authority", role)

    def test_a_membership_from_another_school_never_counts(self):
        with self.assertRaises(Refused):
            schedules.create_schedule(self.school, session=self.session, name="X", actor=self.other_owner)
        stranger = Membership.objects.create(user=self.authority.user, school=self.other_school, role=Role.PRINCIPAL)
        self.give_duty(stranger, school=self.other_school)
        with self.assertRaises(Refused):
            schedules.create_schedule(self.school, session=self.session, name="X", actor=stranger)

    def test_nothing_can_be_done_without_an_actor(self):
        with self.assertRaises(Refused):
            schedules.create_schedule(self.school, session=self.session, name="X", actor=None)

    def test_every_editing_and_publishing_step_checks_authority(self):
        schedule = self.draft()
        item = self.item(schedule)
        finance = self.members["accountant"]
        for call in (
            lambda: schedules.rename_schedule(schedule, "New", actor=finance),
            lambda: schedules.add_item(schedule, actor=finance, code="X", name="X", amount_minor=100, due_date=DUE),
            lambda: schedules.update_item(item, actor=finance, amount_minor=200),
            lambda: schedules.remove_item(item, actor=finance),
            lambda: schedules.publish(schedule, actor=finance),
            lambda: schedules.clone(schedule, actor=finance),
        ):
            with self.assertRaises(Refused) as raised:
                call()
            self.assertEqual(raised.exception.code, "not_billing_authority")

    def test_another_schools_session_cannot_be_used(self):
        other_session, _, _, _ = self.make_year(self.other_school)
        with self.assertRaises(Refused):
            schedules.create_schedule(self.school, session=other_session, name="X", actor=self.owner)

    def test_a_term_must_be_in_the_session(self):
        other_session, other_term, _, _ = self.make_year(self.other_school)
        with self.assertRaises(Refused):
            schedules.create_schedule(self.school, session=self.session, term=other_term, name="X", actor=self.owner)


class DraftingTests(ScheduleTestCase):
    def test_a_draft_has_a_name_and_its_history_is_audited(self):
        schedule = self.draft("First Term 2026")
        self.assertEqual((schedule.status, schedule.created_by), ("draft", self.owner))
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="fee_schedule_created", object_id=str(schedule.id)).exists())

    def test_names_are_unique_among_live_schedules_and_reusable_after_retirement(self):
        first = self.draft("Term 1")
        with self.assertRaises(Refused):
            self.draft("Term 1")
        schedules.retire(first, actor=self.owner, reason="Made in error")
        self.assertEqual(self.draft("Term 1").name, "Term 1")

    def test_an_item_is_stored_in_whole_kobo_with_its_target(self):
        schedule = self.draft()
        item = self.item(schedule, code="TUITION", amount=12_500_000, scope="class", academic_class=self.primary)
        self.assertEqual((item.amount_minor, item.currency, item.scope, item.academic_class), (12_500_000, "NGN", "class", self.primary))

    def test_money_must_be_a_whole_positive_number_of_kobo(self):
        schedule = self.draft()
        for amount in (0, -1, 10.5, "100", True, None, 10**12):
            with self.assertRaises(Refused, msg=repr(amount)) as raised:
                self.item(schedule, amount=amount)
            self.assertEqual(raised.exception.code, "invalid_amount")
        self.assertFalse(FeeItem.objects.exists())

    def test_the_other_fields_are_checked(self):
        schedule = self.draft()
        cases = [
            ({"code": ""}, "invalid_code"), ({"code": "has space"}, "invalid_code"), ({"code": "x" * 41}, "invalid_code"),
            ({"name": ""}, "name_required"), ({"category": "gold"}, "invalid_category"),
            ({"is_mandatory": "yes"}, "invalid_mandatory"), ({"sort_order": -1}, "invalid_order"),
            ({"due_date": "31 Oct"}, "invalid_due_date"), ({"bogus": 1}, "unexpected_field"),
        ]
        for over, code in cases:
            with self.assertRaises(Refused, msg=str(over)) as raised:
                self.item(schedule, **over)
            self.assertEqual(raised.exception.code, code, over)

    def test_a_scope_must_match_what_it_names(self):
        schedule = self.draft()
        stranger_class = self.make_year(self.other_school)[2]
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        student = self.make_student("Ahmad", "Bello")
        bad = [
            dict(scope="all", section="Primary"), dict(scope="section"), dict(scope="section", section="Astronomy"),
            dict(scope="class"), dict(scope="class", academic_class=stranger_class), dict(scope="student"),
            dict(scope="student", student=stranger), dict(scope="class", academic_class=self.primary, section="Primary"),
            dict(scope="nonsense"),
        ]
        for over in bad:
            with self.assertRaises(Refused, msg=str(over)):
                self.item(schedule, **over)
        self.item(schedule, code="A", scope="section", section="primary")  # a section matches without regard to case
        self.item(schedule, code="B", scope="student", student=student)

    def test_an_optional_item_can_only_be_aimed_at_students(self):
        schedule = self.draft()
        student = self.make_student("Ahmad", "Bello")
        with self.assertRaises(Refused) as raised:
            self.item(schedule, code="BUS", is_mandatory=False)
        self.assertEqual(raised.exception.code, "optional_needs_student")
        self.assertFalse(self.item(schedule, code="BUS", is_mandatory=False, scope="student", student=student).is_mandatory)

    def test_an_item_code_is_unique_within_a_schedule(self):
        schedule = self.draft()
        self.item(schedule, code="TUITION")
        with self.assertRaises(Refused) as raised:
            self.item(schedule, code="TUITION")
        self.assertEqual(raised.exception.code, "item_exists")
        self.item(self.draft("Other"), code="TUITION")  # but another schedule may reuse it

    def test_a_draft_item_can_be_changed_and_removed_and_it_is_audited(self):
        schedule = self.draft()
        item = self.item(schedule)
        schedules.update_item(item, actor=self.owner, amount_minor=13_000_000, name="Tuition fee")
        item.refresh_from_db()
        self.assertEqual((item.amount_minor, item.name), (13_000_000, "Tuition fee"))
        event = FinanceAuditEvent.objects.get(kind="fee_item_changed")
        self.assertEqual((event.detail["before"]["amount"], event.detail["after"]["amount"]), (12_500_000, 13_000_000))
        schedules.remove_item(item, actor=self.owner)
        self.assertFalse(FeeItem.objects.exists())
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="fee_item_removed").exists())

    def test_the_database_holds_the_line_on_money_and_scope(self):
        schedule = self.draft()
        with self.assertRaises(IntegrityError), transaction.atomic():
            FeeItem.objects.bulk_create([FeeItem(school=self.school, schedule=schedule, code="X", name="X", amount_minor=0)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            FeeItem.objects.bulk_create([FeeItem(school=self.school, schedule=schedule, code="Y", name="Y", amount_minor=5, scope="class")])


class PlanTests(ScheduleTestCase):
    def test_the_parts_always_add_up_to_exactly_the_whole(self):
        for amount in (100, 12_500_001, 1_000_003, 3, 99_999_999_999):
            for shares in ([5000, 5000], [3334, 3333, 3333], [1, 9999], [2500] * 4):
                parts = plans.split(amount, shares)
                self.assertEqual(sum(parts), amount, (amount, shares))
                self.assertEqual(len(parts), len(shares))
        self.assertEqual(plans.split(100_001, [3333, 3333, 3334]), [33_330, 33_330, 33_341])

    def test_a_plan_is_cleaned_sorted_and_must_add_up(self):
        cleaned = plans.clean_plan([{"basisPoints": 4000, "dueDate": "2026-11-30"}, {"basisPoints": 6000, "dueDate": "2026-09-30"}])
        self.assertEqual([p["dueDate"] for p in cleaned], ["2026-09-30", "2026-11-30"])
        for bad in (
            [{"basisPoints": 5000, "dueDate": "2026-09-30"}], [{"basisPoints": 5000, "dueDate": "2026-09-30"}] * 2 + [{"basisPoints": 1, "dueDate": "2026-09-30"}],
            [{"basisPoints": 5000, "dueDate": "nope"}, {"basisPoints": 5000, "dueDate": "2026-09-30"}],
            [{"basisPoints": 5000.0, "dueDate": "2026-09-30"}, {"basisPoints": 5000, "dueDate": "2026-09-30"}],
            [{"basisPoints": True, "dueDate": "2026-09-30"}] * 2, "monthly", [1, 2], [{"basisPoints": 5000, "dueDate": "2026-09-30"}] * 13,
        ):
            with self.assertRaises(Refused, msg=repr(bad)):
                plans.clean_plan(bad)

    def test_an_amount_too_small_to_split_is_refused(self):
        schedule = self.draft()
        with self.assertRaises(Refused):
            self.item(schedule, amount=1, plan=[{"basisPoints": 5000, "dueDate": "2026-09-30"}, {"basisPoints": 5000, "dueDate": "2026-10-30"}])

    def test_an_item_with_a_plan_needs_no_single_due_date(self):
        schedule = self.draft()
        self.item(schedule, due_date=None, plan=[{"basisPoints": 5000, "dueDate": "2026-09-30"}, {"basisPoints": 5000, "dueDate": "2026-11-30"}])
        self.assertEqual(schedules.problems(schedule), [])


class PublishGateTests(ScheduleTestCase):
    def test_an_empty_schedule_cannot_be_published(self):
        with self.assertRaises(Refused) as raised:
            schedules.publish(self.draft(), actor=self.owner)
        self.assertEqual(raised.exception.code, "not_ready")

    def test_an_item_with_no_due_date_stops_publication(self):
        schedule = self.draft()
        self.item(schedule, due_date=None)
        with self.assertRaises(Refused) as raised:
            schedules.publish(schedule, actor=self.owner)
        self.assertIn("no due date", raised.exception.message)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, "draft")

    def test_a_schedule_is_published_once(self):
        schedule = self.draft()
        self.item(schedule)
        schedules.publish(schedule, actor=self.owner)
        with self.assertRaises(Refused) as raised:
            schedules.publish(schedule, actor=self.owner)
        self.assertEqual(raised.exception.code, "already_published")

    def test_publishing_records_who_and_when(self):
        schedule = self.draft()
        self.item(schedule)
        schedules.publish(schedule, actor=self.authority)
        schedule.refresh_from_db()
        self.assertEqual((schedule.status, schedule.published_by), ("published", self.authority))
        self.assertIsNotNone(schedule.published_at)
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="fee_schedule_published", actor=self.authority).exists())

    def test_a_published_schedule_is_frozen(self):
        schedule = self.draft()
        item = self.item(schedule)
        schedules.publish(schedule, actor=self.owner)
        for call in (
            lambda: schedules.add_item(schedule, actor=self.owner, code="N", name="N", amount_minor=100, due_date=DUE),
            lambda: schedules.update_item(item, actor=self.owner, amount_minor=1),
            lambda: schedules.remove_item(item, actor=self.owner),
            lambda: schedules.rename_schedule(schedule, "New", actor=self.owner),
        ):
            with self.assertRaises(Refused) as raised:
                call()
            self.assertEqual(raised.exception.code, "schedule_not_draft")
        item.amount_minor = 1
        with self.assertRaises(ValidationError):  # and the model itself refuses if a service is bypassed
            item.save()


class MaterialisationTests(ScheduleTestCase):
    def setUp(self):
        super().setUp()
        self.ahmad = self.pupil("Ahmad", "Bello", self.primary)
        self.aisha = self.pupil("Aisha", "Sani", self.primary)
        self.bala = self.pupil("Bala", "Musa", self.jss)

    def receivables(self, **filters):
        return StudentReceivable.objects.filter(**filters)

    def publish(self, *items):
        schedule = self.draft()
        for code, over in items:
            self.item(schedule, code=code, **over)
        return schedule, schedules.publish(schedule, actor=self.owner)

    def test_a_fee_for_everyone_charges_every_placed_billable_student_once(self):
        schedule, report = self.publish(("TUITION", {}))
        self.assertEqual((report.created, len(report.families)), (3, 3))
        self.assertEqual({r.student_id for r in self.receivables()}, {self.ahmad.id, self.aisha.id, self.bala.id})
        r = self.receivables(student=self.ahmad).get()
        self.assertEqual(
            (r.gross_amount_minor, r.due_date, r.status, r.item_code, r.item_name, r.item_category, r.session, r.term, r.installment_number, r.installment_count),
            (12_500_000, DUE, "open", "TUITION", "Tuition", "tuition", self.session, self.term, 1, 1),
        )
        self.assertEqual((r.family, r.published_by, r.school), (families.family_of(self.ahmad), self.owner, self.school))

    def test_a_class_fee_and_a_section_fee_reach_only_their_own_students(self):
        self.publish(("LAB", {"scope": "class", "academic_class": self.primary, "amount": 500_000}),
                     ("SEC", {"scope": "section", "section": "Secondary", "amount": 900_000}))
        self.assertEqual({r.student_id for r in self.receivables(item_code="LAB")}, {self.ahmad.id, self.aisha.id})
        self.assertEqual({r.student_id for r in self.receivables(item_code="SEC")}, {self.bala.id})

    def test_a_student_specific_fee_and_an_optional_service(self):
        self.publish(("BUS", {"scope": "student", "student": self.ahmad, "is_mandatory": False, "amount": 300_000}))
        self.assertEqual([r.student_id for r in self.receivables()], [self.ahmad.id])

    def test_students_who_have_left_or_are_not_billable_are_not_charged(self):
        gone = self.pupil("Old", "Boy", self.primary, status="graduated")
        free = self.make_student("Free", "Rider")
        self.enroll(free, self.primary, self.session, billable=False)
        families.ensure_family_for_student(self.school, free)
        self.publish(("TUITION", {}))
        self.assertFalse(self.receivables(student=gone).exists())
        self.assertFalse(self.receivables(student=free).exists())

    def test_a_student_with_no_class_for_the_session_is_reported_not_guessed_at(self):
        floating = self.make_student("Lost", "Child")
        families.ensure_family_for_student(self.school, floating)
        from apps.students.models import StudentEnrollment
        from django.utils import timezone
        StudentEnrollment.objects.create(school=self.school, student=floating, academic_section="Primary", class_name="A class nobody set up", status="active", is_billable=True, started_at=timezone.now())
        _, report = self.publish(("TUITION", {}), ("LAB", {"scope": "class", "academic_class": self.primary, "amount": 500_000}))
        self.assertEqual([s.id for s in report.unclassified], [floating.id])
        self.assertFalse(self.receivables(student=floating).exists())
        self.assertFalse(report.complete)

    def test_a_student_with_no_family_is_reported_and_charged_once_placed(self):
        orphan = self.pupil("Ola", "Alone", self.primary, family=False)
        schedule, report = self.publish(("TUITION", {}))
        self.assertEqual([s.id for s in report.without_family], [orphan.id])
        self.assertFalse(self.receivables(student=orphan).exists())
        families.ensure_family_for_student(self.school, orphan)
        again = schedules.refresh(schedule, actor=self.owner)
        self.assertEqual((again.created, again.without_family), (1, []))
        self.assertTrue(self.receivables(student=orphan).exists())

    def test_publishing_and_refreshing_are_idempotent(self):
        schedule, report = self.publish(("TUITION", {}), ("LAB", {"amount": 500_000}))
        self.assertEqual(report.created, 6)
        for _ in range(3):
            again = schedules.refresh(schedule, actor=self.owner)
            self.assertEqual((again.created, again.already_existed), (0, 6))
        self.assertEqual(self.receivables().count(), 6)

    def test_running_the_charging_step_twice_at_once_cannot_double_charge(self):
        schedule, _ = self.publish(("TUITION", {}))
        schedules._materialise(schedule, self.owner)
        schedules._materialise(schedule, self.owner)
        self.assertEqual(self.receivables().count(), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentReceivable.objects.create(
                school=self.school, student=self.ahmad, family=families.family_of(self.ahmad), schedule=schedule,
                fee_item=schedule.items.get(), session=self.session, item_code="TUITION", item_name="T", item_category="tuition",
                gross_amount_minor=1, due_date=DUE,
            )

    def test_a_student_who_joins_later_is_charged_on_refresh_and_nobody_else_changes(self):
        schedule, _ = self.publish(("TUITION", {}))
        before = {r.id: (r.gross_amount_minor, r.due_date) for r in self.receivables()}
        newcomer = self.pupil("New", "Pupil", self.primary)
        report = schedules.refresh(schedule, actor=self.owner)
        self.assertEqual(report.created, 1)
        self.assertEqual({r.id: (r.gross_amount_minor, r.due_date) for r in self.receivables() if r.id in before}, before)
        self.assertTrue(self.receivables(student=newcomer).exists())

    def test_instalments_are_separate_charges_that_share_a_key_and_add_up(self):
        plan = [{"basisPoints": 5000, "dueDate": "2026-09-30"}, {"basisPoints": 3000, "dueDate": "2026-11-30"}, {"basisPoints": 2000, "dueDate": "2027-01-31"}]
        # The last instalment falls after First Term ends, so this is a schedule for the whole session.
        schedule = schedules.create_schedule(self.school, session=self.session, term=None, name="Session 2026", actor=self.owner)
        self.item(schedule, amount=12_500_001, due_date=None, plan=plan)
        schedules.publish(schedule, actor=self.owner)
        rows = list(self.receivables(student=self.ahmad).order_by("installment_number"))
        self.assertEqual([r.installment_number for r in rows], [1, 2, 3])
        self.assertEqual([r.installment_count for r in rows], [3, 3, 3])
        self.assertEqual([r.due_date.isoformat() for r in rows], ["2026-09-30", "2026-11-30", "2027-01-31"])
        self.assertEqual(sum(r.gross_amount_minor for r in rows), 12_500_001)
        self.assertEqual(len({r.charge_key for r in rows}), 1)
        self.assertNotEqual(rows[0].charge_key, self.receivables(student=self.aisha).first().charge_key)

    def test_a_charge_never_changes_after_it_is_made(self):
        self.publish(("TUITION", {}))
        r = self.receivables(student=self.ahmad).get()
        for field, value in (("gross_amount_minor", 1), ("due_date", date(2030, 1, 1)), ("student", self.aisha), ("currency", "USD")):
            setattr(r, field, value)
            with self.assertRaises(ValidationError, msg=field):
                r.save()
            r.refresh_from_db()
        r.status = "partially_paid"  # its status, being derived from the ledger, may change
        r.save()
        with self.assertRaises(ValidationError):
            r.delete()

    def test_a_student_at_another_school_is_never_charged_by_this_schedule(self):
        other_session, _, other_primary, _ = self.make_year(self.other_school)
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        self.enroll(stranger, other_primary, other_session)
        families.ensure_family_for_student(self.other_school, stranger)
        self.publish(("TUITION", {}))
        self.assertFalse(StudentReceivable.objects.filter(student=stranger).exists())
        self.assertFalse(StudentReceivable.objects.exclude(school=self.school).exists())

    def test_the_family_and_student_and_school_must_agree(self):
        schedule, _ = self.publish(("TUITION", {}))
        other_family = families.create_family(self.other_school, display_name="Elsewhere")
        with self.assertRaises(ValidationError):
            StudentReceivable(
                school=self.school, student=self.ahmad, family=other_family, schedule=schedule, fee_item=schedule.items.get(),
                session=self.session, item_code="X", item_name="X", item_category="tuition", gross_amount_minor=5, due_date=DUE,
            ).save()


class CorrectionTests(ScheduleTestCase):
    def test_retiring_needs_a_reason_and_keeps_every_charge(self):
        student = self.pupil("Ahmad", "Bello", self.primary)
        schedule = self.draft()
        self.item(schedule)
        schedules.publish(schedule, actor=self.owner)
        with self.assertRaises(Refused):
            schedules.retire(schedule, actor=self.owner, reason="  ")
        schedules.retire(schedule, actor=self.owner, reason="Fees were set for the wrong term")
        schedule.refresh_from_db()
        self.assertEqual((schedule.status, schedule.retired_by, schedule.retire_reason), ("retired", self.owner, "Fees were set for the wrong term"))
        self.assertTrue(StudentReceivable.objects.filter(student=student).exists())
        with self.assertRaises(Refused):
            schedules.retire(schedule, actor=self.owner, reason="again")
        with self.assertRaises(Refused):
            schedules.refresh(schedule, actor=self.owner)

    def test_a_clone_is_a_new_draft_that_charges_nothing_until_published(self):
        schedule = self.draft()
        self.item(schedule, code="A")
        self.item(schedule, code="B", amount=200_000)
        copy = schedules.clone(schedule, actor=self.owner)
        self.assertEqual((copy.status, copy.replaces, copy.items.count()), ("draft", schedule, 2))
        self.assertEqual({i.amount_minor for i in copy.items.all()}, {12_500_000, 200_000})
        self.assertFalse(StudentReceivable.objects.exists())

    def test_a_replacement_cannot_be_published_while_the_original_is_live(self):
        schedule = self.draft()
        self.item(schedule)
        schedules.publish(schedule, actor=self.owner)
        copy = schedules.clone(schedule, actor=self.owner)
        with self.assertRaises(Refused) as raised:
            schedules.publish(copy, actor=self.owner)
        self.assertEqual(raised.exception.code, "replaced_not_retired")

    def test_a_replacement_never_charges_a_student_twice_for_an_item_already_charged(self):
        student = self.pupil("Ahmad", "Bello", self.primary)
        schedule = self.draft()
        self.item(schedule, code="TUITION")
        schedules.publish(schedule, actor=self.owner)
        schedules.retire(schedule, actor=self.owner, reason="Amount was wrong")
        copy = schedules.clone(schedule, actor=self.owner, name="Corrected")
        schedules.update_item(copy.items.get(), actor=self.owner, amount_minor=11_000_000)
        newcomer = self.pupil("New", "Pupil", self.primary)
        report = schedules.publish(copy, actor=self.owner)
        self.assertEqual(report.already_charged_by_replaced, 1)
        self.assertEqual(StudentReceivable.objects.filter(student=student).count(), 1)  # not charged again
        self.assertEqual(StudentReceivable.objects.get(student=newcomer).gross_amount_minor, 11_000_000)
