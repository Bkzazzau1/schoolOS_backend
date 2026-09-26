"""The fee system follows the school's canonical sessions and terms.

A fee schedule belongs to a session (and usually a term) of the academics app; a closed period is never billed;
a student owes nothing for a term that began before they were enrolled; a due date falls within the period it is
for; and what is owed can be read term by term, with what is left from an ended term called arrears.
"""

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.utils import timezone

from apps.academics.models import AcademicSession, AcademicTerm, EnrollmentAcademicContext
from apps.students.models import StudentEnrollment

from .. import adjustments, allocation, families, ledger, periods, reports, schedules, statements
from ..errors import Refused
from ..models import StudentReceivable
from . import test_api
from .base import CLOCK, ReceivablesTestCase

N = 100


class CalendarTestCase(ReceivablesTestCase):
    """The base year (2026/2027, First Term active) with the rest of the session's terms planned."""

    def setUp(self):
        super().setUp()
        self.session, self.term, self.primary, self.jss = self.make_year()
        self.term2 = AcademicTerm.objects.create(
            session=self.session, code="T2", name="Second Term", sequence=2, starts_on=date(2027, 1, 11), ends_on=date(2027, 4, 2), status="planned",
        )
        self.term3 = AcademicTerm.objects.create(
            session=self.session, code="T3", name="Third Term", sequence=3, starts_on=date(2027, 4, 26), ends_on=date(2027, 7, 20), status="planned",
        )
        self.authority = self.members["principal"]
        self.give_duty(self.authority)

    def close(self, *rows):
        for row in rows:  # closing is the academics app's to do; here only its outcome matters
            type(row).objects.filter(pk=row.pk).update(status="closed")
            row.refresh_from_db()

    def pupil(self, first, surname, academic_class=None, *, entry_term=..., started=None):
        student = self.make_student(first, surname)
        enrollment = self.enroll(student, academic_class or self.primary, self.session)
        if entry_term is not ...:  # otherwise whatever the academics app recorded
            EnrollmentAcademicContext.objects.filter(enrollment=enrollment).update(entry_term=entry_term)
        if started is not None:
            StudentEnrollment.objects.filter(pk=enrollment.pk).update(started_at=started)
        families.ensure_family_for_student(self.school, student)
        return student

    def fees(self, term, *, due, amount=50_000 * N, name=None, code="TUITION", **item):
        schedule = schedules.create_schedule(
            self.school, session=self.session, term=term, name=name or f"{term.name if term else 'Whole session'} fees", actor=self.owner,
        )
        schedules.add_item(schedule, actor=self.owner, code=code, name="Tuition", category="tuition", amount_minor=amount, due_date=due, **item)
        return schedule

    def charged(self, student):
        return StudentReceivable.objects.filter(student=student)


