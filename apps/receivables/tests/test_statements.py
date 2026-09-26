from datetime import date, timedelta

from django.db import IntegrityError, transaction

from .. import adjustments, allocation, collection_accounts, families, ledger, schedules, statements
from ..errors import Refused
from ..models import FamilyStatement, FinanceAuditEvent
from .base import ReceivablesTestCase

N = 100


class StatementTestCase(ReceivablesTestCase):
    def setUp(self):
        super().setUp()
        self.bello_family()
        self.publish_bello_fees()
        adjustments.adjust(self.charge(self.ahmad), kind="scholarship", amount_minor=20_000 * N, reason="Founder scholarship", actor=self.owner)
        adjustments.adjust(self.charge(self.aisha), kind="discount", amount_minor=10_000 * N, reason="Sibling discount", actor=self.owner)


class BuildTests(StatementTestCase):
    def test_it_shows_each_student_their_gross_what_was_taken_off_and_what_is_owed(self):
        s = statements.build(self.family, session=self.session)
        self.assertEqual([st["name"] for st in s["students"]], ["Ahmad Bello", "Aisha Bello", "Maryam Bello"])
        ahmad = s["students"][0]
        self.assertEqual(
            (ahmad["totals"]["grossMinor"], ahmad["totals"]["adjustmentsMinor"], ahmad["totals"]["netMinor"], ahmad["totals"]["outstandingMinor"]),
            (120_000 * N, 20_000 * N, 100_000 * N, 100_000 * N),
        )
        self.assertEqual((s["totals"]["grossMinor"], s["totals"]["netMinor"], s["totals"]["outstandingMinor"]), (300_000 * N, 270_000 * N, 270_000 * N))
        self.assertEqual(s["position"]["outstandingMinor"], 270_000 * N)
        self.assertEqual(s["currency"], "NGN")

    def test_it_is_always_worked_out_from_the_ledger_never_remembered(self):
        first = statements.issue(self.family, session=self.session, actor=self.owner)
        allocation.allocate(self.payment(270_000 * N), self.family)
        live = statements.build(self.family, session=self.session)
        self.assertEqual((live["totals"]["outstandingMinor"], live["totals"]["paidMinor"]), (0, 270_000 * N))
        # ...while the snapshot taken the day it was issued still says what it said then (for reference only).
        self.assertEqual(FamilyStatement.objects.get(pk=first.pk).snapshot["totals"]["outstandingMinor"], 270_000 * N)
        self.assertEqual(ledger.family_position(self.family).outstanding, 0)

    def test_the_period_narrows_the_lines_but_the_overall_position_is_always_given(self):
        due = date.today() + timedelta(days=100)
        second = schedules.create_schedule(self.school, session=self.session, name="Elsewhere", actor=self.owner)
        schedules.add_item(second, actor=self.owner, code="X", name="Extra", amount_minor=5_000 * N, due_date=due, scope="student", student=self.ahmad)
        schedules.publish(second, actor=self.owner)
        from ..models import StudentReceivable

        StudentReceivable.objects.filter(fee_item__code="X").update(term=None)
        for_term = statements.build(self.family, session=self.session, term=self.term)
        self.assertEqual(for_term["totals"]["grossMinor"], 300_000 * N)  # the extra charge has no term
        self.assertEqual(for_term["position"]["grossMinor"], 305_000 * N)  # but the family's own position includes it

    def test_a_void_charge_is_not_on_the_statement(self):
        adjustments.void_receivable(self.charge(self.maryam), actor=self.owner, reason="Free place")
        s = statements.build(self.family)
        self.assertEqual([st["name"] for st in s["students"]], ["Ahmad Bello", "Aisha Bello"])
        self.assertEqual(s["totals"]["grossMinor"], 220_000 * N)

    def test_credit_and_the_collection_account_are_shown_without_secrets(self):
        collection_accounts.register(self.family, provider="monnify", account_number="8012345678", account_name="BRIGHTGATE / BELLO", bank_name="Wema", actor=self.owner, provider_meta={"apiKey": "SECRET"})
        allocation.allocate(self.payment(300_000 * N), self.family)
        s = statements.build(self.family)
        self.assertEqual(s["position"]["creditMinor"], 30_000 * N)
        self.assertEqual(s["collectionAccounts"], [{"provider": "monnify", "bankName": "Wema", "accountNumber": "8012345678", "accountName": "BRIGHTGATE / BELLO", "status": "dormant"}])
        self.assertNotIn("SECRET", str(s))

    def test_a_closed_account_is_not_offered(self):
        account = collection_accounts.register(self.family, provider="monnify", account_number="8012345678", actor=self.owner)
        collection_accounts.close(account, actor=self.owner, reason="Retired")
        self.assertEqual(statements.build(self.family)["collectionAccounts"], [])

    def test_overdue_is_what_is_owed_past_its_due_date(self):
        s = statements.build(self.family, today=date.today() + timedelta(days=60))
        self.assertEqual(s["position"]["overdueMinor"], 270_000 * N)
        self.assertEqual(statements.build(self.family, today=date.today())["position"]["overdueMinor"], 0)


class IssuingTests(StatementTestCase):
    def test_a_statement_gets_a_number_a_date_and_an_issuer(self):
        statement = statements.issue(self.family, session=self.session, term=self.term, actor=self.members["accountant"])
        self.assertRegex(statement.number, r"^STM-\d{4}-000001$")
        self.assertEqual((statement.issued_by, statement.family, statement.status), (self.members["accountant"], self.family, "issued"))
        self.assertIsNotNone(statement.issued_at)
        self.assertTrue(FinanceAuditEvent.objects.filter(kind="statement_issued", object_id=str(statement.id)).exists())

    def test_numbers_run_on_within_a_school_and_each_school_counts_for_itself(self):
        a = statements.issue(self.family, session=self.session, actor=self.owner)
        b = statements.issue(self.family, session=self.session, actor=self.owner)
        self.assertEqual((a.number[-6:], b.number[-6:]), ("000001", "000002"))
        other_session, _, _, _ = self.make_year(self.other_school)
        other_family = families.create_family(self.other_school, display_name="Elsewhere")
        c = statements.issue(other_family, session=other_session, actor=self.other_owner)
        self.assertEqual(c.number[-6:], "000001")

    def test_only_those_who_work_the_ledger_may_issue_one(self):
        for role in ("teacher", "parent", "student", "principal"):
            with self.assertRaises(Refused, msg=role):
                statements.issue(self.family, session=self.session, actor=self.members[role])
        with self.assertRaises(Refused):
            statements.issue(self.family, session=self.session, actor=self.other_owner)

    def test_another_schools_session_is_refused(self):
        other_session, _, _, _ = self.make_year(self.other_school)
        with self.assertRaises(Refused):
            statements.issue(self.family, session=other_session, actor=self.owner)

    def test_a_number_is_unique_in_the_database_too(self):
        statement = statements.issue(self.family, session=self.session, actor=self.owner)
        with self.assertRaises(IntegrityError), transaction.atomic():
            FamilyStatement.objects.create(school=self.school, family=self.family, session=self.session, number=statement.number)
