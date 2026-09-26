from datetime import date, datetime
from zoneinfo import ZoneInfo

from apps.academics.models import AcademicLifecycleStatus, AcademicSession, AcademicTerm

from .. import review, summary
from ..constants import ConnectionStatus, ConnectionType
from ..models import BankConnection
from .base import BankTestCase

LAGOS = ZoneInfo("Africa/Lagos")
#: A Wednesday, midday in Lagos.
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=LAGOS)


def at(day, hour=10, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=LAGOS)


class SummaryTestCase(BankTestCase):
    def real_connection(self, provider="paystack", label="", school=None, status=ConnectionStatus.CONNECTED, active=False, environment="live"):
        """A real (not sandbox) provider connection. It is made directly: the only connector that can be reached in a test is the sandbox,
        and this is about how real money is counted."""
        return BankConnection.objects.create(
            school=school or self.school, provider=provider, connection_type=ConnectionType.COLLECTION_PROVIDER, environment=environment,
            merchant_name=f"{provider} merchant", label=label or f"{provider} connection", status=status, is_sandbox=False,
            is_active_provider=active and status == ConnectionStatus.CONNECTED,
        )

    def open_term(self):
        session = AcademicSession.objects.create(
            school=self.school, code="2026/27", name="2026/2027", starts_on=date(2026, 9, 7), ends_on=date(2027, 7, 20),
            status=AcademicLifecycleStatus.ACTIVE,
        )
        return AcademicTerm.objects.create(
            session=session, code="T1", name="First Term", sequence=1, starts_on=date(2026, 9, 7), ends_on=date(2026, 12, 18),
            status=AcademicLifecycleStatus.ACTIVE,
        )

    def build(self, **kw):
        kw.setdefault("now", NOW)
        return summary.build(self.school, **kw)


class EmptySchoolTests(SummaryTestCase):
    def test_with_nothing_connected_it_says_so_instead_of_showing_zeros_as_facts(self):
        body = self.build()
        self.assertFalse(body["available"])
        self.assertEqual(body["providers"], {"connected": 0, "needAttention": 0, "active": None})
        self.assertEqual(body["today"], {"amountMinor": 0, "count": 0})
        self.assertIsNone(body["thisTerm"])
        self.assertEqual((body["byProvider"], body["recent"]), ([], []))
        self.assertFalse(body["outstandingFeesAvailable"])
        self.assertEqual(body["period"]["key"], "all")  # no open term to be "this term"


class PeriodTests(SummaryTestCase):
    def setUp(self):
        super().setUp()
        self.term = self.open_term()
        self.account = self.real_connection()
        for day, hour, amount in ((23, 10, 1_000_000), (21, 9, 2_000_000), (20, 15, 4_000_000), (1, 10, 8_000_000)):
            self.deposit(self.account, transaction_date=at(day, hour), amount_minor=amount, narration=f"pay {day}", sender_name=f"S{day}")

    def test_today_this_week_and_this_term(self):
        body = self.build()
        self.assertEqual(body["today"], {"amountMinor": 1_000_000, "count": 1})
        self.assertEqual(body["thisWeek"], {"amountMinor": 3_000_000, "count": 2})
        self.assertEqual(body["thisTerm"], {"amountMinor": 7_000_000, "count": 3})

    def test_the_chosen_period_drives_the_breakdowns(self):
        for period, expected in (("today", 1_000_000), ("week", 3_000_000), ("term", 7_000_000), ("all", 15_000_000)):
            body = self.build(period=period)
            self.assertEqual((body["period"]["key"], body["selected"]["amountMinor"]), (period, expected), period)
            self.assertEqual(sum(b["amountMinor"] for b in body["byProvider"]), expected, period)

    def test_the_period_says_what_it_covers(self):
        body = self.build(period="term")
        self.assertEqual((body["period"]["label"], body["period"]["from"], body["period"]["to"]), ("First Term", date(2026, 9, 7), date(2026, 12, 18)))
        week = self.build(period="week")["period"]
        self.assertEqual((week["from"], week["to"]), (date(2026, 9, 21), date(2026, 9, 23)))

    def test_a_late_night_transfer_belongs_to_the_schools_own_day(self):
        # 23:30 UTC on the 22nd is 00:30 in Lagos on the 23rd.
        self.deposit(
            self.account, transaction_date=datetime(2026, 9, 22, 23, 30, tzinfo=ZoneInfo("UTC")),
            amount_minor=500_000, narration="late", sender_name="Late",
        )
        self.assertEqual(self.build()["today"], {"amountMinor": 1_500_000, "count": 2})

    def test_an_unknown_period_falls_back_to_the_term(self):
        self.assertEqual(self.build(period="fortnight")["period"]["key"], "term")