class PeriodRulesTests(CalendarTestCase):
    def test_the_current_period_is_the_academics_apps_active_session_and_term(self):
        self.assertEqual(periods.current_period(self.school), (self.session, self.term))
        self.assertEqual(periods.current_period(self.other_school), (None, None))

    def test_a_period_has_a_label_and_a_window(self):
        self.assertEqual(periods.label(self.session, self.term), "2026/2027 · First Term")
        self.assertEqual(periods.label(self.session), "2026/2027")
        self.assertEqual(periods.window(self.session, self.term), (date(2026, 9, 7), date(2026, 12, 18)))
        self.assertEqual(periods.window(self.session), (date(2026, 9, 7), date(2027, 7, 20)))

    def test_a_closed_term_or_session_is_closed_and_past(self):
        self.assertFalse(periods.is_closed(self.session, self.term))
        self.close(self.term)
        self.assertTrue(periods.is_closed(self.session, self.term))
        self.assertTrue(periods.is_past(self.session, self.term, CLOCK))
        self.assertFalse(periods.is_closed(self.session, self.term2))
        self.close(self.session)
        self.assertTrue(periods.is_closed(self.session, self.term2))

    def test_a_period_is_past_once_its_last_day_has_gone(self):
        self.assertFalse(periods.is_past(self.session, self.term, date(2026, 12, 18)))
        self.assertTrue(periods.is_past(self.session, self.term, date(2026, 12, 19)))
        self.assertFalse(periods.is_past(self.session, self.term2, date(2026, 12, 19)))

    def test_the_clock_the_fee_system_reads_is_the_one_place_it_can_be_moved(self):
        self.assertEqual(periods.school_today(), CLOCK)
        self.set_today(date(2027, 1, 12))
        self.assertTrue(periods.is_past(self.session, self.term))
        self.assertFalse(periods.is_past(self.session, self.term2))

    def test_a_due_date_belongs_inside_its_period_with_some_lead(self):
        term = self.term2  # 11 Jan - 2 Apr 2027
        self.assertIsNone(periods.due_date_problem(date(2027, 2, 1), self.session, term))
        self.assertIsNone(periods.due_date_problem(date(2027, 4, 2), self.session, term))
        self.assertIn("after the end", periods.due_date_problem(date(2027, 4, 3), self.session, term))
        lead = periods.DUE_LEAD_DAYS
        self.assertIsNone(periods.due_date_problem(term.starts_on - timedelta(days=lead), self.session, term))
        self.assertIn("days before", periods.due_date_problem(term.starts_on - timedelta(days=lead + 1), self.session, term))

    def test_a_whole_session_schedule_may_be_due_any_time_in_the_session(self):
        self.assertIsNone(periods.due_date_problem(date(2027, 7, 20), self.session))
        self.assertIsNotNone(periods.due_date_problem(date(2027, 7, 21), self.session))

    def test_a_student_who_entered_in_a_later_term_joined_after_an_earlier_one(self):
        ada = self.pupil("Ada", "Eze", entry_term=self.term2)
        context = EnrollmentAcademicContext.objects.get(enrollment__student=ada)
        self.assertTrue(periods.joined_after(context, self.term))
        self.assertFalse(periods.joined_after(context, self.term2))
        self.assertFalse(periods.joined_after(context, self.term3))
        self.assertFalse(periods.joined_after(context, None))  # a whole-session fee is owed by everyone in it

    def test_with_no_entry_term_the_day_the_enrolment_began_decides(self):
        ada = self.pupil("Ada", "Eze", entry_term=None, started=timezone.make_aware(datetime.combine(date(2027, 1, 20), time(10, 0))))
        context = EnrollmentAcademicContext.objects.select_related("enrollment").get(enrollment__student=ada)
        self.assertTrue(periods.joined_after(context, self.term))
        self.assertFalse(periods.joined_after(context, self.term2))
        early = self.pupil("Bola", "Eze", entry_term=None)  # enrolled before the year began
        context = EnrollmentAcademicContext.objects.select_related("enrollment").get(enrollment__student=early)
        self.assertFalse(periods.joined_after(context, self.term))


class DefaultsTests(CalendarTestCase):
    def test_a_schedule_with_no_period_named_is_for_the_current_session_and_term(self):
        schedule = schedules.create_for_current_period(self.school, name="Tuition", actor=self.owner)
        self.assertEqual((schedule.session, schedule.term), (self.session, self.term))

    def test_or_for_the_whole_of_the_current_session(self):
        schedule = schedules.create_for_current_period(self.school, name="Levy", actor=self.owner, whole_session=True)
        self.assertEqual((schedule.session, schedule.term), (self.session, None))

    def test_a_school_with_no_active_session_is_told_to_choose_one(self):
        AcademicSession.objects.filter(pk=self.session.pk).update(status="planned")
        with self.assertRaises(Refused) as caught:
            schedules.create_for_current_period(self.school, name="Tuition", actor=self.owner)
        self.assertEqual(caught.exception.code, "no_active_session")

    def test_a_session_between_terms_asks_for_a_term_or_the_whole_session(self):
        AcademicTerm.objects.filter(pk=self.term.pk).update(status="closed")
        with self.assertRaises(Refused) as caught:
            schedules.create_for_current_period(self.school, name="Tuition", actor=self.owner)
        self.assertEqual(caught.exception.code, "no_active_term")
        self.assertEqual(schedules.create_for_current_period(self.school, name="Tuition", actor=self.owner, whole_session=True).term, None)

    def test_only_a_billing_authority_may_make_one(self):
        with self.assertRaises(Refused):
            schedules.create_for_current_period(self.school, name="Tuition", actor=self.members["accountant"])


class ClosedPeriodTests(CalendarTestCase):
    def test_a_closed_term_cannot_be_given_a_schedule(self):
        self.close(self.term)
        with self.assertRaises(Refused) as caught:
            schedules.create_schedule(self.school, session=self.session, term=self.term, name="Late", actor=self.owner)
        self.assertEqual(caught.exception.code, "period_closed")

    def test_nor_can_a_term_of_a_closed_session(self):
        self.close(self.session)
        with self.assertRaises(Refused) as caught:
            schedules.create_schedule(self.school, session=self.session, term=self.term2, name="Late", actor=self.owner)
        self.assertEqual(caught.exception.code, "period_closed")

    def test_a_draft_cannot_be_published_once_its_term_has_closed(self):
        self.pupil("Ada", "Eze")
        draft = self.fees(self.term, due=CLOCK + timedelta(days=10))
        self.close(self.term)
        with self.assertRaises(Refused) as caught:
            schedules.publish(draft, actor=self.owner)
        self.assertEqual(caught.exception.code, "period_closed")
        self.assertIn("closed", " ".join(schedules.problems(draft)))
        self.assertFalse(StudentReceivable.objects.exists())

    def test_a_published_schedule_of_a_closed_term_charges_nobody_new(self):
        first = self.pupil("Ada", "Eze")
        schedule = self.fees(self.term, due=CLOCK + timedelta(days=10))
        schedules.publish(schedule, actor=self.owner)
        self.close(self.term)
        latecomer = self.pupil("Bola", "Eze")
        with self.assertRaises(Refused) as caught:
            schedules.refresh(schedule, actor=self.owner)
        self.assertEqual(caught.exception.code, "period_closed")
        self.assertFalse(self.charged(latecomer).exists())
        self.assertEqual(self.charged(first).count(), 1)

    def test_what_was_already_charged_stays_a_live_debt_that_can_still_be_paid_and_adjusted(self):
        ada = self.pupil("Ada", "Eze")
        schedules.publish(self.fees(self.term, due=CLOCK + timedelta(days=10)), actor=self.owner)
        family = families.family_of(ada)
        self.close(self.term)
        charge = self.charged(ada).get()
        adjustments.adjust(charge, kind="discount", amount_minor=10_000 * N, reason="Late concession", actor=self.owner)
        allocation.allocate(self.payment(30_000 * N), family)
        position = ledger.family_position(family)
        self.assertEqual((position.net, position.paid, position.outstanding), (40_000 * N, 30_000 * N, 10_000 * N))
        self.assertEqual(ledger.verify_family(family), [])

    def test_the_rest_of_the_session_can_still_be_billed(self):
        self.close(self.term)
        self.pupil("Ada", "Eze")
        schedule = self.fees(self.term2, due=date(2027, 1, 20))
        self.assertEqual(schedules.publish(schedule, actor=self.owner).created, 1)


class JoinedLaterTests(CalendarTestCase):
    def setUp(self):
        super().setUp()
        self.veteran = self.pupil("Ada", "Eze")  # here from the start of the year
        self.newcomer = self.pupil("Bola", "Ojo", entry_term=self.term2)

    def test_a_student_who_entered_in_term_two_owes_nothing_for_term_one(self):
        schedules.publish(self.fees(self.term, due=CLOCK + timedelta(days=10)), actor=self.owner)
        self.assertTrue(self.charged(self.veteran).exists())
        self.assertFalse(self.charged(self.newcomer).exists())

    def test_but_is_charged_for_the_term_they_entered_and_later_ones(self):
        schedules.publish(self.fees(self.term2, due=date(2027, 1, 20)), actor=self.owner)
        schedules.publish(self.fees(self.term3, due=date(2027, 5, 3)), actor=self.owner)
        self.assertEqual(self.charged(self.newcomer).count(), 2)
        self.assertEqual({r.term_id for r in self.charged(self.newcomer)}, {self.term2.id, self.term3.id})

    def test_the_publish_report_and_the_preview_say_who_was_left_out_and_why(self):
        schedule = self.fees(self.term, due=CLOCK + timedelta(days=10))
        preview = schedules.preview(schedule)
        self.assertEqual([s.id for s in preview["joinedLater"]], [self.newcomer.id])
        self.assertEqual(preview["unclassified"], [])
        report = schedules.publish(schedule, actor=self.owner)
        self.assertEqual(report.created, 1)
        self.assertEqual([s.id for s in report.joined_later], [self.newcomer.id])
        self.assertEqual(report.unclassified, [])
        self.assertTrue(report.complete)  # nobody is missing: they simply were not there yet

    def test_a_refresh_does_not_pick_them_up_for_a_term_that_began_before_them(self):
        schedule = self.fees(self.term, due=CLOCK + timedelta(days=10))
        schedules.publish(schedule, actor=self.owner)
        self.assertEqual(schedules.refresh(schedule, actor=self.owner).created, 0)
        self.assertFalse(self.charged(self.newcomer).exists())

    def test_a_fee_for_the_whole_session_is_owed_by_everyone_in_it(self):
        schedules.publish(self.fees(None, due=date(2027, 2, 1), name="Session levy", code="LEVY"), actor=self.owner)
        self.assertTrue(self.charged(self.veteran).exists())
        self.assertTrue(self.charged(self.newcomer).exists())

    def test_a_fee_aimed_at_one_named_student_is_theirs_whenever_they_joined(self):
        # The person choosing a student by name has said so: this is how a late arrival is charged for a past term.
        schedules.publish(
            self.fees(self.term, due=CLOCK + timedelta(days=10), code="CATCHUP", scope="student", student=self.newcomer), actor=self.owner,
        )
        self.assertEqual(self.charged(self.newcomer).count(), 1)

    def test_where_no_entry_term_was_recorded_the_enrolment_date_decides(self):
        late = self.pupil("Chidi", "Ude", entry_term=None, started=timezone.make_aware(datetime.combine(date(2027, 1, 20), time(10, 0))))
        schedules.publish(self.fees(self.term, due=CLOCK + timedelta(days=10)), actor=self.owner)
        self.assertFalse(self.charged(late).exists())
        schedules.publish(self.fees(self.term2, due=date(2027, 1, 25), code="T2"), actor=self.owner)
        self.assertTrue(self.charged(late).exists())