class WhatCountsTests(SummaryTestCase):
    def setUp(self):
        super().setUp()
        self.open_term()
        self.account = self.real_connection()

    def pay(self, amount=1_000_000, **over):
        over.setdefault("transaction_date", at(23))
        over.setdefault("sender_name", f"Sender {amount} {over.get('narration', '')}")
        return self.deposit(self.account, amount_minor=amount, **over)

    def test_money_that_went_back_and_held_duplicates_are_not_counted_as_collected(self):
        counted = self.pay(1_000_000, narration="a")
        for status in ("reversed", "refunded", "duplicate"):
            row = self.pay(2_000_000, narration=status)
            type(row).objects.filter(id=row.id).update(reconciliation_status=status)
        body = self.build()
        self.assertEqual(body["today"], {"amountMinor": counted.amount_minor, "count": 1})
        self.assertEqual(body["reconciliation"]["pendingReviewCount"], 2)  # the pending payment and the held duplicate

    def test_money_going_out_is_never_collected(self):
        self.pay(1_000_000, narration="in")
        self.pay(9_000_000, narration="out", direction="debit")
        self.assertEqual(self.build()["today"]["amountMinor"], 1_000_000)

    def test_only_naira_is_totalled_and_the_rest_is_counted_separately(self):
        self.pay(1_000_000, narration="naira")
        self.pay(5_000_000, narration="dollars", currency="USD")
        body = self.build()
        self.assertEqual((body["today"]["amountMinor"], body["otherCurrencyTransactions"]), (1_000_000, 1))

    def test_another_schools_money_is_never_included(self):
        self.pay(1_000_000, narration="ours")
        theirs = self.real_connection(school=self.other_school)
        self.deposit(theirs, amount_minor=9_000_000, transaction_date=at(23), narration="theirs")
        self.assertEqual(self.build()["today"]["amountMinor"], 1_000_000)
        self.assertEqual(summary.build(self.other_school, now=NOW)["today"]["amountMinor"], 9_000_000)


class ReconciliationTotalsTests(SummaryTestCase):
    def test_reconciled_and_unreconciled_add_up_to_what_was_collected(self):
        self.open_term()
        account = self.legacy_connection("tuition")  # the student-matching engine serves payments from the earlier bank-account model
        aisha = self.make_student("BG-0042", "Aisha", "Bello")
        matched = self.deposit(account, transaction_date=at(23), narration="BG-0042", amount_minor=4_000_000)
        donation = self.deposit(account, transaction_date=at(23), narration="donation", sender_name="Church", amount_minor=1_000_000)
        review.decide(self.owner, donation.id, action="unrelated_income", note="Donation")
        partial = self.deposit(account, transaction_date=at(23), narration="mixed", sender_name="Parent", amount_minor=3_000_000)
        review.decide(self.owner, partial.id, action="split", allocations=[{"studentId": str(aisha.id), "amountMinor": 1_000_000}])
        self.deposit(account, transaction_date=at(23), narration="mystery", sender_name="Unknown", amount_minor=2_000_000)
        body = self.build()
        rec = body["reconciliation"]
        # matched 4m + donation 1m + the allocated 1m of the partial; the other 2m of it and the mystery 2m are open.
        self.assertEqual(rec["reconciledMinor"], 6_000_000)
        self.assertEqual(rec["unreconciledMinor"], 4_000_000)
        self.assertEqual(rec["reconciledMinor"] + rec["unreconciledMinor"], body["today"]["amountMinor"])
        self.assertEqual(rec["pendingReviewCount"], 2)
        self.assertEqual(matched.reconciliation_status, "matched")


class BreakdownTests(SummaryTestCase):
    def test_by_provider_biggest_first(self):
        self.open_term()
        first = self.real_connection("paystack", "Fees by Paystack")
        second = self.real_connection("monnify", "Fees by Monnify")
        for n, amount in enumerate((3_000_000, 2_000_000)):
            self.deposit(first, transaction_date=at(23), amount_minor=amount, narration=f"t{n}", sender_name=f"T{n}")
        self.deposit(second, transaction_date=at(23), amount_minor=9_000_000, narration="bus", sender_name="B")
        body = self.build()
        self.assertEqual(
            [(b["provider"], b["label"], b["environment"], b["amountMinor"], b["count"]) for b in body["byProvider"]],
            [("monnify", "Fees by Monnify", "live", 9_000_000, 1), ("paystack", "Fees by Paystack", "live", 5_000_000, 2)],
        )


class AccountsTests(SummaryTestCase):
    def test_how_many_providers_are_connected_which_need_attention_and_which_is_active(self):
        good = self.real_connection("paystack", active=True)
        self.real_connection("monnify", status=ConnectionStatus.NEEDS_REAUTH)
        self.real_connection("remita", status=ConnectionStatus.ERROR)
        self.real_connection("legacy_one", status=ConnectionStatus.DISABLED)
        self.real_connection("legacy_two", status=ConnectionStatus.REVOKED)
        body = self.build()
        self.assertTrue(body["available"])
        self.assertEqual((body["providers"]["connected"], body["providers"]["needAttention"]), (1, 2))
        self.assertEqual(
            body["providers"]["active"],
            {"connectionId": str(good.id), "provider": "paystack", "environment": "live", "merchantName": "paystack merchant"},
        )


class SandboxTests(SummaryTestCase):
    def setUp(self):
        super().setUp()
        self.open_term()
        self.test_account = self.row(self.connected()[0])
        self.deposit(self.test_account, transaction_date=at(23), amount_minor=7_000_000, narration="test money")

    def test_test_money_is_left_out_and_the_summary_says_how_much_was(self):
        body = self.build()
        self.assertEqual(body["today"]["amountMinor"], 0)
        self.assertEqual((body["sandboxIncluded"], body["sandboxHidden"]), (False, 1))
        self.assertFalse(body["available"])  # a sandbox account is not a real account
        self.assertEqual(body["recent"], [])

    def test_it_can_be_asked_for_and_is_then_labelled(self):
        body = self.build(include_sandbox=True)
        self.assertEqual((body["today"]["amountMinor"], body["sandboxIncluded"], body["sandboxHidden"]), (7_000_000, True, 0))
        self.assertTrue(body["available"])
        self.assertTrue(body["recent"][0]["isSandbox"])

    def test_real_money_is_unaffected_by_test_money_beside_it(self):
        real = self.real_connection()
        self.deposit(real, transaction_date=at(23), amount_minor=1_000_000, narration="real", sender_name="Real")
        body = self.build()
        self.assertEqual((body["today"]["amountMinor"], body["sandboxHidden"]), (1_000_000, 1))