class DueDateWindowTests(CalendarTestCase):
    def draft(self, term=None, session=None):
        return schedules.create_schedule(self.school, session=session or self.session, term=term, name="Fees", actor=self.owner)

    def add(self, schedule, due=None, **over):
        fields = dict(code="TUITION", name="Tuition", category="tuition", amount_minor=50_000 * N, due_date=due)
        fields.update(over)
        return schedules.add_item(schedule, actor=self.owner, **fields)

    def test_a_due_date_after_the_term_ends_is_refused(self):
        with self.assertRaises(Refused) as caught:
            self.add(self.draft(self.term), date(2026, 12, 19))
        self.assertEqual(caught.exception.code, "due_date_outside_period")
        self.assertIn("2026/2027 · First Term", str(caught.exception))

    def test_a_due_date_long_before_the_term_begins_is_refused(self):
        with self.assertRaises(Refused) as caught:
            self.add(self.draft(self.term2), date(2026, 10, 1))
        self.assertEqual(caught.exception.code, "due_date_outside_period")

    def test_a_due_date_a_little_before_the_term_is_normal_and_allowed(self):
        self.add(self.draft(self.term2), date(2026, 12, 15))

    def test_every_instalment_is_held_to_the_same_window(self):
        plan = [{"basisPoints": 6000, "dueDate": "2026-10-01"}, {"basisPoints": 4000, "dueDate": "2027-01-15"}]
        with self.assertRaises(Refused) as caught:
            self.add(self.draft(self.term), plan=plan)
        self.assertEqual(caught.exception.code, "due_date_outside_period")
        self.add(self.draft(None), plan=plan)  # the whole session takes both

    def test_changing_an_item_is_held_to_it_too(self):
        item = self.add(self.draft(self.term), date(2026, 10, 1))
        with self.assertRaises(Refused):
            schedules.update_item(item, actor=self.owner, due_date=date(2027, 3, 1))
        schedules.update_item(item, actor=self.owner, due_date=date(2026, 11, 1))

    def test_a_schedule_whose_term_moved_is_stopped_from_publishing_with_the_reason(self):
        schedule = self.draft(self.term)
        self.add(schedule, date(2026, 12, 1))
        AcademicTerm.objects.filter(pk=self.term.pk).update(ends_on=date(2026, 11, 20))
        schedule.refresh_from_db()
        found = schedules.problems(schedule)
        self.assertEqual(len(found), 1)
        self.assertIn("after the end", found[0])
        with self.assertRaises(Refused) as caught:
            schedules.publish(schedule, actor=self.owner)
        self.assertEqual(caught.exception.code, "not_ready")


class ReportTests(CalendarTestCase):
    """Two children owing for First Term and Second Term, one of them having paid part of First Term."""

    def setUp(self):
        super().setUp()
        self.ada = self.pupil("Ada", "Eze")
        self.bola = self.pupil("Bola", "Ojo")
        self.family_ada, self.family_bola = families.family_of(self.ada), families.family_of(self.bola)
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=100_000 * N), actor=self.owner)
        schedules.publish(self.fees(self.term2, due=date(2027, 1, 20), amount=80_000 * N), actor=self.owner)
        allocation.allocate(self.payment(60_000 * N), self.family_ada)
        adjustments.adjust(self.charged(self.bola).get(term=self.term), kind="discount", amount_minor=10_000 * N, reason="Sibling", actor=self.owner)

    def line(self, term, report=None):
        report = report or reports.by_term(self.school)
        return next(p for p in report["periods"] if p["termId"] == str(term.id))

    def test_what_is_owed_is_reported_term_by_term_in_calendar_order(self):
        report = reports.by_term(self.school)
        self.assertEqual([p["termName"] for p in report["periods"]], ["First Term", "Second Term"])
        first = self.line(self.term, report)
        self.assertEqual(
            (first["charges"], first["grossMinor"], first["adjustmentsMinor"], first["netMinor"], first["paidMinor"], first["outstandingMinor"]),
            (2, 200_000 * N, 10_000 * N, 190_000 * N, 60_000 * N, 130_000 * N),
        )
        second = self.line(self.term2, report)
        self.assertEqual((second["grossMinor"], second["paidMinor"], second["outstandingMinor"]), (160_000 * N, 0, 160_000 * N))
        self.assertEqual(report["totals"]["outstandingMinor"], 290_000 * N)
        self.assertEqual(report["totals"]["grossMinor"], 360_000 * N)

    def test_each_line_says_who_and_how_much_of_what_was_payable_has_been_collected(self):
        first = self.line(self.term)
        self.assertEqual((first["students"], first["families"], first["familiesOwing"]), (2, 2, 2))
        self.assertEqual(first["collectionRateBp"], 60_000 * N * 10_000 // (190_000 * N))
        self.assertEqual(self.line(self.term2)["collectionRateBp"], 0)

    def test_a_family_that_has_paid_a_term_in_full_is_no_longer_counted_as_owing_it(self):
        allocation.allocate(self.payment(40_000 * N), self.family_ada)
        first = self.line(self.term)
        self.assertEqual((first["familiesOwing"], first["outstandingMinor"]), (1, 90_000 * N))

    def test_the_first_term_is_current_and_owes_nothing_in_arrears_while_it_runs(self):
        first = self.line(self.term)
        self.assertEqual((first["isCurrent"], first["isPast"], first["isClosed"]), (True, False, False))
        self.assertEqual(reports.school_position(self.school)["arrearsMinor"], 0)
        self.assertEqual(reports.school_position(self.school)["currentMinor"], 290_000 * N)

    def test_what_is_left_of_a_term_that_has_ended_is_arrears(self):
        self.set_today(date(2027, 1, 12))
        position = reports.school_position(self.school)
        self.assertEqual((position["arrearsMinor"], position["currentMinor"]), (130_000 * N, 160_000 * N))
        first, second = self.line(self.term), self.line(self.term2)
        self.assertEqual((first["isPast"], first["isCurrent"], second["isCurrent"]), (True, False, True))
        family = ledger.family_position(self.family_ada)
        self.assertEqual((family.arrears, family.current, family.outstanding), (40_000 * N, 80_000 * N, 120_000 * N))

    def test_a_term_the_school_has_closed_counts_as_past_even_before_its_last_day(self):
        self.close(self.term)
        self.assertEqual(reports.school_position(self.school)["arrearsMinor"], 130_000 * N)

    def test_overdue_is_what_is_past_its_due_date(self):
        self.set_today(date(2026, 10, 20))
        position = reports.school_position(self.school)
        self.assertEqual(position["overdueMinor"], 130_000 * N)
        self.assertEqual(position["arrearsMinor"], 0)  # late, but the term is still running

    def test_the_school_position_adds_up_and_counts_the_families_owing(self):
        position = reports.school_position(self.school)
        self.assertTrue(position["available"])
        self.assertEqual((position["outstandingMinor"], position["familiesOwing"], position["creditMinor"]), (290_000 * N, 2, 0))
        self.assertEqual(sum(p["outstandingMinor"] for p in position["periods"]), position["outstandingMinor"])

    def test_credit_families_hold_is_reported_apart_and_never_netted_off(self):
        allocation.allocate(self.payment(80_000 * N), self.family_ada)  # 40,000 over First Term, then 40,000 to Second Term
        allocation.allocate(self.payment(200_000 * N), self.family_bola)  # more than Bola's whole bill
        position = reports.school_position(self.school)
        self.assertGreater(position["creditMinor"], 0)
        self.assertEqual(position["outstandingMinor"], sum(p["outstandingMinor"] for p in position["periods"]))

    def test_void_charges_are_left_out(self):
        adjustments.void_receivable(self.charged(self.bola).get(term=self.term), actor=self.owner, reason="Left before term")
        self.assertEqual(self.line(self.term)["charges"], 1)

    def test_a_session_can_be_asked_for_on_its_own(self):
        other = AcademicSession.objects.create(school=self.school, code="2025/26", name="2025/2026", starts_on=date(2025, 9, 1), ends_on=date(2026, 7, 1), status="closed")
        self.assertEqual(reports.by_term(self.school, session=other)["periods"], [])
        self.assertEqual(len(reports.by_term(self.school, session=self.session)["periods"]), 2)

    def test_a_school_with_no_charges_has_no_figures_rather_than_zeros(self):
        position = reports.school_position(self.other_school)
        self.assertEqual((position["available"], position["outstandingMinor"], position["periods"]), (False, 0, []))

    def test_another_schools_charges_never_appear(self):
        other_session, _, other_primary, _ = self.make_year(self.other_school)
        stranger = self.make_student("Zed", "Other", school=self.other_school)
        self.enroll(stranger, other_primary, other_session)
        families.ensure_family_for_student(self.other_school, stranger)
        schedule = schedules.create_schedule(self.other_school, session=other_session, name="Theirs", actor=self.other_owner)
        schedules.add_item(schedule, actor=self.other_owner, code="X", name="X", amount_minor=999 * N, due_date=CLOCK + timedelta(days=5))
        schedules.publish(schedule, actor=self.other_owner)
        self.assertEqual(reports.school_position(self.school)["outstandingMinor"], 290_000 * N)
        self.assertEqual(reports.school_position(self.other_school)["outstandingMinor"], 999 * N)


class PositionSurvivesChunkingTests(CalendarTestCase):
    def test_reading_many_charges_in_pieces_gives_the_same_figures_as_all_at_once(self):
        students = [self.pupil(f"Kid{i}", "Many") for i in range(7)]
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=10_000 * N), actor=self.owner)
        allocation.allocate(self.payment(4_000 * N), families.family_of(students[0]))
        receivables = list(StudentReceivable.objects.all())
        whole = ledger.positions(receivables)
        with mock.patch.object(ledger, "CHUNK", 2):
            pieces = ledger.positions(receivables)
        self.assertEqual(whole, pieces)
        self.assertEqual(len(whole), 7)