class RecentTests(SummaryTestCase):
    def test_the_latest_payments_in_brief(self):
        account = self.real_connection()
        for n in range(1, 8):
            self.deposit(account, transaction_date=at(n, 10), amount_minor=n * 100_000, narration=f"n{n}", sender_name=f"S{n}")
        recent = self.build(recent=3)["recent"]
        self.assertEqual([r["amountMinor"] for r in recent], [700_000, 600_000, 500_000])
        self.assertEqual(set(recent[0]), {"id", "senderName", "amountMinor", "currency", "transactionDate", "provider",
                                          "reconciliationStatus", "isSandbox"})


class SummaryApiTests(SummaryTestCase):
    def test_owner_and_finance_office_read_it_and_nobody_else(self):
        for who in (self.owner, self.members["accountant"]):
            response = self.api_get("summary/", who=who)
            self.assertEqual(response.status_code, 200)
            self.assertIn("summary", response.json())
        for role in ("principal", "administrator", "teacher", "staff", "parent", "student", "driver"):
            self.assertEqual(self.api_get("summary/", who=self.members[role]).status_code, 403, role)

    def test_a_period_and_the_test_data_switch(self):
        connection = self.row(self.connected()[0])
        self.deposit(connection, amount_minor=1_000_000)
        self.assertEqual(self.api_get("summary/?period=today").json()["summary"]["period"]["key"], "today")
        self.assertEqual(self.api_get("summary/?period=all").json()["summary"]["selected"]["amountMinor"], 0)
        self.assertEqual(self.api_get("summary/?period=all&includeSandbox=true").json()["summary"]["selected"]["amountMinor"], 1_000_000)
        self.assertEqual(self.api_get("summary/?period=fortnight").status_code, 400)

    def test_another_school_sees_only_its_own(self):
        self.deposit(self.row(self.connected()[0]), amount_minor=1_000_000)
        theirs = self.api_get("summary/?includeSandbox=true&period=all", who=self.other_owner, school=self.other_school).json()["summary"]
        self.assertEqual(theirs["selected"]["amountMinor"], 0)
        self.assertEqual(self.api_get("summary/", who=self.other_owner).status_code, 403)


class DashboardTests(SummaryTestCase):
    def dashboard(self, kind, who=None, school=None):
        self.client.force_authenticate((who or self.owner).user)
        return self.client.get(f"/api/v1/dashboards/schools/{(school or self.school).id}/{kind}/")

    def test_both_dashboards_carry_the_collections_and_say_fees_owed_are_unknown(self):
        for kind, who in (("owner", self.owner), ("finance", self.members["accountant"])):
            body = self.dashboard(kind, who).json()
            self.assertIn("collections", body, kind)
            self.assertFalse(body["collections"]["outstandingFeesAvailable"])
        self.assertIn("outstanding balances", self.dashboard("finance", self.members["accountant"]).json()["notAvailableYet"])

    def test_fee_collection_is_not_available_until_a_real_account_is_connected(self):
        for kind in ("owner", "finance"):
            self.assertIn("fee collection", self.dashboard(kind).json()["notAvailableYet"], kind)
        self.real_connection()
        for kind in ("owner", "finance"):
            body = self.dashboard(kind).json()
            self.assertNotIn("fee collection", body["notAvailableYet"], kind)
            self.assertTrue(body["collections"]["available"])
        self.assertIn("students", self.dashboard("owner").json()["notAvailableYet"])

    def test_the_dashboard_and_the_collections_screen_never_disagree(self):
        account = self.real_connection()
        self.deposit(account, amount_minor=3_000_000)
        on_dashboard = self.dashboard("owner").json()["collections"]
        on_screen = self.api_get("summary/").json()["summary"]
        for key in ("today", "thisWeek", "reconciliation", "byProvider", "providers", "available"):
            self.assertEqual(on_dashboard[key], on_screen[key], key)

    def test_another_schools_dashboard_shows_none_of_it(self):
        self.real_connection()
        theirs = self.dashboard("owner", self.other_owner, self.other_school).json()["collections"]
        self.assertFalse(theirs["available"])