class StatementByPeriodTests(CalendarTestCase):
    def test_a_statement_names_the_term_of_each_line_and_rolls_up_by_period(self):
        ada = self.pupil("Ada", "Eze")
        family = families.family_of(ada)
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=100_000 * N), actor=self.owner)
        schedules.publish(self.fees(self.term2, due=date(2027, 1, 20), amount=80_000 * N), actor=self.owner)
        allocation.allocate(self.payment(100_000 * N), family)
        self.set_today(date(2027, 1, 12))
        built = statements.build(family, session=self.session)
        lines = built["students"][0]["lines"]
        self.assertEqual([(l["sessionName"], l["termName"]) for l in lines], [("2026/2027", "First Term"), ("2026/2027", "Second Term")])
        by_period = built["byPeriod"]
        self.assertEqual([p["termName"] for p in by_period], ["First Term", "Second Term"])
        self.assertEqual((by_period[0]["outstandingMinor"], by_period[1]["outstandingMinor"]), (0, 80_000 * N))
        self.assertEqual((built["position"]["arrearsMinor"], built["position"]["currentMinor"]), (0, 80_000 * N))

    def test_the_arrears_of_an_ended_term_show_on_the_statement(self):
        ada = self.pupil("Ada", "Eze")
        family = families.family_of(ada)
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=100_000 * N), actor=self.owner)
        self.set_today(date(2027, 1, 12))
        built = statements.build(family, session=self.session)
        self.assertEqual((built["position"]["arrearsMinor"], built["position"]["currentMinor"]), (100_000 * N, 0))
        self.assertTrue(built["byPeriod"][0]["isPast"])


class CalendarApiTests(test_api.ApiTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.primary, self.jss = self.make_year()
        self.term2 = AcademicTerm.objects.create(
            session=self.session, code="T2", name="Second Term", sequence=2, starts_on=date(2027, 1, 11), ends_on=date(2027, 4, 2), status="planned",
        )
        self.ada = self.make_student("Ada", "Eze")
        self.enroll(self.ada, self.primary, self.session)
        families.ensure_family_for_student(self.school, self.ada)

    def test_the_calendar_says_what_is_current_and_what_a_schedule_can_be_made_for(self):
        body = self.get("calendar/").json()
        self.assertEqual(body["current"], {"sessionId": str(self.session.id), "termId": str(self.term.id), "label": "2026/2027 · First Term"})
        self.assertEqual(body["dueDateLeadDays"], periods.DUE_LEAD_DAYS)
        session = body["sessions"][0]
        self.assertEqual((session["name"], session["isCurrent"], session["status"]), ("2026/2027", True, "active"))
        self.assertEqual([(t["name"], t["isCurrent"], t["status"]) for t in session["terms"]], [("First Term", True, "active"), ("Second Term", False, "planned")])

    def test_a_schedule_posted_with_no_session_is_for_the_current_period(self):
        made = self.post("fee-schedules/", {"name": "Tuition"}).json()["schedule"]
        self.assertEqual((made["sessionId"], made["termId"]), (str(self.session.id), str(self.term.id)))
        whole = self.post("fee-schedules/", {"name": "Levy", "wholeSession": True}).json()["schedule"]
        self.assertEqual((whole["sessionId"], whole["termId"]), (str(self.session.id), None))

    def test_a_closed_period_is_refused_with_a_reason_the_app_can_show(self):
        AcademicTerm.objects.filter(pk=self.term.pk).update(status="closed")
        response = self.post("fee-schedules/", {"name": "Late", "sessionId": str(self.session.id), "termId": str(self.term.id)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "period_closed")

    def test_schedules_and_charges_can_be_filtered_by_session_and_term(self):
        schedule = self.post("fee-schedules/", {"name": "Tuition"}).json()["schedule"]
        self.post(f"fee-schedules/{schedule['id']}/items/", {"code": "T", "name": "Tuition", "amountMinor": 50_000 * N, "dueDate": "2026-10-15"})
        self.assertEqual(self.post(f"fee-schedules/{schedule['id']}/publish/").status_code, 200)
        second = self.post("fee-schedules/", {"name": "Term 2", "sessionId": str(self.session.id), "termId": str(self.term2.id)}).json()["schedule"]
        self.post(f"fee-schedules/{second['id']}/items/", {"code": "T2", "name": "Tuition", "amountMinor": 60_000 * N, "dueDate": "2027-01-20"})
        self.post(f"fee-schedules/{second['id']}/publish/")

        by_term = lambda term: self.get(f"charges/?term={term.id}").json()["receivables"]
        self.assertEqual([(c["termName"], c["sessionName"], c["grossMinor"]) for c in by_term(self.term)], [("First Term", "2026/2027", 50_000 * N)])
        self.assertEqual([c["termName"] for c in by_term(self.term2)], ["Second Term"])
        self.assertEqual(len(self.get(f"charges/?session={self.session.id}").json()["receivables"]), 2)
        self.assertEqual(len(self.get(f"fee-schedules/?term={self.term2.id}").json()["schedules"]), 1)
        self.assertEqual(self.get("charges/?term=not-a-uuid").status_code, 400)

    def test_the_term_report_and_the_position_are_for_the_owner_and_finance_only(self):
        for tail in ("reports/terms/", "reports/position/", "calendar/"):
            for who in (self.owner, self.finance, self.authority):
                self.assertEqual(self.get(tail, who=who).status_code, 200, (tail, who.role))
            for role in ("teacher", "parent", "student", "driver"):
                self.assertEqual(self.get(tail, who=self.members[role]).status_code, 403, (tail, role))

    def test_the_reports_read_what_is_owed(self):
        self.post("fee-schedules/", {"name": "Tuition"})
        schedule = self.post("fee-schedules/", {"name": "Fees"}).json()["schedule"]
        self.post(f"fee-schedules/{schedule['id']}/items/", {"code": "T", "name": "Tuition", "amountMinor": 50_000 * N, "dueDate": "2026-10-15"})
        self.post(f"fee-schedules/{schedule['id']}/publish/")
        report = self.get("reports/terms/").json()["report"]
        self.assertEqual([(p["termName"], p["outstandingMinor"]) for p in report["periods"]], [("First Term", 50_000 * N)])
        position = self.get("reports/position/").json()["position"]
        self.assertEqual((position["available"], position["outstandingMinor"], position["familiesOwing"]), (True, 50_000 * N, 1))

    def test_another_schools_session_is_not_found(self):
        other_session, *_ = self.make_year(self.other_school)
        self.assertEqual(self.get(f"reports/terms/?session={other_session.id}").status_code, 404)

    def test_the_preview_says_who_joined_after_the_term_and_which_period_it_is_for(self):
        late = self.make_student("Bola", "Ojo")
        enrollment = self.enroll(late, self.primary, self.session)
        EnrollmentAcademicContext.objects.filter(enrollment=enrollment).update(entry_term=self.term2)
        families.ensure_family_for_student(self.school, late)
        schedule = self.post("fee-schedules/", {"name": "Tuition"}).json()["schedule"]
        self.post(f"fee-schedules/{schedule['id']}/items/", {"code": "T", "name": "Tuition", "amountMinor": 50_000 * N, "dueDate": "2026-10-15"})
        preview = self.get(f"fee-schedules/{schedule['id']}/preview/").json()["preview"]
        self.assertEqual([s["name"] for s in preview["joinedLater"]], ["Bola Ojo"])
        self.assertEqual(preview["period"], {"label": "2026/2027 · First Term", "closed": False})
        self.assertEqual(preview["unclassified"], [])
        published = self.post(f"fee-schedules/{schedule['id']}/publish/").json()["report"]
        self.assertEqual([s["name"] for s in published["joinedLater"]], ["Bola Ojo"])


class DashboardFiguresTests(CalendarTestCase):
    """What the school is owed is a real figure on the dashboards once it has charges - and only then."""

    def dashboard(self, kind, who=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(f"/api/v1/dashboards/schools/{self.school.id}/{kind}/").json()

    def test_until_there_are_charges_the_dashboards_say_outstanding_balances_are_unavailable(self):
        finance = self.dashboard("finance", self.members["accountant"])
        self.assertFalse(finance["collections"]["outstandingFeesAvailable"])
        self.assertFalse(finance["collections"]["receivables"]["available"])
        self.assertIn("outstanding balances", finance["notAvailableYet"])

    def test_once_the_school_has_charged_fees_what_is_owed_is_shown_by_period(self):
        ada = self.pupil("Ada", "Eze")
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=100_000 * N), actor=self.owner)
        schedules.publish(self.fees(self.term2, due=date(2027, 1, 20), amount=80_000 * N), actor=self.owner)
        allocation.allocate(self.payment(30_000 * N), families.family_of(ada))
        for kind, who in (("owner", self.owner), ("finance", self.members["accountant"])):
            body = self.dashboard(kind, who)
            self.assertTrue(body["collections"]["outstandingFeesAvailable"], kind)
            owed = body["collections"]["receivables"]
            self.assertEqual((owed["outstandingMinor"], owed["familiesOwing"]), (150_000 * N, 1), kind)
            self.assertEqual([p["termName"] for p in owed["periods"]], ["First Term", "Second Term"], kind)
        self.assertNotIn("outstanding balances", self.dashboard("finance", self.members["accountant"])["notAvailableYet"])

    def test_the_dashboard_and_the_receivables_reports_are_the_same_figures(self):
        self.pupil("Ada", "Eze")
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15), amount=100_000 * N), actor=self.owner)
        # The dashboard reads the real clock (it is the bank summary's), so compare like with like.
        self.assertEqual(self.dashboard("owner")["collections"]["receivables"], reports.school_position(self.school, today=timezone.localdate()))

    def test_another_school_is_still_told_it_has_nothing_to_show(self):
        self.pupil("Ada", "Eze")
        schedules.publish(self.fees(self.term, due=date(2026, 10, 15)), actor=self.owner)
        self.client.force_authenticate(self.other_owner.user)
        body = self.client.get(f"/api/v1/dashboards/schools/{self.other_school.id}/finance/").json()
        self.assertFalse(body["collections"]["outstandingFeesAvailable"])
        self.assertIn("outstanding balances", body["notAvailableYet"])
